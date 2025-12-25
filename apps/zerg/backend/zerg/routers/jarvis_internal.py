"""Jarvis internal endpoints for durable runs continuation.

These endpoints are for internal service-to-service communication,
not exposed to external clients. They handle the webhook callbacks
when background workers complete.

Durable runs v2.2 Phase 4: Worker completion triggers supervisor continuation.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class ContinuationPayload(BaseModel):
    """Payload for worker completion webhook."""

    trigger: str = "worker_complete"  # Type of trigger
    job_id: int  # Completed worker job ID
    worker_id: str  # Worker ID for artifact lookup
    status: str  # Worker completion status (success/failed)
    result_summary: str  # Summary of worker result


class ContinuationResponse(BaseModel):
    """Response from continuation endpoint."""

    status: str  # "continuation_triggered" or "skipped"
    original_run_id: int
    continuation_run_id: Optional[int] = None
    message: Optional[str] = None


@router.post("/runs/{run_id}/continue", response_model=ContinuationResponse)
async def continue_run(
    run_id: int,
    payload: ContinuationPayload,
    db: Session = Depends(get_db),
) -> ContinuationResponse:
    """Handle worker completion - trigger supervisor continuation.

    Called internally when a background worker completes. This endpoint:
    1. Validates the original run exists and is in DEFERRED state
    2. Triggers supervisor_service.run_continuation() as a background task
    3. Returns immediately so the worker processor doesn't block

    Args:
        run_id: The original (deferred) supervisor run ID
        payload: Worker completion details
        db: Database session

    Returns:
        ContinuationResponse indicating whether continuation was triggered

    Raises:
        HTTPException: 404 if run not found
    """
    from zerg.services.supervisor_service import SupervisorService

    # Validate run exists
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    # Only continue DEFERRED runs (timeout-migrated runs waiting for workers)
    if run.status != RunStatus.DEFERRED:
        logger.info(f"Skipping continuation for run {run_id}: status is {run.status.value}, not DEFERRED")
        return ContinuationResponse(
            status="skipped",
            original_run_id=run_id,
            message=f"Run status is {run.status.value}, not DEFERRED. No continuation needed.",
        )

    logger.info(
        f"Triggering continuation for deferred run {run_id} " f"(job={payload.job_id}, worker={payload.worker_id}, status={payload.status})"
    )

    # Create supervisor service with fresh DB session for background task
    supervisor_service = SupervisorService(db)

    # Run continuation as background task so we return immediately
    # Note: In a distributed setup, this would be an async job queue
    async def run_continuation_task():
        try:
            result = await supervisor_service.run_continuation(
                original_run_id=run_id,
                job_id=payload.job_id,
                worker_id=payload.worker_id,
                result_summary=payload.result_summary,
            )
            logger.info(f"Continuation run {result.run_id} completed for deferred run {run_id}: {result.status}")
        except Exception as e:
            logger.exception(f"Continuation failed for deferred run {run_id}: {e}")

    # Start background task
    asyncio.create_task(run_continuation_task())

    return ContinuationResponse(
        status="continuation_triggered",
        original_run_id=run_id,
        message=f"Continuation triggered for job {payload.job_id}",
    )
