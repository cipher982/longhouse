"""Batch commis resume flow extracted from commis_resume."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob
from zerg.models.enums import RunStatus
from zerg.models.models import CommisJob
from zerg.models.models import Run
from zerg.services.commis_runner import RunnerFactory
from zerg.services.commis_runner import default_runner_factory as _default_runner_factory
from zerg.services.oikos_context import reset_seq
from zerg.services.oikos_run_lifecycle import emit_error_event_and_close_stream
from zerg.services.oikos_run_lifecycle import emit_failed_run_updated
from zerg.services.oikos_run_lifecycle import emit_oikos_complete_success
from zerg.services.oikos_run_lifecycle import emit_oikos_waiting_and_run_updated
from zerg.services.oikos_run_lifecycle import emit_stream_control_for_pending_commiss
from zerg.services.oikos_run_lifecycle import emit_success_run_updated

logger = logging.getLogger(__name__)


async def resume_oikos_batch(
    db: Session,
    run_id: int,
    commis_results: list[dict[str, Any]],
    *,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> dict[str, Any] | None:
    """Resume oikos with ALL commis results (batch continuation).

    Called by check_and_resume_if_all_complete when all commis are done.
    Creates ToolMessages for each commis result and resumes oikos.

    Args:
        db: Database session.
        run_id: Run ID to resume.
        commis_results: List of dicts with tool_call_id, result, error, status.
        runner_factory: Callable that creates a ContinuationRunner from
            (fiche, model_override, reasoning_effort). Defaults to FicheRunner.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.events import OikosEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.managers.fiche_runner import FicheInterrupted
    from zerg.services.event_store import emit_run_event
    from zerg.services.oikos_context import reset_oikos_context
    from zerg.services.oikos_context import set_oikos_context

    # Load run
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        logger.error("Cannot batch resume: run %s not found", run_id)
        return None

    if run.status != RunStatus.WAITING:
        logger.info("Skipping batch resume: run %s is %s, not WAITING", run_id, run.status.value)
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
    trace_id = str(run.trace_id) if run.trace_id else None

    logger.info(
        "Batch resuming oikos run %s (thread=%s) with %s commis results",
        run_id,
        thread.id,
        len(commis_results),
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
            "batch_size": len(commis_results),
            "trace_id": trace_id,
        },
    )

    # Set up contexts
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
        # Create runner via factory and run batch continuation
        runner = runner_factory(
            fiche,
            model_override=run.model,
            reasoning_effort=run.reasoning_effort,
        )
        created_rows = await runner.run_batch_continuation(
            db=db,
            thread=thread,
            commis_results=commis_results,
            run_id=run_id,
            trace_id=trace_id,
        )

        # Normal completion
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.SUCCESS
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens

        # Mark barrier as completed
        barrier = db.query(CommisBarrier).filter(CommisBarrier.run_id == run_id).first()
        if barrier:
            barrier.status = "completed"
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
            batch_size=len(commis_results),
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

        # Auto-summary -> Memory Files (async, best-effort)
        from zerg.models.models import ThreadMessage
        from zerg.services.memory_summarizer import schedule_run_summary

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
        logger.info("Successfully batch resumed oikos run %s", run_id)
        return {"status": "success", "result": final_response}

    except FicheInterrupted as e:
        # Oikos spawned more commis - set back to WAITING and reuse/reset barrier
        interrupt_value = e.interrupt_value
        interrupt_message = "Working on more tasks in the background..."

        # Handle parallel commis (commis_pending) or single commis
        if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "commis_pending":
            job_ids = interrupt_value.get("job_ids", [])
            created_jobs = interrupt_value.get("created_jobs", [])

            logger.info(f"Batch re-interrupt: resetting barrier for {len(job_ids)} new commis")

            # Reuse existing barrier (unique constraint on run_id)
            barrier = db.query(CommisBarrier).filter(CommisBarrier.run_id == run_id).first()
            if barrier:
                # Prune old BarrierJobs to prevent stale data in resume
                # (old completed jobs would pollute commis_results)
                db.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).delete()
                logger.debug(f"Pruned old BarrierJobs for barrier {barrier.id}")

                # Reset barrier for new batch
                barrier.status = "waiting"
                barrier.expected_count = len(job_ids)
                barrier.completed_count = 0
                barrier.deadline_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)

                # Create new BarrierJobs for the new commis
                for job_info in created_jobs:
                    job = job_info["job"]
                    tool_call_id = job_info["tool_call_id"]
                    db.add(
                        CommisBarrierJob(
                            barrier_id=barrier.id,
                            job_id=job.id,
                            tool_call_id=tool_call_id,
                            status="queued",
                        )
                    )

                # Flip new jobs from 'created' to 'queued'
                for job_id in job_ids:
                    db.query(CommisJob).filter(
                        CommisJob.id == job_id,
                        CommisJob.status == "created",
                    ).update({"status": "queued"})

                logger.info(f"Barrier {barrier.id} reset: {len(job_ids)} new jobs queued")
            else:
                logger.warning(f"No existing barrier for run {run_id} - creating new one")
                # This shouldn't happen, but handle gracefully
                barrier = CommisBarrier(
                    run_id=run_id,
                    expected_count=len(job_ids),
                    status="waiting",
                    deadline_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10),
                )
                db.add(barrier)
                db.flush()

                for job_info in created_jobs:
                    job = job_info["job"]
                    tool_call_id = job_info["tool_call_id"]
                    db.add(
                        CommisBarrierJob(
                            barrier_id=barrier.id,
                            job_id=job.id,
                            tool_call_id=tool_call_id,
                            status="queued",
                        )
                    )

                for job_id in job_ids:
                    db.query(CommisJob).filter(
                        CommisJob.id == job_id,
                        CommisJob.status == "created",
                    ).update({"status": "queued"})

            # Emit commis_spawned events for UI (after jobs are queued)
            from zerg.services.event_store import append_run_event

            for job_info in created_jobs:
                job = job_info["job"]
                tool_call_id = job_info["tool_call_id"]
                task = job.task[:100] if job.task else ""
                await append_run_event(
                    run_id=run_id,
                    event_type="commis_spawned",
                    payload={
                        "job_id": job.id,
                        "tool_call_id": tool_call_id,
                        "task": task,
                        "model": job.model,
                        "owner_id": owner_id,
                        "trace_id": trace_id,
                    },
                )
            logger.info(f"Batch re-interrupt: emitted {len(created_jobs)} commis_spawned events")

            interrupt_message = f"Working on {len(job_ids)} more tasks in the background..."
        else:
            # Single commis path
            job_ids = interrupt_value.get("job_ids") if isinstance(interrupt_value, dict) else None

        # Update run status to WAITING (atomic with barrier reset above)
        run.status = RunStatus.WAITING
        run.duration_ms = _compute_duration_ms(run.started_at)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
        db.commit()  # Single commit: WAITING + barrier reset

        await emit_oikos_waiting_and_run_updated(
            db=db,
            run_id=run.id,
            fiche_id=fiche.id,
            thread_id=thread.id,
            owner_id=owner_id,
            message_id=message_id,
            message=interrupt_message,
            trace_id=trace_id,
            job_ids=job_ids,
        )

        logger.info(
            "Batch resume interrupted: run %s waiting for more commis %s",
            run_id,
            job_ids,
        )
        return {"status": "waiting", "run_id": run_id, "job_ids": job_ids, "message": interrupt_message}

    except Exception as e:
        logger.exception("Failed to batch resume oikos run %s: %s", run_id, e)

        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = RunStatus.FAILED
        run.error = str(e)
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        # Mark barrier as failed (prevents stuck "resuming" state)
        barrier = db.query(CommisBarrier).filter(CommisBarrier.run_id == run_id).first()
        if barrier:
            barrier.status = "failed"

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


def _compute_duration_ms(started_at, *, end_time: datetime | None = None) -> int:
    if started_at is None:
        return 0
    end_dt = end_time or datetime.now(timezone.utc)
    try:
        started_dt = started_at.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    return max(0, int((end_dt - started_dt).total_seconds() * 1000))
