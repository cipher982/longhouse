"""Worker resume handler - resumes interrupted supervisor after worker completion.

This module provides two paths for resuming supervisors:
1. LangGraph path (legacy): Uses Command(resume=...) for checkpoint-based resume
2. LangGraph-free path (new): Uses AgentRunner.run_continuation() for DB-based resume

Key requirements:
- Idempotent: multiple callers must not resume the same run twice.
- Durable: resumed execution persists new messages to the thread.
- Interrupt-safe: supervisor may interrupt again (multiple workers sequentially).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.services.supervisor_context import reset_seq
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)

# Feature flag for rollback - use LangGraph path if set
USE_LANGGRAPH_SUPERVISOR = os.getenv("USE_LANGGRAPH_SUPERVISOR", "0") == "1"


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


# ---------------------------------------------------------------------------
# LangGraph-free continuation (new path)
# ---------------------------------------------------------------------------


async def _continue_supervisor_langgraph_free(
    db: Session,
    run_id: int,
    worker_result: str,
    job_id: int | None = None,
) -> dict[str, Any] | None:
    """Resume supervisor using the LangGraph-free AgentRunner.run_continuation().

    This is the new path that doesn't rely on LangGraph checkpointing.

    Args:
        db: Database session.
        run_id: AgentRun ID to resume.
        worker_result: Worker's result string.
        job_id: Optional WorkerJob ID to look up tool_call_id.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.events import SupervisorEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.managers.agent_runner import AgentInterrupted
    from zerg.managers.agent_runner import AgentRunner
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

    # Ensure stable message_id
    if not run.assistant_message_id:
        run.assistant_message_id = str(uuid.uuid4())
        db.commit()

    # Idempotency gate: WAITING → RUNNING atomically
    updated = (
        db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.status == RunStatus.WAITING).update({AgentRun.status: RunStatus.RUNNING})
    )
    db.commit()
    if updated == 0:
        return {"status": "skipped", "reason": "run no longer waiting", "run_id": run_id}

    # Reload with relationships
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        return None

    thread = run.thread
    agent = run.agent
    owner_id = agent.owner_id
    message_id = run.assistant_message_id

    # Get tool_call_id from WorkerJob
    tool_call_id = None
    if job_id:
        job = db.query(WorkerJob).filter(WorkerJob.id == job_id).first()
        if job:
            tool_call_id = job.tool_call_id

    if not tool_call_id:
        # Fallback: find most recent pending worker job for this run
        job = (
            db.query(WorkerJob)
            .filter(
                WorkerJob.supervisor_run_id == run_id,
                WorkerJob.tool_call_id.isnot(None),
            )
            .order_by(WorkerJob.created_at.desc())
            .first()
        )
        if job:
            tool_call_id = job.tool_call_id

    if not tool_call_id:
        logger.error("Cannot resume run %s: no tool_call_id found", run_id)
        error_msg = "No tool_call_id found for worker resume"
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.FAILED
        run.error = error_msg
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        # Emit error events for UI consistency
        await emit_run_event(
            db=db,
            run_id=run.id,
            event_type="error",
            payload={
                "thread_id": thread.id,
                "message": error_msg,
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
                "error": error_msg,
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        return {"status": "error", "error": error_msg}

    logger.info(
        "Resuming supervisor run %s (thread=%s) with tool_call_id=%s [LangGraph-free]",
        run_id,
        thread.id,
        tool_call_id,
    )

    # Emit "resumed" event
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

    # Set up contexts
    _supervisor_ctx_token = set_supervisor_context(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
    )

    _supervisor_emitter = SupervisorEmitter(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
    )
    _emitter_token = set_emitter(_supervisor_emitter)
    _user_ctx_token = set_current_user_id(owner_id)

    try:
        # Create AgentRunner and run continuation
        runner = AgentRunner(agent)
        created_rows = await runner.run_continuation(
            db=db,
            thread=thread,
            tool_call_id=tool_call_id,
            tool_result=worker_result,
            run_id=run_id,
        )

        # Normal completion
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.SUCCESS
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        # Token usage (use `is not None` to record even 0 tokens)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
        db.commit()

        # Extract final response
        final_response = None
        for row in reversed(created_rows):
            if row.role == "assistant" and row.content:
                final_response = row.content
                break

        # Emit completion events
        usage_payload = {
            "prompt_tokens": runner.usage_prompt_tokens,
            "completion_tokens": runner.usage_completion_tokens,
            "total_tokens": runner.usage_total_tokens,
            "reasoning_tokens": runner.usage_reasoning_tokens,
        }

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
        logger.info("Successfully resumed supervisor run %s [LangGraph-free]", run_id)
        return {"status": "success", "result": final_response}

    except AgentInterrupted as e:
        # Supervisor spawned another worker - set back to WAITING
        interrupt_value = e.interrupt_value
        job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
        interrupt_message = "Working on this in the background..."

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

        logger.info(
            "Supervisor run %s interrupted again (WAITING for worker job %s) [LangGraph-free]",
            run_id,
            job_id,
        )
        return {"status": "waiting", "run_id": run_id, "job_id": job_id, "message": interrupt_message}

    except Exception as e:
        logger.exception("Failed to resume supervisor run %s [LangGraph-free]: %s", run_id, e)

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
        reset_supervisor_context(_supervisor_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)


# ---------------------------------------------------------------------------
# LangGraph-based continuation (legacy path)
# ---------------------------------------------------------------------------


async def _resume_supervisor_langgraph(
    db: Session,
    run_id: int,
    worker_result: str,
) -> dict[str, Any] | None:
    """Resume supervisor using LangGraph's Command(resume=...) pattern.

    This is the legacy path that uses LangGraph checkpointing.
    """
    # This is the original implementation - just renamed
    from zerg.agents_def import zerg_react_agent
    from zerg.agents_def.zerg_react_agent import get_llm_usage
    from zerg.agents_def.zerg_react_agent import reset_llm_usage
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import reset_current_thread_id
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
    updated = (
        db.query(AgentRun).filter(AgentRun.id == run_id, AgentRun.status == RunStatus.WAITING).update({AgentRun.status: RunStatus.RUNNING})
    )
    db.commit()
    if updated == 0:
        return {"status": "skipped", "reason": "run no longer waiting", "run_id": run_id}

    # Re-load with relationships after the status transition
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        return None

    thread = run.thread
    agent = run.agent
    owner_id = agent.owner_id
    message_id = run.assistant_message_id

    logger.info("Resuming supervisor run %s (thread=%s) [LangGraph]", run_id, thread.id)

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
    _supervisor_ctx_token = set_supervisor_context(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
    )

    _supervisor_emitter = SupervisorEmitter(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
    )
    _emitter_token = set_emitter(_supervisor_emitter)

    _user_ctx_token = set_current_user_id(owner_id)
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

        # Check if the last message is an AIMessage with pending tool calls.
        from langchain_core.messages import AIMessage
        from langchain_core.messages import ToolMessage

        last_conv_msg = conversation_msgs[-1] if conversation_msgs else None
        use_fresh_messages = False

        if isinstance(last_conv_msg, AIMessage) and last_conv_msg.tool_calls:
            tool_call_ids = {tc["id"] for tc in last_conv_msg.tool_calls}
            responded_ids = {m.tool_call_id for m in conversation_msgs if isinstance(m, ToolMessage)}
            pending_ids = tool_call_ids - responded_ids

            if pending_ids:
                logger.info("[RESUME] Found pending tool calls, using fresh messages path")
                tool_call_id = list(pending_ids)[0]
                tool_msg = ToolMessage(
                    content=f"Worker completed:\n\n{worker_result}",
                    tool_call_id=tool_call_id,
                    name="spawn_worker",
                )
                thread_service.save_new_messages(
                    db,
                    thread_id=thread.id,
                    messages=[tool_msg],
                    processed=True,
                )
                conversation_msgs = conversation_msgs + [tool_msg]
                use_fresh_messages = True

        runnable = zerg_react_agent.get_runnable(agent)
        config = {"configurable": {"thread_id": str(thread.id)}}
        reset_llm_usage()

        if use_fresh_messages:
            db_messages = thread_service.get_thread_messages_as_langchain(db, thread.id)
            result = await runnable.ainvoke(db_messages, config)
        else:
            result = await runnable.ainvoke(Command(resume=worker_result), config)

        # Handle interrupt
        if isinstance(result, dict) and result.get("__interrupt__"):
            interrupt_value = _extract_interrupt_value(result)
            interrupt_message = "Working on this in the background..."
            job_id = None
            if isinstance(interrupt_value, dict):
                job_id = interrupt_value.get("job_id")
                interrupt_message = interrupt_value.get("message") or interrupt_message
            elif interrupt_value is not None:
                interrupt_message = str(interrupt_value)

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

            logger.info("Supervisor run %s interrupted again (WAITING for worker job %s) [LangGraph]", run_id, job_id)
            return {"status": "waiting", "run_id": run_id, "job_id": job_id, "message": interrupt_message}

        # Normal completion
        if isinstance(result, dict):
            messages = result.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError(f"Unexpected resume result dict (no messages): {list(result.keys())}")
        else:
            messages = result

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

        final_response = None
        for row in reversed(created_rows):
            if row.role == "assistant" and row.content:
                final_response = row.content
                break

        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)
        run.status = RunStatus.SUCCESS
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, int):
            run.total_tokens = (run.total_tokens or 0) + total_tokens
        db.commit()

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
        logger.info("Successfully resumed and completed supervisor run %s [LangGraph]", run_id)
        return {"status": "success", "result": final_response}

    except Exception as e:
        logger.exception("Failed to resume supervisor run %s [LangGraph]: %s", run_id, e)

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
        reset_supervisor_context(_supervisor_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)
        reset_current_thread_id(_thread_ctx_token)
        reset_credential_resolver(_cred_ctx_token)


