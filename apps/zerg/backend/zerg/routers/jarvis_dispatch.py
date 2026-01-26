"""Jarvis manual fiche dispatch endpoint."""

import logging
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.models.models import Course
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.task_runner import execute_fiche_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisDispatchRequest(BaseModel):
    """Jarvis dispatch request to trigger fiche execution."""

    fiche_id: int = Field(..., description="ID of fiche to execute")
    task_override: Optional[str] = Field(None, description="Optional task instruction override")


class JarvisDispatchResponse(BaseModel):
    """Jarvis dispatch response with course/thread IDs."""

    course_id: int = Field(..., description="Course ID for tracking execution")
    thread_id: int = Field(..., description="Thread ID containing conversation")
    status: str = Field(..., description="Initial course status")
    fiche_name: str = Field(..., description="Name of fiche being executed")


@router.post("/dispatch", response_model=JarvisDispatchResponse)
async def jarvis_dispatch(
    request: JarvisDispatchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisDispatchResponse:
    """Dispatch fiche task from Jarvis.

    Triggers immediate execution of a fiche task and returns course/thread IDs
    for tracking. Jarvis can then listen to the SSE stream for updates.

    Args:
        request: Dispatch request with fiche_id and optional task override
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        JarvisDispatchResponse with course and thread IDs

    Raises:
        404: Fiche not found
        409: Fiche already running
        500: Execution error
    """
    # Get fiche
    fiche = crud.get_fiche(db, request.fiche_id)
    if not fiche:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fiche {request.fiche_id} not found",
        )
    # Authorization: only owner or admin may dispatch a fiche's task
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: not fiche owner")

    # Optionally override task instructions
    original_task = fiche.task_instructions
    if request.task_override:
        fiche.task_instructions = request.task_override

    try:
        # Execute fiche task (creates thread and course)
        thread = await execute_fiche_task(db, fiche, thread_type="manual")

        # Get the created course
        course = db.query(Course).filter(Course.thread_id == thread.id).order_by(Course.created_at.desc()).first()

        if not course:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create fiche course",
            )

        logger.info(f"Jarvis dispatched fiche {fiche.id} (course {course.id}, thread {thread.id})")

        return JarvisDispatchResponse(
            course_id=course.id,
            thread_id=thread.id,
            status=course.status.value if hasattr(course.status, "value") else str(course.status),
            fiche_name=fiche.name,
        )

    except ValueError as e:
        # Fiche already running or validation error
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Jarvis dispatch failed for fiche {fiche.id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to dispatch fiche: {str(e)}",
        )
    finally:
        # Restore original task instructions if overridden
        if request.task_override:
            fiche.task_instructions = original_task
            db.add(fiche)
            db.commit()
