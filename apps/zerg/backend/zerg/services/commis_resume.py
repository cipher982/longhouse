"""Commis resume handler - resumes interrupted concierge after commis completion.

Uses FicheRunner.run_continuation() for DB-based resume.

Key requirements:
- Idempotent: multiple callers must not resume the same run twice.
- Durable: resumed execution persists new messages to the thread.
- Interrupt-safe: concierge may interrupt again (multiple commis sequentially).
- Barrier-safe: parallel commis coordinate via barrier pattern (single resume trigger).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob
from zerg.models.enums import CourseStatus
from zerg.models.models import CommisJob
from zerg.models.models import Course
from zerg.services.concierge_context import reset_seq

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Barrier-Based Resume (Parallel Commis Coordination)
# ---------------------------------------------------------------------------


async def check_and_resume_if_all_complete(
    db: Session,
    course_id: int,
    job_id: int,
    result: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Atomic barrier check. Only ONE commis triggers resume.

    Uses SELECT FOR UPDATE + status guard in single transaction to prevent
    the double-resume race condition where multiple commis completing
    simultaneously might both try to resume the concierge.

    Args:
        db: Database session.
        course_id: Concierge run ID (CommisBarrier.course_id).
        job_id: Commis job ID that just completed.
        result: Commis result string.
        error: Optional error message if commis failed.

    Returns:
        Dict with status and details:
        - {"status": "resume", "commis_results": [...]} if this commis triggers resume
        - {"status": "waiting", "completed": N, "expected": M} if not all complete
        - {"status": "skipped", "reason": "..."} if barrier not waiting or already done
    """
    try:
        # Use a transaction context for atomic operations
        # Note: SQLAlchemy's begin() creates a subtransaction if already in one
        with db.begin_nested():
            # 1. Lock the barrier row with FOR UPDATE
            barrier = db.query(CommisBarrier).filter(CommisBarrier.course_id == course_id).with_for_update().first()

            if not barrier:
                logger.warning("No barrier found for course_id=%s", course_id)
                return {"status": "skipped", "reason": "no barrier found"}

            if barrier.status != "waiting":
                logger.info("Barrier for run %s is %s, not waiting", course_id, barrier.status)
                return {"status": "skipped", "reason": f"barrier is {barrier.status}, not waiting"}

            # 2. Update the specific CommisBarrierJob
            barrier_job = (
                db.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id, CommisBarrierJob.job_id == job_id).first()
            )

            if not barrier_job:
                logger.warning("No CommisBarrierJob found for barrier_id=%s, job_id=%s", barrier.id, job_id)
                return {"status": "skipped", "reason": "no barrier job found"}

            if barrier_job.status in ["completed", "failed"]:
                logger.info("CommisBarrierJob %s already %s", barrier_job.id, barrier_job.status)
                return {"status": "skipped", "reason": f"barrier job already {barrier_job.status}"}

            # Update barrier job
            barrier_job.status = "failed" if error else "completed"
            barrier_job.result = result
            barrier_job.error = error
            barrier_job.completed_at = sa_func.now()

            # 3. Increment counter atomically
            barrier.completed_count += 1

            logger.info(
                "Barrier for run %s: %s/%s complete (job %s %s)",
                course_id,
                barrier.completed_count,
                barrier.expected_count,
                job_id,
                "failed" if error else "completed",
            )

            # 4. Check if ALL complete AND claim resume atomically
            if barrier.completed_count >= barrier.expected_count:
                # Claim resume (this prevents double-resume)
                barrier.status = "resuming"
                db.flush()  # Persist within transaction

                # 5. Collect all results for batch resume
                all_jobs = db.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).all()
                commis_results = [
                    {
                        "tool_call_id": j.tool_call_id,
                        "result": j.result,
                        "error": j.error,
                        "status": j.status,
                        "job_id": j.job_id,
                    }
                    for j in all_jobs
                ]

                logger.info(
                    "Barrier for run %s complete! Triggering batch resume with %s results",
                    course_id,
                    len(commis_results),
                )

                # Transaction commits at end of `with db.begin_nested()`
                return {"status": "resume", "commis_results": commis_results}

            # Not all complete yet - just commit the update
            return {
                "status": "waiting",
                "completed": barrier.completed_count,
                "expected": barrier.expected_count,
            }

    except Exception as e:
        logger.exception("Error in check_and_resume_if_all_complete for run %s: %s", course_id, e)
        # Let the caller handle the exception
        raise


