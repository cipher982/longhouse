"""Worker resume handler - resumes interrupted supervisor after worker completion.

This module bridges worker completion back into the supervisor graph using LangGraph's
native interrupt()/Command(resume=...) pattern.

Key requirements:
- Idempotent: multiple callers must not resume the same run twice.
- Durable: resumed execution persists new messages to the thread.
- Interrupt-safe: supervisor may interrupt again (multiple workers sequentially).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.services.supervisor_context import reset_seq
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)


def _count_leading_system_messages(messages: list[Any]) -> int:
    """Return number of consecutive system messages at the start of a message list."""
    count = 0
    for msg in messages:
        if getattr(msg, "type", None) == "system":
            count += 1
            continue
        break
    return count


def _extract_interrupt_value(result: dict[str, Any]) -> Any | None:
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    interrupt_info = interrupts[0]
    return getattr(interrupt_info, "value", interrupt_info)


async def resume_supervisor_with_worker_result(
    db: Session,
    run_id: int,
    worker_result: str,
) -> dict[str, Any] | None:
    """Resume an interrupted supervisor run with a worker result.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    from zerg.agents_def import zerg_react_agent
    from zerg.agents_def.zerg_react_agent import get_llm_usage
    from zerg.agents_def.zerg_react_agent import reset_llm_usage
    from zerg.callbacks.token_stream import current_db_session_var
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import reset_current_thread_id
    from zerg.callbacks.token_stream import set_current_db_session
    from zerg.callbacks.token_stream import set_current_thread_id
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.connectors.context import reset_credential_resolver
    from zerg.connectors.context import set_credential_resolver
    from zerg.connectors.resolver import CredentialResolver
    from zerg.events import SupervisorEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.services.event_store import emit_run_event
    from zerg.services.supervisor_context import reset_supervisor_context
    from zerg.services.supervisor_context import set_supervisor_context

    # Load run
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        logger.error("Cannot resume: run %s not found", run_id)
        return None

    if run.status != RunStatus.WAITING:
        logger.info("Skipping resume: run %s is %s, not WAITING", run_id, run.status.value)
        return {"status": "skipped", "reason": f"run is {run.status.value}, not waiting", "run_id": run_id}

    # Ensure we always have a stable message_id for UI correlation
    if not run.assistant_message_id:
        run.assistant_message_id = str(uuid.uuid4())
        db.commit()

    # Idempotency gate: flip WAITING → RUNNING atomically.
    #
    # This prevents double-resume when:
    # - two workers finish at nearly the same time, or
    # - multiple internal callers hit /resume, or
    # - retry logic schedules concurrent resume tasks.
    updated = (
        db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.status == RunStatus.WAITING).update({AgentRun.status: RunStatus.RUNNING})
    )
    db.commit()
    if updated == 0:
        # Another caller already resumed (or the run changed state).
        return {"status": "skipped", "reason": "run no longer waiting", "run_id": run_id}

    # Re-load with relationships after the status transition
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        return None

    thread = run.thread
    agent = run.agent
    owner_id = agent.owner_id
    message_id = run.assistant_message_id

    logger.info("Resuming supervisor run %s (thread=%s)", run_id, thread.id)

    # Emit "resumed" event so UIs can clear any waiting indicators (best-effort)
    await emit_run_event(
        db=db,
        run_id=run.id,
        event_type="supervisor_resumed",
        payload={
            "agent_id": agent.id,
            "thread_id": thread.id,
            "message_id": message_id,
            "owner_id": owner_id,
        },
    )

    # Set up contexts (mirrors SupervisorService.run_supervisor)
    _supervisor_ctx_tokens = set_supervisor_context(
        run_id=run.id,
        db=db,
        owner_id=owner_id,
        message_id=message_id,
    )

    # Set up injected emitter for event emission (Phase 2 of emitter refactor)
    # SupervisorEmitter always emits supervisor_tool_* events regardless of contextvar state
    # This is critical for resume - without this, leaked WorkerContext could cause
    # supervisor tool events to emit as worker_tool_* events
    # Note: Emitter does NOT hold a DB session - event emission opens its own session
    _supervisor_emitter = SupervisorEmitter(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
    )
    _emitter_token = set_emitter(_supervisor_emitter)

    _user_ctx_token = set_current_user_id(owner_id)
    _db_ctx_token = set_current_db_session(db)
    _thread_ctx_token = set_current_thread_id(thread.id)
    _cred_ctx_token = set_credential_resolver(
        CredentialResolver(
            agent_id=agent.id,
            db=db,
            owner_id=owner_id,
        )
    )

    thread_service = ThreadService()

    try:
        # Capture DB conversation length for slicing (excludes stale DB system messages).
        db_messages = thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [m for m in db_messages if getattr(m, "type", None) != "system"]

        # Resume the graph - Command(resume=...) makes interrupt() return this value inside spawn_worker.
        runnable = zerg_react_agent.get_runnable(agent)
        config = {"configurable": {"thread_id": str(thread.id)}}

        reset_llm_usage()
        result = await runnable.ainvoke(Command(resume=worker_result), config)

        # Handle interrupt (supervisor can spawn another worker after resume)
        if isinstance(result, dict) and result.get("__interrupt__"):
            interrupt_value = _extract_interrupt_value(result)
            interrupt_message = "Working on this in the background..."
            job_id = None
            if isinstance(interrupt_value, dict):
                job_id = interrupt_value.get("job_id")
                interrupt_message = interrupt_value.get("message") or interrupt_message
            elif interrupt_value is not None:
                interrupt_message = str(interrupt_value)

            # Persist any new messages returned in the interrupted state (best-effort)
            messages = result.get("messages") if isinstance(result.get("messages"), list) else None
            if messages:
                leading_system = _count_leading_system_messages(messages)
                offset = leading_system + len(conversation_msgs)
                new_messages = messages[offset:] if offset < len(messages) else []
                if new_messages:
                    thread_service.save_new_messages(
                        db,
                        thread_id=thread.id,
                        messages=new_messages,
                        processed=True,
                    )

            # Flip back to WAITING
            run.status = RunStatus.WAITING
            run.duration_ms = _compute_duration_ms(run.started_at)
            db.commit()

            await emit_run_event(
                db=db,
                run_id=run.id,
                event_type="supervisor_waiting",
                payload={
                    "agent_id": agent.id,
                    "thread_id": thread.id,
                    "job_id": job_id,
                    "message": interrupt_message,
                    "owner_id": owner_id,
                    "message_id": message_id,
                    "close_stream": False,
                },
            )

            await emit_run_event(
                db=db,
                run_id=run.id,
                event_type="run_updated",
                payload={
                    "agent_id": agent.id,
                    "status": "waiting",
                    "thread_id": thread.id,
                    "owner_id": owner_id,
                },
            )

            logger.info("Supervisor run %s interrupted again (WAITING for worker job %s)", run_id, job_id)
            return {"status": "waiting", "run_id": run_id, "job_id": job_id, "message": interrupt_message}

        # Normal completion: extract message history from result
        if isinstance(result, dict):
            messages = result.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError(f"Unexpected resume result dict (no messages): {list(result.keys())}")
        else:
            messages = result

        # Persist ONLY the new messages since the DB conversation (skip leading injected system/context).
        leading_system = _count_leading_system_messages(messages)
        offset = leading_system + len(conversation_msgs)
        new_messages = messages[offset:] if offset < len(messages) else []
        created_rows = []
        if new_messages:
            created_rows = thread_service.save_new_messages(
                db,
                thread_id=thread.id,
                messages=new_messages,
                processed=True,
            )

        # Best-effort: store usage metadata on the last assistant row
        usage = get_llm_usage() or {}
        usage_payload = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
        }
        if created_rows and any(v is not None for v in usage_payload.values()):
            last_assistant_row = next((row for row in reversed(created_rows) if row.role == "assistant"), None)
            if last_assistant_row is not None:
                existing_meta = dict(last_assistant_row.message_metadata or {})
                existing_meta["usage"] = usage_payload
                last_assistant_row.message_metadata = existing_meta
                db.commit()

        # Extract the final response (prefer DB rows we just created)
        final_response = None
        for row in reversed(created_rows):
            if row.role == "assistant" and row.content:
                final_response = row.content
                break

        # Update run as SUCCESS
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)
        run.status = RunStatus.SUCCESS
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        # Token usage (partial; resumed phase only)
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, int):
            run.total_tokens = (run.total_tokens or 0) + total_tokens
        db.commit()

        # Emit completion + run_updated events (matches SupervisorService schema)
        await emit_run_event(
            db=db,
            run_id=run.id,
            event_type="supervisor_complete",
            payload={
                "agent_id": agent.id,
                "thread_id": thread.id,
                "result": final_response or "(No result)",
                "status": "success",
                "duration_ms": duration_ms,
                "debug_url": f"/supervisor/{run.id}",
                "owner_id": owner_id,
                "message_id": message_id,
                "usage": usage_payload,
            },
        )

        await emit_run_event(
            db=db,
            run_id=run.id,
            event_type="run_updated",
            payload={
                "agent_id": agent.id,
                "status": "success",
                "finished_at": end_time.isoformat(),
                "duration_ms": duration_ms,
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        reset_seq(run.id)
        from zerg.agents_def.zerg_react_agent import clear_evidence_mount_warning

        clear_evidence_mount_warning(run.id)
        logger.info("Successfully resumed and completed supervisor run %s", run_id)
        return {"status": "success", "result": final_response}

    except Exception as e:
        logger.exception("Failed to resume supervisor run %s: %s", run_id, e)

        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        await emit_run_event(
            db=db,
            run_id=run.id,
            event_type="error",
            payload={
                "thread_id": thread.id,
                "message": str(e),
                "status": "error",
                "owner_id": owner_id,
            },
        )

        await emit_run_event(
            db=db,
            run_id=run.id,
            event_type="run_updated",
            payload={
                "agent_id": agent.id,
                "status": "failed",
                "finished_at": end_time.isoformat(),
                "duration_ms": duration_ms,
                "error": str(e),
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        reset_seq(run.id)
        from zerg.agents_def.zerg_react_agent import clear_evidence_mount_warning

        clear_evidence_mount_warning(run.id)
        return {"status": "error", "error": str(e)}

    finally:
        # Reset all contexts and emitter
        reset_supervisor_context(_supervisor_ctx_tokens)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)
        current_db_session_var.reset(_db_ctx_token)
        reset_current_thread_id(_thread_ctx_token)
        reset_credential_resolver(_cred_ctx_token)


def _compute_duration_ms(started_at, *, end_time: datetime | None = None) -> int:
    if started_at is None:
        return 0
    end_dt = end_time or datetime.now(timezone.utc)
    try:
        started_dt = started_at.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    return max(0, int((end_dt - started_dt).total_seconds() * 1000))
