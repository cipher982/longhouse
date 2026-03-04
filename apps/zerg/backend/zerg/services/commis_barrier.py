"""Barrier coordination helpers for parallel commis completion."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from zerg.models.commis_barrier import CommisBarrier
from zerg.models.commis_barrier import CommisBarrierJob

logger = logging.getLogger(__name__)


async def check_and_resume_if_all_complete(
    db: Session,
    run_id: int,
    job_id: int,
    result: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Atomic barrier check. Only ONE commis triggers resume.

    Uses SELECT FOR UPDATE + status guard in single transaction to prevent
    the double-resume race condition where multiple commis completing
    simultaneously might both try to resume the oikos.
    """
    try:
        # Use a transaction context for atomic operations
        # Note: SQLAlchemy's begin() creates a subtransaction if already in one
        with db.begin_nested():
            # 1. Lock the barrier row with FOR UPDATE
            barrier = db.query(CommisBarrier).filter(CommisBarrier.run_id == run_id).with_for_update().first()

            if not barrier:
                logger.warning("No barrier found for run_id=%s", run_id)
                return {"status": "skipped", "reason": "no barrier found"}

            if barrier.status != "waiting":
                logger.info("Barrier for run %s is %s, not waiting", run_id, barrier.status)
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
                run_id,
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
                    run_id,
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
        logger.exception("Error in check_and_resume_if_all_complete for run %s: %s", run_id, e)
        # Let the caller handle the exception
        raise
