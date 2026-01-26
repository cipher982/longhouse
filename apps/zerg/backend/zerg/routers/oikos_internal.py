"""Oikos internal endpoints for run resume and background completion.

These endpoints are called internally by the backend (not exposed to public API)
to handle run resume when a oikos is interrupted and commis complete.

Uses the LangGraph-free oikos resume pattern:
- Oikos calls spawn_commis() and the loop raises RunInterrupted
- Run status becomes WAITING
- Commis completes, calls resume endpoint
- FicheRunner.run_continuation() continues execution
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
from zerg.models.models import Run

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/runs",
    tags=["internal"],
    dependencies=[Depends(require_internal_call)],
)


class CommisCompletionPayload(BaseModel):
    """Payload for commis completion webhook."""

    job_id: int = Field(..., description="Commis job ID")
    commis_id: str = Field(..., description="Commis artifact ID")
    status: str = Field(..., description="Commis status: success or failed")
    result_summary: str | None = Field(None, description="Brief summary of commis result")
    error: str | None = Field(None, description="Error message if failed")


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: int,
    payload: CommisCompletionPayload,
    db: Session = Depends(get_db),
):
    """Resume a WAITING run when a commis completes.

    Called internally when a commis completes while the oikos run was
    WAITING (interrupted by spawn_commis). Uses FicheRunner.run_continuation()
    to continue the oikos loop from persisted history.

    Args:
        run_id: ID of the WAITING oikos run
        payload: Commis completion data
        db: Database session

    Returns:
        Dict with resumed run info

    Raises:
        404: Run not found
        500: Error resuming run
    """
    run = db.query(Run).filter(Run.id == run_id).first()

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

    logger.info(f"Resuming run {run_id} after commis {payload.commis_id} completed with status {payload.status}")

    try:
        # Prepare result text for resume
        result_text = payload.result_summary or f"Commis job {payload.job_id} completed"
        if payload.status == "failed":
            result_text = f"Commis failed: {payload.error or 'Unknown error'}"

        # Use resume_oikos_with_commis_result which:
        # 1. Locates the tool_call_id for the commis job
        # 2. Injects the tool result via FicheRunner.run_continuation()
        # 3. Emits completion events
        from zerg.services.commis_resume import resume_oikos_with_commis_result

        result = await resume_oikos_with_commis_result(
            db=db,
            run_id=run_id,
            commis_result=result_text,
            job_id=payload.job_id,
        )

        result_status = result.get("status") if result else "unknown"
        # Preserve "resumed" status on success, but surface skips/errors/waiting explicitly.
        resume_status = "resumed"
        if result_status in ("skipped", "error", "waiting"):
            resume_status = result_status

        return {
            "status": resume_status,
            "run_id": run_id,
            "result_status": result_status,
        }

    except Exception as e:
        logger.exception(f"Error resuming run {run_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume run: {str(e)}",
        )
