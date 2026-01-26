"""Jarvis internal endpoints for course resume and background completion.

These endpoints are called internally by the backend (not exposed to public API)
to handle course resume when a concierge is interrupted and commis complete.

Uses the LangGraph-free concierge resume pattern:
- Concierge calls spawn_commis() and the loop raises CourseInterrupted
- Course status becomes WAITING
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
from zerg.models.enums import CourseStatus
from zerg.models.models import Course

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/courses",
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


@router.post("/{course_id}/resume")
async def resume_course(
    course_id: int,
    payload: CommisCompletionPayload,
    db: Session = Depends(get_db),
):
    """Resume a WAITING course when a commis completes.

    Called internally when a commis completes while the concierge course was
    WAITING (interrupted by spawn_commis). Uses FicheRunner.run_continuation()
    to continue the concierge loop from persisted history.

    Args:
        course_id: ID of the WAITING concierge course
        payload: Commis completion data
        db: Database session

    Returns:
        Dict with resumed course info

    Raises:
        404: Course not found
        500: Error resuming course
    """
    course = db.query(Course).filter(Course.id == course_id).first()

    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course {course_id} not found",
        )

    if course.status != CourseStatus.WAITING:
        # If course already completed or failed, this is a no-op (idempotent)
        logger.info(f"Course {course_id} status is {course.status.value}, skipping resume")
        return {
            "status": "skipped",
            "reason": f"Course is {course.status.value}, not WAITING",
            "course_id": course_id,
        }

    logger.info(f"Resuming course {course_id} after commis {payload.commis_id} completed with status {payload.status}")

    try:
        # Prepare result text for resume
        result_text = payload.result_summary or f"Commis job {payload.job_id} completed"
        if payload.status == "failed":
            result_text = f"Commis failed: {payload.error or 'Unknown error'}"

        # Use resume_concierge_with_commis_result which:
        # 1. Locates the tool_call_id for the commis job
        # 2. Injects the tool result via FicheRunner.run_continuation()
        # 3. Emits completion events
        from zerg.services.commis_resume import resume_concierge_with_commis_result

        result = await resume_concierge_with_commis_result(
            db=db,
            course_id=course_id,
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
            "course_id": course_id,
            "result_status": result_status,
        }

    except Exception as e:
        logger.exception(f"Error resuming course {course_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume course: {str(e)}",
        )
