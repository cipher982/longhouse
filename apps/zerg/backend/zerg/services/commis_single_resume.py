"""Single-commis continuation flow."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.models import CommisJob
from zerg.models.models import Run
from zerg.services.commis_runner import RunnerFactory
from zerg.services.commis_timing import compute_duration_ms
from zerg.services.oikos_context import reset_seq
from zerg.services.oikos_run_lifecycle import emit_error_event_and_close_stream
from zerg.services.oikos_run_lifecycle import emit_failed_run_updated
from zerg.services.oikos_run_lifecycle import emit_oikos_complete_success
from zerg.services.oikos_run_lifecycle import emit_oikos_waiting_and_run_updated
from zerg.services.oikos_run_lifecycle import emit_stream_control_for_pending_commiss
from zerg.services.oikos_run_lifecycle import emit_success_run_updated

logger = logging.getLogger(__name__)


async def continue_oikos_langgraph_free(
    db: Session,
    run_id: int,
    commis_result: str,
    job_id: int | None = None,
    *,
    runner_factory: RunnerFactory,
) -> dict[str, Any] | None:
    """Resume oikos using a generic continuation runner (LangGraph-free)."""
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.events import OikosEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.managers.fiche_runner import FicheInterrupted
    from zerg.models.models import ThreadMessage
    from zerg.services.event_store import emit_run_event
    from zerg.services.memory_summarizer import schedule_run_summary
    from zerg.services.oikos_context import reset_oikos_context
    from zerg.services.oikos_context import set_oikos_context

    # Load run
    run = db.query(Run).filter(Run.id == run_id).first()
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
    updated = db.query(Run).filter(Run.id == run_id, Run.status == RunStatus.WAITING).update({Run.status: RunStatus.RUNNING})
    db.commit()
    if updated == 0:
        return {"status": "skipped", "reason": "run no longer waiting", "run_id": run_id}

    # Reload with relationships
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        return None

    thread = run.thread
    fiche = run.fiche
    owner_id = fiche.owner_id
    message_id = run.assistant_message_id
    # Inherit trace_id from the original run for end-to-end tracing
    trace_id = str(run.trace_id) if run.trace_id else None

    # Get tool_call_id - priority order:
    # 1. pending_tool_call_id (from wait_for_commis) - highest priority
    # 2. CommisJob.tool_call_id (from job_id parameter)
    # 3. Most recent pending commis job for this run
    tool_call_id = None

    # Priority 1: Check pending_tool_call_id first (from wait_for_commis)
    if run.pending_tool_call_id:
        tool_call_id = run.pending_tool_call_id
        # Clear pending_tool_call_id (one-time use)
        run.pending_tool_call_id = None
        db.commit()
        logger.info(f"Using pending_tool_call_id={tool_call_id} from wait_for_commis")

    # Priority 2: Get from CommisJob if job_id provided
    if not tool_call_id and job_id:
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        if job:
            tool_call_id = job.tool_call_id

    # Priority 3: Fallback - find most recent pending commis job for this run
    if not tool_call_id:
        job = (
            db.query(CommisJob)
            .filter(
                CommisJob.oikos_run_id == run_id,
                CommisJob.tool_call_id.isnot(None),
            )
            .order_by(CommisJob.created_at.desc())
            .first()
        )
        if job:
            tool_call_id = job.tool_call_id

    if not tool_call_id:
        logger.error("Cannot resume run %s: no tool_call_id found", run_id)
        error_msg = "No tool_call_id found for commis resume"
        end_time = datetime.now(timezone.utc)
        duration_ms = compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.FAILED
        run.error = error_msg
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        # Emit error events for UI consistency
        await emit_error_event_and_close_stream(
            db=db,
            run=run,
            thread_id=thread.id,
            owner_id=owner_id,
            message=error_msg,
            trace_id=trace_id,
        )

        await emit_failed_run_updated(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            finished_at_iso=end_time.isoformat(),
            duration_ms=duration_ms,
            error=error_msg,
        )

        return {"status": "error", "error": error_msg}

    logger.info(
        "Resuming oikos run %s (thread=%s) with tool_call_id=%s [LangGraph-free]",
        run_id,
        thread.id,
        tool_call_id,
    )

    # Emit "resumed" event
    await emit_run_event(
        db=db,
        run_id=run.id,
        event_type="oikos_resumed",
        payload={
            "fiche_id": fiche.id,
            "thread_id": thread.id,
            "message_id": message_id,
            "owner_id": owner_id,
            "trace_id": trace_id,
        },
    )

    # Set up contexts (include trace_id for end-to-end tracing)
    # Include model/reasoning_effort so commis inherit oikos settings
    _oikos_ctx_token = set_oikos_context(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
    )

    _oikos_emitter = OikosEmitter(
        run_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
    )
    _emitter_token = set_emitter(_oikos_emitter)
    _user_ctx_token = set_current_user_id(owner_id)

    try:
        # Create runner via factory and run continuation
        # Inherit model/reasoning_effort from the original run for consistency
        runner = runner_factory(
            fiche,
            model_override=run.model,
            reasoning_effort=run.reasoning_effort,
        )
        created_rows = await runner.run_continuation(
            db=db,
            thread=thread,
            tool_call_id=tool_call_id,
            tool_result=commis_result,
            run_id=run_id,
            trace_id=trace_id,
        )

        # Normal completion
        end_time = datetime.now(timezone.utc)
        duration_ms = compute_duration_ms(run.started_at, end_time=end_time)

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

        await emit_oikos_complete_success(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            message_id=message_id,
            result=final_response or "(No result)",
            duration_ms=duration_ms,
            debug_url=f"/oikos/{run.id}",
            usage=usage_payload,
            trace_id=trace_id,
        )

        # Emit stream_control based on pending commiss
        await emit_stream_control_for_pending_commiss(db, run, owner_id, ttl_ms=120_000)

        await emit_success_run_updated(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            finished_at_iso=end_time.isoformat(),
            duration_ms=duration_ms,
        )

        task_row = (
            db.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "user",
                ThreadMessage.internal.is_(False),
            )
            .order_by(ThreadMessage.sent_at.desc())
            .first()
        )
        task_text = task_row.content if task_row else ""
        schedule_run_summary(
            owner_id=owner_id,
            thread_id=thread.id,
            run_id=run.id,
            task=task_text or "",
            result_text=final_response or "",
            trace_id=str(run.trace_id) if run.trace_id else None,
        )

        reset_seq(run.id)
        logger.info("Successfully resumed oikos run %s", run_id)
        return {"status": "success", "result": final_response}

    except FicheInterrupted as e:
        # Oikos spawned another commis - set back to WAITING
        interrupt_value = e.interrupt_value
        job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
        interrupt_message = "Working on this in the background..."

        run.status = RunStatus.WAITING
        run.duration_ms = compute_duration_ms(run.started_at)
        # Persist partial token usage (will be added to on next resume)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
        db.commit()

        await emit_oikos_waiting_and_run_updated(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            message_id=message_id,
            message=interrupt_message,
            trace_id=trace_id,
            job_id=job_id,
        )

        logger.info(
            "Oikos run %s interrupted again (WAITING for commis job %s) [LangGraph-free]",
            run_id,
            job_id,
        )
        return {"status": "waiting", "run_id": run_id, "job_id": job_id, "message": interrupt_message}

    except Exception as e:
        logger.exception("Failed to resume oikos run %s [LangGraph-free]: %s", run_id, e)

        end_time = datetime.now(timezone.utc)
        duration_ms = compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        await emit_error_event_and_close_stream(
            db=db,
            run=run,
            thread_id=thread.id,
            owner_id=owner_id,
            message=str(e),
            trace_id=trace_id,
        )

        await emit_failed_run_updated(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            finished_at_iso=end_time.isoformat(),
            duration_ms=duration_ms,
            error=str(e),
        )

        reset_seq(run.id)

        return {"status": "error", "error": str(e)}

    finally:
        reset_oikos_context(_oikos_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)


async def resume_oikos_with_commis_result(
    db: Session,
    run_id: int,
    commis_result: str,
    job_id: int | None = None,
    *,
    runner_factory: RunnerFactory,
) -> dict[str, Any] | None:
    """Resume an interrupted oikos run with a commis result."""
    return await continue_oikos_langgraph_free(
        db=db,
        run_id=run_id,
        commis_result=commis_result,
        job_id=job_id,
        runner_factory=runner_factory,
    )
