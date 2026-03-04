"""Reaper logic for expired commis barriers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob
from zerg.models.models import CommisJob

logger = logging.getLogger(__name__)

ResumeBatchFn = Callable[..., Awaitable[dict[str, Any] | None]]


async def reap_expired_barriers(
    db: Session,
    *,
    resume_batch: ResumeBatchFn,
) -> dict[str, Any]:
    """Find and handle expired barriers that have been waiting too long."""
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
            result = await resume_batch(
                db=db,
                run_id=barrier.run_id,
                commis_results=commis_results,
            )

            reaped.append(
                {
                    "barrier_id": barrier.id,
                    "run_id": barrier.run_id,
                    "timeout_count": len(incomplete_jobs),
                    "result": result.get("status") if result else "none",
                }
            )

            logger.info(
                f"Reaped expired barrier {barrier.id} (run={barrier.run_id}): "
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