# ---------------------------------------------------------------------------
# Public API - dispatches to appropriate implementation
# ---------------------------------------------------------------------------


async def resume_supervisor_with_worker_result(
    db: Session,
    run_id: int,
    worker_result: str,
    job_id: int | None = None,
) -> dict[str, Any] | None:
    """Resume an interrupted supervisor run with a worker result.

    Dispatches to the appropriate implementation based on USE_LANGGRAPH_SUPERVISOR flag:
    - Default (flag=0): Uses LangGraph-free AgentRunner.run_continuation()
    - Legacy (flag=1): Uses LangGraph Command(resume=...) pattern

    Args:
        db: Database session.
        run_id: AgentRun ID to resume.
        worker_result: Worker's result string.
        job_id: Optional WorkerJob ID to look up tool_call_id (used by LangGraph-free path).

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    if USE_LANGGRAPH_SUPERVISOR:
        logger.info("Using LangGraph resume path for run %s (USE_LANGGRAPH_SUPERVISOR=1)", run_id)
        return await _resume_supervisor_langgraph(db, run_id, worker_result)
    else:
        logger.info("Using LangGraph-free resume path for run %s", run_id)
        return await _continue_supervisor_langgraph_free(db, run_id, worker_result, job_id)


def _compute_duration_ms(started_at, *, end_time: datetime | None = None) -> int:
    if started_at is None:
        return 0
    end_dt = end_time or datetime.now(timezone.utc)
    try:
        started_dt = started_at.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    return max(0, int((end_dt - started_dt).total_seconds() * 1000))
