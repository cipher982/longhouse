"""Trigger logic for commis inbox continuation runs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import CommisJob
from zerg.models.models import Run
from zerg.services.commis_inbox_followup import INBOX_QUEUED_RESULT
from zerg.services.commis_inbox_followup import schedule_inbox_followup_after_run
from zerg.services.commis_inbox_prompt import build_commis_inbox_synthetic_task
from zerg.services.commis_updates import queue_commis_update

logger = logging.getLogger(__name__)


async def trigger_commis_inbox_run(
    db: Session,
    original_run_id: int,
    commis_job_id: int,
    commis_result: str,
    commis_status: str,  # "success" | "failed"
    commis_error: str | None = None,
) -> dict[str, Any]:
    """Trigger a continuation oikos run when commis completes and original run is terminal."""
    from zerg.services.oikos_service import OikosService
    from zerg.services.oikos_service import emit_stream_control

    try:
        # Load original run
        original_run = db.query(Run).filter(Run.id == original_run_id).first()
        if not original_run:
            logger.warning(f"Cannot trigger inbox: original run {original_run_id} not found")
            return {"status": "skipped", "reason": "original run not found"}

        # Verify original run is in terminal state
        if original_run.status not in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
            logger.info(f"Skipping inbox trigger: original run {original_run_id} is {original_run.status.value}, not terminal")
            return {"status": "skipped", "reason": f"original run is {original_run.status.value}"}

        # Load commis job for context
        commis_job = db.query(CommisJob).filter(CommisJob.id == commis_job_id).first()
        commis_task = commis_job.task if commis_job else "unknown task"

        # Get original run's context
        thread = original_run.thread
        fiche = original_run.fiche
        owner_id = fiche.owner_id

        # Determine root_run_id for SSE aliasing through continuation chains
        # If original_run already has a root_run_id, propagate it; otherwise use original_run_id
        root_run_id = getattr(original_run, "root_run_id", None) or original_run_id

        # Check for existing continuation run
        existing_continuation = db.query(Run).filter(Run.continuation_of_run_id == original_run_id).first()

        if existing_continuation:
            # Existing continuation - handle based on its status
            if existing_continuation.status == RunStatus.RUNNING:
                # Continuation is running; queue update and trigger follow-up after it finishes.
                logger.info(
                    "Queueing commis %s update while continuation %s is running",
                    commis_job_id,
                    existing_continuation.id,
                )
                queue_commis_update(
                    db=db,
                    thread_id=thread.id,
                    commis_job_id=commis_job_id,
                    commis_task=commis_task,
                    commis_status=commis_status,
                    commis_result=commis_result,
                    commis_error=commis_error,
                )
                schedule_inbox_followup_after_run(
                    run_id=existing_continuation.id,
                    commis_job_id=commis_job_id,
                    commis_status=commis_status,
                    commis_error=commis_error,
                    trigger_followup=trigger_commis_inbox_run,
                )
                return {
                    "status": "queued",
                    "continuation_run_id": existing_continuation.id,
                    "message": "Queued commis update; follow-up will run after current continuation completes",
                }

            if existing_continuation.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
                # Continuation already finished - create a chain (continuation of continuation)
                logger.info(
                    f"Existing continuation {existing_continuation.id} is {existing_continuation.status.value}, "
                    f"creating chain continuation"
                )
                # Use the existing continuation as the parent for the new one
                original_run = existing_continuation
                original_run_id = existing_continuation.id
            else:
                # WAITING or QUEUED - let normal resume handle it
                logger.info(
                    f"Existing continuation {existing_continuation.id} is {existing_continuation.status.value}, " f"skipping inbox trigger"
                )
                return {
                    "status": "skipped",
                    "reason": f"existing continuation is {existing_continuation.status.value}",
                }

        # Create new continuation run
        start_time = datetime.now(timezone.utc)
        started_at_naive = start_time.replace(tzinfo=None)

        # Generate new trace_id but inherit model/reasoning_effort
        new_trace_id = uuid.uuid4()
        new_message_id = str(uuid.uuid4())

        continuation_run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            continuation_of_run_id=original_run_id,
            root_run_id=root_run_id,  # For SSE aliasing through chains
            status=RunStatus.RUNNING,
            trigger=RunTrigger.CONTINUATION,
            started_at=started_at_naive,
            model=original_run.model,
            reasoning_effort=original_run.reasoning_effort,
            trace_id=new_trace_id,
            assistant_message_id=new_message_id,
        )
        db.add(continuation_run)

        try:
            db.commit()
            db.refresh(continuation_run)
        except IntegrityError as e:
            # Unique constraint violation - another process created continuation
            db.rollback()
            logger.info(f"Continuation already exists for run {original_run_id} (race condition): {e}")
            # Re-query and try to merge into the existing continuation
            existing = db.query(Run).filter(Run.continuation_of_run_id == original_run_id).first()
            if existing:
                if existing.status == RunStatus.RUNNING:
                    queue_commis_update(
                        db=db,
                        thread_id=thread.id,
                        commis_job_id=commis_job_id,
                        commis_task=commis_task,
                        commis_status=commis_status,
                        commis_result=commis_result,
                        commis_error=commis_error,
                    )
                    schedule_inbox_followup_after_run(
                        run_id=existing.id,
                        commis_job_id=commis_job_id,
                        commis_status=commis_status,
                        commis_error=commis_error,
                        trigger_followup=trigger_commis_inbox_run,
                    )
                    return {
                        "status": "queued",
                        "continuation_run_id": existing.id,
                        "message": "Race recovery: queued commis update; follow-up will run after current continuation completes",
                    }
                if existing.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
                    # Existing continuation already finished - recurse to create chain
                    logger.info(
                        "Race recovery: existing continuation %s is %s, recursing to create chain",
                        existing.id,
                        existing.status.value,
                    )
                    return await trigger_commis_inbox_run(
                        db=db,
                        original_run_id=existing.id,  # Chain off the existing continuation
                        commis_job_id=commis_job_id,
                        commis_result=commis_result,
                        commis_status=commis_status,
                        commis_error=commis_error,
                    )
            return {"status": "skipped", "reason": "continuation already exists (race)"}

        logger.info(
            f"Created inbox continuation run {continuation_run.id} for original run {original_run_id} "
            f"(commis {commis_job_id} completed)"
        )

        # Emit stream_control:keep_open for continuation start
        await emit_stream_control(db, original_run, "keep_open", "continuation_start", owner_id, ttl_ms=180_000)

        # Build synthetic task for oikos
        synthetic_task = build_commis_inbox_synthetic_task(
            commis_result=commis_result,
            commis_status=commis_status,
            commis_task=commis_task,
            commis_error=commis_error,
            queued_result_sentinel=INBOX_QUEUED_RESULT,
        )

        # Run oikos with the synthetic task
        oikos_service = OikosService(db)
        result = await oikos_service.run_oikos(
            owner_id=owner_id,
            task=synthetic_task,
            run_id=continuation_run.id,
            message_id=new_message_id,
            trace_id=str(new_trace_id),
            model_override=original_run.model,
            reasoning_effort=original_run.reasoning_effort,
            timeout=120,  # Give continuation plenty of time
            source_surface_id="system",
            source_conversation_id="system:commis-inbox",
            source_message_id=new_message_id,
        )

        logger.info(f"Inbox continuation run {continuation_run.id} completed with status {result.status}")

        return {
            "status": "triggered",
            "continuation_run_id": continuation_run.id,
            "result_status": result.status,
        }

    except Exception as e:
        logger.exception(f"Error triggering inbox run for original run {original_run_id}: {e}")
        return {"status": "error", "error": str(e)}
