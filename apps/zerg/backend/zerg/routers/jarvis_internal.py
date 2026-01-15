"""Jarvis internal endpoints for run resume and background completion.

These endpoints are called internally by the backend (not exposed to public API)
to handle run resume when a supervisor is interrupted and workers complete.

Uses the LangGraph-free supervisor resume pattern:
- Supervisor calls spawn_worker() and the loop raises AgentInterrupted
- Run status becomes WAITING
- Worker completes, calls resume endpoint
- AgentRunner.run_continuation() continues execution
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_internal_call
from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/runs",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)


class WorkerCompletionPayload(BaseModel):
    """Payload for worker completion webhook."""

    job_id: int = Field(..., description="Worker job ID")
    worker_id: str = Field(..., description="Worker artifact ID")
    status: str = Field(..., description="Worker status: success or failed")
    result_summary: str | None = Field(None, description="Brief summary of worker result")
    error: str | None = Field(None, description="Error message if failed")


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: int,
    payload: WorkerCompletionPayload,
    db: Session = Depends(get_db),
):
    """Resume a WAITING run when a worker completes.

    Called internally when a worker completes while the supervisor run was
    WAITING (interrupted by spawn_worker). Uses AgentRunner.run_continuation()
    to continue the supervisor loop from persisted history.

    Args:
        run_id: ID of the WAITING supervisor run
        payload: Worker completion data
        db: Database session

    Returns:
        Dict with resumed run info

    Raises:
        404: Run not found
        500: Error resuming run
    """
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    if run.status != RunStatus.WAITING:
        # If run already completed or failed, this is a no-op (idempotent)
        logger.info(f"Run {run_id} status is {run.status.value}, skipping resume")
        return {
            "status": "skipped",
            "reason": f"Run is {run.status.value}, not WAITING",
            "run_id": run_id,
        }

    logger.info(f"Resuming run {run_id} after worker {payload.worker_id} completed with status {payload.status}")

    try:
        # Prepare result text for resume
        result_text = payload.result_summary or f"Worker job {payload.job_id} completed"
        if payload.status == "failed":
            result_text = f"Worker failed: {payload.error or 'Unknown error'}"

        # Use resume_supervisor_with_worker_result which:
        # 1. Locates the tool_call_id for the worker job
        # 2. Injects the tool result via AgentRunner.run_continuation()
        # 3. Emits completion events
        from zerg.services.worker_resume import resume_supervisor_with_worker_result

        result = await resume_supervisor_with_worker_result(
            db=db,
            run_id=run_id,
            worker_result=result_text,
            job_id=payload.job_id,
        )

        return {
            "status": "resumed",
            "run_id": run_id,
            "result_status": result.get("status") if result else "unknown",
        }

    except Exception as e:
        logger.exception(f"Error resuming run {run_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume run: {str(e)}",
        )


# Keep old endpoint for backwards compatibility during transition
@router.post("/{run_id}/continue")
async def continue_run(
    run_id: int,
    payload: WorkerCompletionPayload,
    db: Session = Depends(get_db),
):
    """Deprecated: Use /resume instead. Kept for backwards compatibility."""
    logger.warning(f"Deprecated /continue endpoint called for run {run_id}, redirecting to /resume")
    return await resume_run(run_id, payload, db)
