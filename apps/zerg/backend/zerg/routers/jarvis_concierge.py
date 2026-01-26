"""Jarvis concierge endpoints - dispatch and cancel."""

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import Course
from zerg.models.models import Fiche
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user
from zerg.services.concierge_context import reset_seq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])

# Track running concierge tasks so we can cancel them on demand
_concierge_tasks: Dict[int, asyncio.Task] = {}
_concierge_tasks_lock = asyncio.Lock()


async def _register_concierge_task(course_id: int, task: asyncio.Task) -> None:
    """Store the running concierge task for cancellation."""
    async with _concierge_tasks_lock:
        _concierge_tasks[course_id] = task


async def _pop_concierge_task(course_id: int) -> Optional[asyncio.Task]:
    """Remove and return the concierge task for a course."""
    async with _concierge_tasks_lock:
        return _concierge_tasks.pop(course_id, None)


async def _cancel_concierge_task(course_id: int) -> bool:
    """Attempt to cancel a running concierge task.

    Returns:
        bool: True if a task was found (cancellation requested), False otherwise.
    """
    async with _concierge_tasks_lock:
        task = _concierge_tasks.get(course_id)

    if not task or task.done():
        return False

    task.cancel()
    try:
        # Give the task a moment to process cancellation
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.TimeoutError:
        # Task is taking longer to cooperate; leave it cancelled in the background
        pass
    except asyncio.CancelledError:
        pass

    return True


class JarvisConciergeRequest(BaseModel):
    """Request to dispatch a task to the concierge fiche."""

    task: str = Field(..., description="Natural language task for the concierge")
    context: Optional[dict] = Field(
        None,
        description="Optional context including conversation_id and previous_messages",
    )
    preferences: Optional[dict] = Field(
        None,
        description="Optional preferences like verbosity and notify_on_complete",
    )


class JarvisConciergeResponse(BaseModel):
    """Response from concierge dispatch."""

    course_id: int = Field(..., description="Concierge course ID for tracking")
    thread_id: int = Field(..., description="Concierge thread ID (long-lived)")
    status: str = Field(..., description="Initial course status")
    stream_url: str = Field(..., description="SSE stream URL for progress updates")


class JarvisCancelResponse(BaseModel):
    """Response from concierge cancellation."""

    course_id: int = Field(..., description="The cancelled course ID")
    status: str = Field(..., description="Course status after cancellation")
    message: str = Field(..., description="Human-readable status message")


@router.post("/concierge", response_model=JarvisConciergeResponse)
async def jarvis_concierge(
    request: JarvisConciergeRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisConciergeResponse:
    """Dispatch a task to the concierge fiche.

    The concierge is the "one brain" that coordinates commis and maintains
    long-term context. Each user has a single concierge thread that persists
    across sessions.

    This endpoint:
    1. Finds or creates the user's concierge thread (idempotent)
    2. Creates a new course attached to that thread
    3. Kicks off concierge execution in the background
    4. Returns immediately with course_id and stream_url

    Args:
        request: Task and optional context/preferences
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisConciergeResponse with course_id, thread_id, and stream_url

    Example:
        POST /api/jarvis/concierge
        {"task": "Check my server health"}

        Response:
        {
            "course_id": 456,
            "thread_id": 789,
            "status": "running",
            "stream_url": "/api/stream/courses/456"
        }
    """
    from zerg.services.concierge_service import ConciergeService

    concierge_service = ConciergeService(db)

    # Server-side enforcement: respect user tool configuration.
    if not _is_tool_enabled(current_user.context or {}, "concierge"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tool disabled: concierge",
        )

    # Get or create concierge components (idempotent)
    fiche = concierge_service.get_or_create_concierge_fiche(current_user.id)
    thread = concierge_service.get_or_create_concierge_thread(current_user.id, fiche)

    # Create course record (marks as running)
    from zerg.models.enums import CourseStatus
    from zerg.models.enums import CourseTrigger

    course = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
    )
    db.add(course)
    db.commit()
    db.refresh(course)

    logger.info(f"Jarvis concierge: created course {course.id} for user {current_user.id}, task: {request.task[:50]}...")

    # Unit tests use a per-test ephemeral database that is torn down immediately after the request.
    # Spawning background tasks here can outlive the test and crash xdist commis during teardown.
    from zerg.config import get_settings

    if get_settings().testing:
        logger.debug("TESTING=1: skipping background concierge execution for course %s", course.id)
        return JarvisConciergeResponse(
            course_id=course.id,
            thread_id=thread.id,
            status="running",
            stream_url=f"/api/stream/courses/{course.id}",
        )

    # Start concierge execution in background
    async def run_concierge_background(owner_id: int, task: str, course_id: int):
        """Execute concierge in background."""
        from zerg.database import db_session
        from zerg.services.concierge_service import ConciergeService

        try:
            with db_session() as bg_db:
                service = ConciergeService(bg_db)
                # Run concierge - pass course_id to avoid duplicate course creation
                await service.run_concierge(
                    owner_id=owner_id,
                    task=task,
                    course_id=course_id,  # Use the course created in the endpoint
                    timeout=120,
                    return_on_deferred=False,
                )
        except Exception as e:
            logger.exception(f"Background concierge execution failed for course {course_id}: {e}")
        finally:
            await _pop_concierge_task(course_id)

    # Create background task - runs independently of the request
    task_handle = asyncio.create_task(run_concierge_background(current_user.id, request.task, course.id))
    await _register_concierge_task(course.id, task_handle)

    return JarvisConciergeResponse(
        course_id=course.id,
        thread_id=thread.id,
        status="running",
        stream_url=f"/api/stream/courses/{course.id}",
    )


@router.post("/concierge/{course_id}/cancel", response_model=JarvisCancelResponse)
async def jarvis_concierge_cancel(
    course_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisCancelResponse:
    """Cancel a running concierge investigation.

    Marks the course as cancelled and emits a cancellation event to SSE subscribers.
    If the course is already complete, returns the current status without error.

    Args:
        course_id: The concierge course ID to cancel
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisCancelResponse with course status

    Raises:
        HTTPException 404: If course not found or doesn't belong to user
    """
    from zerg.models.enums import CourseStatus

    # Validate course exists
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course {course_id} not found",
        )

    # Check ownership via the course's fiche
    fiche = db.query(Fiche).filter(Fiche.id == course.fiche_id).first()
    if not fiche or fiche.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Course {course_id} not found",  # Don't reveal existence to other users
        )

    # Check if already complete
    terminal_statuses = {CourseStatus.SUCCESS, CourseStatus.FAILED, CourseStatus.CANCELLED}
    if course.status in terminal_statuses:
        return JarvisCancelResponse(
            course_id=course_id,
            status=course.status.value if hasattr(course.status, "value") else str(course.status),
            message="Course already completed",
        )

    # Mark as cancelled
    course.status = CourseStatus.CANCELLED
    course.finished_at = datetime.now(timezone.utc)
    db.add(course)
    db.commit()

    # Attempt to cancel the running background task (best-effort)
    await _cancel_concierge_task(course_id)

    logger.info(f"Concierge course {course_id} cancelled by user {current_user.id}")

    # Emit cancellation event for SSE subscribers
    await event_bus.publish(
        EventType.CONCIERGE_COMPLETE,
        {
            "event_type": "concierge_complete",
            "course_id": course_id,
            "owner_id": current_user.id,
            "status": "cancelled",
            "message": "Investigation cancelled by user",
        },
    )

    # Reset sequence counter once final event is emitted
    reset_seq(course_id)

    return JarvisCancelResponse(
        course_id=course_id,
        status="cancelled",
        message="Investigation cancelled",
    )
