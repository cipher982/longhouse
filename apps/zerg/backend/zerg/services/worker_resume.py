"""Worker resume handler - resumes interrupted supervisor after worker completion.

Uses AgentRunner.run_continuation() for DB-based resume.

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

from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.services.supervisor_context import reset_seq

logger = logging.getLogger(__name__)


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
    # Inherit trace_id from the original run for end-to-end tracing
    trace_id = str(run.trace_id) if run.trace_id else None

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

    # Set up contexts (include trace_id for end-to-end tracing)
    _supervisor_ctx_token = set_supervisor_context(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
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
        # Inherit model/reasoning_effort from the original run for consistency
        runner = AgentRunner(
            agent,
            model_override=run.model,
            reasoning_effort=run.reasoning_effort,
        )
        created_rows = await runner.run_continuation(
            db=db,
            thread=thread,
            tool_call_id=tool_call_id,
            tool_result=worker_result,
            run_id=run_id,
            trace_id=trace_id,
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
        logger.info("Successfully resumed supervisor run %s", run_id)
        return {"status": "success", "result": final_response}

    except AgentInterrupted as e:
        # Supervisor spawned another worker - set back to WAITING
        interrupt_value = e.interrupt_value
        job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
        interrupt_message = "Working on this in the background..."

        run.status = RunStatus.WAITING
        run.duration_ms = _compute_duration_ms(run.started_at)
        # Persist partial token usage (will be added to on next resume)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
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
        return {"status": "error", "error": str(e)}

    finally:
        reset_supervisor_context(_supervisor_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)


async def resume_supervisor_with_worker_result(
    db: Session,
    run_id: int,
    worker_result: str,
    job_id: int | None = None,
) -> dict[str, Any] | None:
    """Resume an interrupted supervisor run with a worker result.

    Uses AgentRunner.run_continuation() for DB-based resume.

    Args:
        db: Database session.
        run_id: AgentRun ID to resume.
        worker_result: Worker's result string.
        job_id: Optional WorkerJob ID to look up tool_call_id.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
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
