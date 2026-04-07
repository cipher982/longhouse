"""Oikos internal endpoints for run resume.

Called internally when a commis completes while an oikos run is WAITING.
The new commis.py handles resume automatically, but this endpoint exists
for backward compatibility with external commis hooks.
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
    commis_id: str = Field("", description="Commis artifact ID (legacy, unused)")
    status: str = Field(..., description="Commis status: success or failed")
    result_summary: str | None = Field(None, description="Brief summary of commis result")
    error: str | None = Field(None, description="Error message if failed")


@router.post("/{run_id}/resume")
async def resume_run(
    run_id: int,
    payload: CommisCompletionPayload,
    db: Session = Depends(get_db),
):
    """Resume a WAITING run when its commis completes."""
    from zerg.services.commis import _resume_oikos

    run = db.query(Run).filter(Run.id == run_id).first()

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    if run.status != RunStatus.WAITING:
        logger.info(f"Run {run_id} status is {run.status.value}, skipping resume")
        return {
            "status": "skipped",
            "reason": f"Run is {run.status.value}, not WAITING",
            "run_id": run_id,
        }

    logger.info(f"Resuming run {run_id} after commis job {payload.job_id} completed with status {payload.status}")

    try:
        result_text = payload.result_summary or f"Commis job {payload.job_id} completed"
        if payload.status == "failed":
            result_text = f"Commis failed: {payload.error or 'Unknown error'}"

        await _resume_oikos(run_id, None, result_text)

        return {
            "status": "resumed",
            "run_id": run_id,
        }

    except Exception as e:
        logger.exception(f"Error resuming run {run_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume run: {str(e)}",
        )