async def resume_concierge_batch(
    db: Session,
    course_id: int,
    commis_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resume concierge with ALL commis results (batch continuation).

    Called by check_and_resume_if_all_complete when all commis are done.
    Creates ToolMessages for each commis result and resumes concierge.

    Args:
        db: Database session.
        course_id: Course ID to resume.
        commis_results: List of dicts with tool_call_id, result, error, status.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.events import ConciergeEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.managers.fiche_runner import CourseInterrupted
    from zerg.managers.fiche_runner import FicheRunner
    from zerg.services.concierge_context import reset_concierge_context
    from zerg.services.concierge_context import set_concierge_context
    from zerg.services.event_store import emit_course_event

    # Load run
    run = db.query(Course).filter(Course.id == course_id).first()
    if not run:
        logger.error("Cannot batch resume: run %s not found", course_id)
        return None

    if run.status != CourseStatus.WAITING:
        logger.info("Skipping batch resume: run %s is %s, not WAITING", course_id, run.status.value)
        return {"status": "skipped", "reason": f"run is {run.status.value}, not waiting", "course_id": course_id}

    # Ensure stable message_id
    if not run.assistant_message_id:
        run.assistant_message_id = str(uuid.uuid4())
        db.commit()

    # Idempotency gate: WAITING → RUNNING atomically
    updated = (
        db.query(Course).filter(Course.id == course_id, Course.status == CourseStatus.WAITING).update({Course.status: CourseStatus.RUNNING})
    )
    db.commit()
    if updated == 0:
        return {"status": "skipped", "reason": "run no longer waiting", "course_id": course_id}

    # Reload with relationships
    run = db.query(Course).filter(Course.id == course_id).first()
    if not run:
        return None

    thread = run.thread
    fiche = run.fiche
    owner_id = fiche.owner_id
    message_id = run.assistant_message_id
    trace_id = str(run.trace_id) if run.trace_id else None

    logger.info(
        "Batch resuming concierge run %s (thread=%s) with %s commis results",
        course_id,
        thread.id,
        len(commis_results),
    )

    # Emit "resumed" event
    await emit_course_event(
        db=db,
        course_id=run.id,
        event_type="concierge_resumed",
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
    _concierge_ctx_token = set_concierge_context(
        course_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
    )

    _concierge_emitter = ConciergeEmitter(
        course_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
    )
    _emitter_token = set_emitter(_concierge_emitter)
    _user_ctx_token = set_current_user_id(owner_id)

    try:
        # Create FicheRunner and run batch continuation
        runner = FicheRunner(
            fiche,
            model_override=run.model,
            reasoning_effort=run.reasoning_effort,
        )
        created_rows = await runner.run_batch_continuation(
            db=db,
            thread=thread,
            commis_results=commis_results,
            course_id=course_id,
            trace_id=trace_id,
        )

        # Normal completion
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = CourseStatus.SUCCESS
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens

        # Mark barrier as completed
        barrier = db.query(CommisBarrier).filter(CommisBarrier.course_id == course_id).first()
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

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="concierge_complete",
            payload={
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "result": final_response or "(No result)",
                "status": "success",
                "duration_ms": duration_ms,
                "debug_url": f"/concierge/{run.id}",
                "owner_id": owner_id,
                "message_id": message_id,
                "usage": usage_payload,
                "batch_size": len(commis_results),
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
                "status": "success",
                "finished_at": end_time.isoformat(),
                "duration_ms": duration_ms,
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        # Auto-summary -> Memory Files (async, best-effort)
        from zerg.models.models import ThreadMessage
        from zerg.services.memory_summarizer import schedule_course_summary

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
        schedule_course_summary(
            owner_id=owner_id,
            thread_id=thread.id,
            course_id=run.id,
            task=task_text or "",
            result_text=final_response or "",
            trace_id=str(run.trace_id) if run.trace_id else None,
        )

        reset_seq(run.id)
        logger.info("Successfully batch resumed concierge run %s", course_id)
        return {"status": "success", "result": final_response}

    except CourseInterrupted as e:
        # Concierge spawned more commis - set back to WAITING and reuse/reset barrier
        interrupt_value = e.interrupt_value
        interrupt_message = "Working on more tasks in the background..."

        # Handle parallel commis (commis_pending) or single commis
        if isinstance(interrupt_value, dict) and interrupt_value.get("type") == "commis_pending":
            job_ids = interrupt_value.get("job_ids", [])
            created_jobs = interrupt_value.get("created_jobs", [])

            logger.info(f"Batch re-interrupt: resetting barrier for {len(job_ids)} new commis")

            # Reuse existing barrier (unique constraint on course_id)
            barrier = db.query(CommisBarrier).filter(CommisBarrier.course_id == course_id).first()
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
                logger.warning(f"No existing barrier for run {course_id} - creating new one")
                # This shouldn't happen, but handle gracefully
                barrier = CommisBarrier(
                    course_id=course_id,
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
            from zerg.services.event_store import append_course_event

            for job_info in created_jobs:
                job = job_info["job"]
                tool_call_id = job_info["tool_call_id"]
                task = job.task[:100] if job.task else ""
                await append_course_event(
                    course_id=course_id,
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
        run.status = CourseStatus.WAITING
        run.duration_ms = _compute_duration_ms(run.started_at)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
        db.commit()  # Single commit: WAITING + barrier reset

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="concierge_waiting",
            payload={
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "job_ids": job_ids,
                "message": interrupt_message,
                "owner_id": owner_id,
                "message_id": message_id,
                "close_stream": False,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
                "status": "waiting",
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        logger.info(
            "Batch resume interrupted: run %s waiting for more commis %s",
            course_id,
            job_ids,
        )
        return {"status": "waiting", "course_id": course_id, "job_ids": job_ids, "message": interrupt_message}

    except Exception as e:
        logger.exception("Failed to batch resume concierge run %s: %s", course_id, e)

        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = CourseStatus.FAILED
        run.error = str(e)
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms

        # Mark barrier as failed (prevents stuck "resuming" state)
        barrier = db.query(CommisBarrier).filter(CommisBarrier.course_id == course_id).first()
        if barrier:
            barrier.status = "failed"

        db.commit()

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="error",
            payload={
                "thread_id": thread.id,
                "message": str(e),
                "status": "error",
                "owner_id": owner_id,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
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
        reset_concierge_context(_concierge_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)


# ---------------------------------------------------------------------------
# LangGraph-free continuation (new path)
# ---------------------------------------------------------------------------


async def _continue_concierge_langgraph_free(
    db: Session,
    course_id: int,
    commis_result: str,
    job_id: int | None = None,
) -> dict[str, Any] | None:
    """Resume concierge using the LangGraph-free FicheRunner.run_continuation().

    This is the new path that doesn't rely on LangGraph checkpointing.

    Args:
        db: Database session.
        course_id: Course ID to resume.
        commis_result: Commis's result string.
        job_id: Optional CommisJob ID to look up tool_call_id.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    from zerg.callbacks.token_stream import current_user_id_var
    from zerg.callbacks.token_stream import set_current_user_id
    from zerg.events import ConciergeEmitter
    from zerg.events import reset_emitter
    from zerg.events import set_emitter
    from zerg.managers.fiche_runner import CourseInterrupted
    from zerg.managers.fiche_runner import FicheRunner
    from zerg.services.concierge_context import reset_concierge_context
    from zerg.services.concierge_context import set_concierge_context
    from zerg.services.event_store import emit_course_event

    # Load run
    run = db.query(Course).filter(Course.id == course_id).first()
    if not run:
        logger.error("Cannot resume: run %s not found", course_id)
        return None

    if run.status != CourseStatus.WAITING:
        logger.info("Skipping resume: run %s is %s, not WAITING", course_id, run.status.value)
        return {"status": "skipped", "reason": f"run is {run.status.value}, not waiting", "course_id": course_id}

    # Ensure stable message_id
    if not run.assistant_message_id:
        run.assistant_message_id = str(uuid.uuid4())
        db.commit()

    # Idempotency gate: WAITING → RUNNING atomically
    updated = (
        db.query(Course).filter(Course.id == course_id, Course.status == CourseStatus.WAITING).update({Course.status: CourseStatus.RUNNING})
    )
    db.commit()
    if updated == 0:
        return {"status": "skipped", "reason": "run no longer waiting", "course_id": course_id}

    # Reload with relationships
    run = db.query(Course).filter(Course.id == course_id).first()
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
                CommisJob.concierge_course_id == course_id,
                CommisJob.tool_call_id.isnot(None),
            )
            .order_by(CommisJob.created_at.desc())
            .first()
        )
        if job:
            tool_call_id = job.tool_call_id

    if not tool_call_id:
        logger.error("Cannot resume run %s: no tool_call_id found", course_id)
        error_msg = "No tool_call_id found for commis resume"
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = CourseStatus.FAILED
        run.error = error_msg
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        # Emit error events for UI consistency
        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="error",
            payload={
                "thread_id": thread.id,
                "message": error_msg,
                "status": "error",
                "owner_id": owner_id,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
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
        "Resuming concierge run %s (thread=%s) with tool_call_id=%s [LangGraph-free]",
        course_id,
        thread.id,
        tool_call_id,
    )

    # Emit "resumed" event
    await emit_course_event(
        db=db,
        course_id=run.id,
        event_type="concierge_resumed",
        payload={
            "fiche_id": fiche.id,
            "thread_id": thread.id,
            "message_id": message_id,
            "owner_id": owner_id,
            "trace_id": trace_id,
        },
    )

    # Set up contexts (include trace_id for end-to-end tracing)
    # Include model/reasoning_effort so commis inherit concierge settings
    _concierge_ctx_token = set_concierge_context(
        course_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
    )

    _concierge_emitter = ConciergeEmitter(
        course_id=run.id,
        owner_id=owner_id,
        message_id=message_id,
        trace_id=trace_id,
    )
    _emitter_token = set_emitter(_concierge_emitter)
    _user_ctx_token = set_current_user_id(owner_id)

    try:
        # Create FicheRunner and run continuation
        # Inherit model/reasoning_effort from the original run for consistency
        runner = FicheRunner(
            fiche,
            model_override=run.model,
            reasoning_effort=run.reasoning_effort,
        )
        created_rows = await runner.run_continuation(
            db=db,
            thread=thread,
            tool_call_id=tool_call_id,
            tool_result=commis_result,
            course_id=course_id,
            trace_id=trace_id,
        )

        # Normal completion
        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = CourseStatus.SUCCESS
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

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="concierge_complete",
            payload={
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "result": final_response or "(No result)",
                "status": "success",
                "duration_ms": duration_ms,
                "debug_url": f"/concierge/{run.id}",
                "owner_id": owner_id,
                "message_id": message_id,
                "usage": usage_payload,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
                "status": "success",
                "finished_at": end_time.isoformat(),
                "duration_ms": duration_ms,
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        # Auto-summary -> Memory Files (async, best-effort)
        from zerg.models.models import ThreadMessage
        from zerg.services.memory_summarizer import schedule_course_summary

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
        schedule_course_summary(
            owner_id=owner_id,
            thread_id=thread.id,
            course_id=run.id,
            task=task_text or "",
            result_text=final_response or "",
            trace_id=str(run.trace_id) if run.trace_id else None,
        )

        reset_seq(run.id)
        logger.info("Successfully resumed concierge run %s", course_id)
        return {"status": "success", "result": final_response}

    except CourseInterrupted as e:
        # Concierge spawned another commis - set back to WAITING
        interrupt_value = e.interrupt_value
        job_id = interrupt_value.get("job_id") if isinstance(interrupt_value, dict) else None
        interrupt_message = "Working on this in the background..."

        run.status = CourseStatus.WAITING
        run.duration_ms = _compute_duration_ms(run.started_at)
        # Persist partial token usage (will be added to on next resume)
        if runner.usage_total_tokens is not None:
            run.total_tokens = (run.total_tokens or 0) + runner.usage_total_tokens
        db.commit()

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="concierge_waiting",
            payload={
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "job_id": job_id,
                "message": interrupt_message,
                "owner_id": owner_id,
                "message_id": message_id,
                "close_stream": False,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
                "status": "waiting",
                "thread_id": thread.id,
                "owner_id": owner_id,
            },
        )

        logger.info(
            "Concierge run %s interrupted again (WAITING for commis job %s) [LangGraph-free]",
            course_id,
            job_id,
        )
        return {"status": "waiting", "course_id": course_id, "job_id": job_id, "message": interrupt_message}

    except Exception as e:
        logger.exception("Failed to resume concierge run %s [LangGraph-free]: %s", course_id, e)

        end_time = datetime.now(timezone.utc)
        duration_ms = _compute_duration_ms(run.started_at, end_time=end_time)

        run.status = CourseStatus.FAILED
        run.error = str(e)
        run.finished_at = end_time.replace(tzinfo=None)
        run.duration_ms = duration_ms
        db.commit()

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="error",
            payload={
                "thread_id": thread.id,
                "message": str(e),
                "status": "error",
                "owner_id": owner_id,
                "trace_id": trace_id,
            },
        )

        await emit_course_event(
            db=db,
            course_id=run.id,
            event_type="course_updated",
            payload={
                "fiche_id": fiche.id,
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
        reset_concierge_context(_concierge_ctx_token)
        reset_emitter(_emitter_token)
        current_user_id_var.reset(_user_ctx_token)


async def resume_concierge_with_commis_result(
    db: Session,
    course_id: int,
    commis_result: str,
    job_id: int | None = None,
) -> dict[str, Any] | None:
    """Resume an interrupted concierge run with a commis result.

    Uses FicheRunner.run_continuation() for DB-based resume.

    Args:
        db: Database session.
        course_id: Course ID to resume.
        commis_result: Commis's result string.
        job_id: Optional CommisJob ID to look up tool_call_id.

    Returns:
        Dict with {"status": "success"|"waiting"|"error"|"skipped", ...}
    """
    return await _continue_concierge_langgraph_free(db, course_id, commis_result, job_id)


def _compute_duration_ms(started_at, *, end_time: datetime | None = None) -> int:
    if started_at is None:
        return 0
    end_dt = end_time or datetime.now(timezone.utc)
    try:
        started_dt = started_at.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    return max(0, int((end_dt - started_dt).total_seconds() * 1000))


# ---------------------------------------------------------------------------
# Timeout Reaper (Background Task)
# ---------------------------------------------------------------------------


async def reap_expired_barriers(db: Session) -> dict[str, Any]:
    """Find and handle expired barriers that have been waiting too long.

    Called periodically by the scheduler to prevent deadlock when commis hang.
    For each expired barrier:
    1. Mark incomplete BarrierJobs as 'timeout'
    2. Trigger resume with partial results + timeout errors

    Args:
        db: Database session.

    Returns:
        Dict with reaped barrier stats.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Find expired barriers still waiting
    expired_barriers = (
        db.query(CommisBarrier)
        .filter(
            CommisBarrier.status == "waiting",
            CommisBarrier.deadline_at.isnot(None),
            CommisBarrier.deadline_at < now,
        )
        .all()
    )

    if not expired_barriers:
        return {"reaped": 0}

    logger.info(f"Reaper found {len(expired_barriers)} expired barriers")
    reaped = []

    for barrier in expired_barriers:
        try:
            # Lock the barrier row to prevent concurrent resume
            locked_barrier = db.query(CommisBarrier).filter(CommisBarrier.id == barrier.id).with_for_update(nowait=True).first()

            if not locked_barrier or locked_barrier.status != "waiting":
                continue  # Already being processed

            # Mark as resuming (claim)
            locked_barrier.status = "resuming"

            # Mark incomplete BarrierJobs as timeout
            incomplete_jobs = (
                db.query(CommisBarrierJob)
                .filter(
                    CommisBarrierJob.barrier_id == barrier.id,
                    CommisBarrierJob.status.in_(["created", "queued"]),
                )
                .all()
            )

            for job in incomplete_jobs:
                job.status = "timeout"
                job.error = "Commis timed out (deadline exceeded)"
                job.completed_at = now

            db.commit()

            # Collect all results for batch resume (including timeouts)
            all_jobs = db.query(CommisBarrierJob).filter(CommisBarrierJob.barrier_id == barrier.id).all()
            commis_results = [
                {
                    "tool_call_id": j.tool_call_id,
                    "result": j.result or "",
                    "error": j.error,
                    "status": j.status,
                }
                for j in all_jobs
            ]

            # Trigger batch resume with partial results
            result = await resume_concierge_batch(
                db=db,
                course_id=barrier.course_id,
                commis_results=commis_results,
            )

            reaped.append(
                {
                    "barrier_id": barrier.id,
                    "course_id": barrier.course_id,
                    "timeout_count": len(incomplete_jobs),
                    "result": result.get("status") if result else "none",
                }
            )

            logger.info(
                f"Reaped expired barrier {barrier.id} (run={barrier.course_id}): "
                f"{len(incomplete_jobs)} timed out, resume status={result.get('status') if result else 'none'}"
            )

        except Exception as e:
            # Check if this is a lock contention error (nowait=True fails when row is locked)
            error_str = str(e).lower()
            is_lock_error = any(x in error_str for x in ["lock", "could not obtain", "nowait", "busy"])

            if is_lock_error:
                # Skip this barrier - another process is handling it
                logger.debug(f"Skipping barrier {barrier.id} - locked by another process")
                db.rollback()  # Clear any partial state
                continue

            # For other errors, mark as failed to prevent retry loops
            logger.exception(f"Failed to reap barrier {barrier.id}: {e}")
            try:
                db.rollback()  # Clear nested transaction state
                barrier.status = "failed"
                db.commit()
            except Exception:
                db.rollback()

    # Also clean up orphaned 'created' jobs (no barrier, stuck > 5 minutes)
    orphan_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    orphaned_jobs = (
        db.query(CommisJob)
        .filter(
            CommisJob.status == "created",
            CommisJob.created_at < orphan_cutoff,
        )
        .all()
    )

    orphan_count = 0
    for job in orphaned_jobs:
        # Check if this job has a barrier (via CommisBarrierJob)
        has_barrier = db.query(CommisBarrierJob).filter(CommisBarrierJob.job_id == job.id).first()
        if not has_barrier:
            job.status = "failed"
            job.error = "Orphaned job - barrier creation failed"
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            orphan_count += 1
            logger.warning(f"Cleaned up orphaned job {job.id} (stuck in 'created' without barrier)")

    if orphan_count:
        db.commit()
        logger.info(f"Cleaned up {orphan_count} orphaned 'created' jobs")

    return {"reaped": len(reaped), "orphans_cleaned": orphan_count, "details": reaped}
