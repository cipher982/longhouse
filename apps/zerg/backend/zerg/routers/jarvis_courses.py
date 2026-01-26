"""Jarvis course history endpoints."""

import json
import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.models.course_event import CourseEvent
from zerg.models.enums import CourseStatus
from zerg.models.models import Course
from zerg.models.models import Fiche
from zerg.models.models import ThreadMessage
from zerg.routers.jarvis_auth import get_current_jarvis_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["jarvis"])


class JarvisCourseSummary(BaseModel):
    """Minimal course summary for Jarvis Task Inbox."""

    id: int
    fiche_id: int
    thread_id: Optional[int] = None
    fiche_name: str
    status: str
    summary: Optional[str] = None
    signal: Optional[str] = None
    signal_source: Optional[str] = None
    error: Optional[str] = None
    last_event_type: Optional[str] = None
    last_event_message: Optional[str] = None
    last_event_at: Optional[datetime] = None
    continuation_of_course_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


@router.get("/courses", response_model=List[JarvisCourseSummary])
def list_jarvis_courses(
    limit: int = 50,
    fiche_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> List[JarvisCourseSummary]:
    """List recent fiche courses for Jarvis Task Inbox.

    Returns recent course history with summaries, filtered by fiche if specified.
    This powers the Task Inbox UI in Jarvis showing all automated activity.

    Args:
        limit: Maximum number of courses to return (default 50)
        fiche_id: Optional filter by specific fiche
        db: Database session
        current_user: Authenticated user (Jarvis service account)

    Returns:
        List of course summaries ordered by created_at descending
    """
    # Get recent courses
    # TODO: Add crud method for filtering by fiche_id and ordering by created_at
    # For now, get all courses and filter/sort in memory

    # Multi-tenant SaaS: Jarvis shows only the logged-in user's courses.
    query = (
        db.query(Course)
        .options(selectinload(Course.fiche))
        .join(Fiche, Fiche.id == Course.fiche_id)
        .filter(Fiche.owner_id == current_user.id)
    )

    if fiche_id:
        query = query.filter(Course.fiche_id == fiche_id)

    courses = query.order_by(Course.created_at.desc()).limit(limit).all()

    course_ids = [course.id for course in courses]
    thread_ids = [course.thread_id for course in courses if course.thread_id]

    last_events_by_course = _get_latest_course_events(db, course_ids)
    last_messages_by_thread = _get_latest_assistant_messages(db, thread_ids)

    summaries = []
    for course in courses:
        fiche_name = course.fiche.name if course.fiche else f"Fiche {course.fiche_id}"

        # Extract summary from course (will be populated in Phase 2.3)
        summary = getattr(course, "summary", None)

        last_event = last_events_by_course.get(course.id)
        last_event_type = getattr(last_event, "event_type", None) if last_event else None
        last_event_at = getattr(last_event, "created_at", None) if last_event else None
        last_event_message = _extract_event_message(getattr(last_event, "payload", None)) if last_event else None

        signal = summary if summary else None
        signal_source = "summary" if summary else None

        if not signal:
            course_error = getattr(course, "error", None)
            if course_error:
                signal = course_error
                signal_source = "error"

        if not signal and course.thread_id:
            last_message = last_messages_by_thread.get(course.thread_id)
            if last_message:
                signal = last_message
                signal_source = "last_message"

        if not signal and last_event_message:
            signal = last_event_message
            signal_source = "last_event"

        signal = _truncate_signal(signal, 240)

        summaries.append(
            JarvisCourseSummary(
                id=course.id,
                fiche_id=course.fiche_id,
                thread_id=course.thread_id,
                fiche_name=fiche_name,
                status=course.status.value if hasattr(course.status, "value") else str(course.status),
                summary=summary,
                signal=signal,
                signal_source=signal_source,
                error=getattr(course, "error", None),
                last_event_type=last_event_type,
                last_event_message=last_event_message,
                last_event_at=last_event_at,
                continuation_of_course_id=getattr(course, "continuation_of_course_id", None),
                created_at=course.created_at,
                updated_at=course.updated_at,
                completed_at=course.finished_at,
            )
        )

    return summaries


def _extract_text_from_message_content(content: Any) -> Optional[str]:
    """Extract text from ThreadMessage content payloads."""
    if not content:
        return None

    # Handle string content (most common case)
    if isinstance(content, str):
        # Try to parse as JSON if it looks like structured content
        if content.startswith("[") or content.startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    # Handle array of content blocks
                    text_parts = []
                    for block in parsed:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    return " ".join(text_parts) if text_parts else content
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON, return as-is
        return content

    # Handle native list (if column supports JSON type)
    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        return " ".join(text_parts) if text_parts else None

    return str(content) if content else None


def _get_last_assistant_message(db: Session, thread_id: int) -> Optional[str]:
    """Get the last assistant message from a thread.

    Args:
        db: Database session
        thread_id: Thread ID to query

    Returns:
        Content of the last assistant message, or None if not found
    """

    last_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.desc())
        .first()
    )

    if not last_msg or not last_msg.content:
        return None

    return _extract_text_from_message_content(last_msg.content)


def _get_latest_assistant_messages(db: Session, thread_ids: List[int]) -> Dict[int, str]:
    if not thread_ids:
        return {}

    subquery = (
        db.query(
            ThreadMessage.thread_id.label("thread_id"),
            ThreadMessage.content.label("content"),
            func.row_number()
            .over(
                partition_by=ThreadMessage.thread_id,
                order_by=ThreadMessage.id.desc(),
            )
            .label("rn"),
        )
        .filter(ThreadMessage.thread_id.in_(thread_ids))
        .filter(ThreadMessage.role == "assistant")
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    output: Dict[int, str] = {}
    for row in rows:
        text = _extract_text_from_message_content(row.content)
        if text:
            output[row.thread_id] = text
    return output


def _get_latest_course_events(db: Session, course_ids: List[int]) -> Dict[int, Any]:
    if not course_ids:
        return {}

    subquery = (
        db.query(
            CourseEvent.course_id.label("course_id"),
            CourseEvent.event_type.label("event_type"),
            CourseEvent.payload.label("payload"),
            CourseEvent.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=CourseEvent.course_id,
                order_by=CourseEvent.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(CourseEvent.course_id.in_(course_ids))
        .subquery()
    )

    rows = db.query(subquery).filter(subquery.c.rn == 1).all()
    return {row.course_id: row for row in rows}


def _extract_event_message(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload or not isinstance(payload, dict):
        return None

    for key in ("message", "summary", "error", "result", "status"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return f"Tool: {tool_name}"

    return None


def _truncate_signal(signal: Optional[str], max_length: int) -> Optional[str]:
    if not signal:
        return signal
    normalized = " ".join(signal.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1].rstrip() + "…"


class CourseStatusResponse(BaseModel):
    """Detailed status of a specific course."""

    course_id: int
    status: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[str] = None


@router.get("/courses/active")
def get_active_course(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
):
    """Get the user's currently running concierge course (if any).

    Returns the most recent RUNNING, WAITING, or DEFERRED course for the user's concierge fiche.
    Returns 204 No Content if no active course exists.

    This endpoint enables course reconnection after page refresh.

    Args:
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        JSONResponse with course details if found, or 204 No Content
    """
    # Import here to avoid circular dependency
    from zerg.services.concierge_service import ConciergeService

    concierge_service = ConciergeService(db)
    concierge_fiche = concierge_service.get_or_create_concierge_fiche(current_user.id)

    # Prefer RUNNING courses. DEFERRED courses are "in-flight" only if they have not
    # already produced a successful continuation.
    active_course = (
        db.query(Course)
        .filter(Course.fiche_id == concierge_fiche.id)
        .filter(Course.status == CourseStatus.RUNNING)
        .order_by(Course.created_at.desc())
        .first()
    )

    if not active_course:
        # WAITING courses are interrupted via spawn_commis (concierge resume).
        active_course = (
            db.query(Course)
            .filter(Course.fiche_id == concierge_fiche.id)
            .filter(Course.status == CourseStatus.WAITING)
            .order_by(Course.created_at.desc())
            .first()
        )

    if not active_course:
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        from zerg.models.enums import CourseTrigger

        Continuation = aliased(Course)
        has_terminal_continuation = exists().where(
            (Continuation.continuation_of_course_id == Course.id)
            & (Continuation.trigger == CourseTrigger.CONTINUATION)
            & (Continuation.status.in_([CourseStatus.SUCCESS, CourseStatus.FAILED, CourseStatus.CANCELLED]))
        )

        active_course = (
            db.query(Course)
            .filter(Course.fiche_id == concierge_fiche.id)
            .filter(Course.status == CourseStatus.DEFERRED)
            .filter(~has_terminal_continuation)
            .order_by(Course.created_at.desc())
            .first()
        )

    if not active_course:
        # No active course - return 204 No Content
        return JSONResponse(status_code=204, content=None)

    # Return course details for reconnection
    return JSONResponse(
        {
            "course_id": active_course.id,
            "status": active_course.status.value,
            "created_at": active_course.created_at.isoformat(),
        }
    )


@router.get("/courses/{course_id}", response_model=CourseStatusResponse)
def get_course_status(
    course_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> CourseStatusResponse:
    """Get current status of a specific course.

    Returns detailed status including timing, errors, and result if completed.
    This endpoint is used for polling course status after async task submission.

    Args:
        course_id: ID of the course to query
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        Course status with result if completed

    Raises:
        HTTPException: 404 if course not found or not owned by user
    """
    # Multi-tenant security: only return courses owned by the current user
    course = (
        db.query(Course)
        .join(Fiche, Fiche.id == Course.fiche_id)
        .filter(Course.id == course_id)
        .filter(Fiche.owner_id == current_user.id)
        .first()
    )

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Include result only if course succeeded
    result = None
    if course.status == CourseStatus.SUCCESS:
        result = _get_last_assistant_message(db, course.thread_id)

    return CourseStatusResponse(
        course_id=course.id,
        status=course.status.value if hasattr(course.status, "value") else str(course.status),
        created_at=course.created_at,
        finished_at=course.finished_at,
        error=course.error,
        result=result,
    )


@router.get("/courses/{course_id}/stream")
async def attach_to_course_stream(
    course_id: int,
    current_user=Depends(get_current_jarvis_user),
):
    """Attach to an existing course's event stream.

    For RUNNING courses: Streams events via SSE as they occur.
    For completed courses: Returns a single completion event and closes.

    This enables course reconnection after page refresh.

    Args:
        course_id: ID of the course to attach to
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        EventSourceResponse for SSE streaming
    """
    from zerg.database import db_session

    # CRITICAL: Use SHORT-LIVED session for security check and data retrieval
    # Don't use Depends(get_db) - it holds the session open for the entire
    # SSE stream duration, blocking TRUNCATE during E2E resets.
    with db_session() as db:
        # Multi-tenant security: only return courses owned by the current user
        course = (
            db.query(Course)
            .join(Fiche, Fiche.id == Course.fiche_id)
            .filter(Course.id == course_id)
            .filter(Fiche.owner_id == current_user.id)
            .first()
        )

        if not course:
            raise HTTPException(status_code=404, detail="Course not found")

        # Capture all values we need before session closes
        course_id_val = course.id
        course_status = course.status
        course_error = course.error
        course_finished_at = course.finished_at
        thread_id = course.thread_id

        # For completed runs, get result now (while session is open)
        result = None
        if course_status in (CourseStatus.SUCCESS, CourseStatus.FAILED):
            result = _get_last_assistant_message(db, thread_id)
    # Session is now closed - no DB connection held during streaming

    # Check course status
    if course_status == CourseStatus.RUNNING:
        # Stream live events using existing stream_course_events
        from zerg.routers.jarvis_sse import stream_course_events

        return EventSourceResponse(
            stream_course_events(
                course_id=course_id_val,
                owner_id=current_user.id,
            )
        )
    else:
        # Course is complete/failed - return single completion event and close
        async def completed_stream():
            # Send completion event matching the format from jarvis_sse.py
            event_type = "concierge_complete" if course_status == CourseStatus.SUCCESS else "error"
            payload = {
                "course_id": course_id_val,
                "status": course_status.value,
                "result": result,
                "error": course_error,
                "finished_at": course_finished_at.isoformat() if course_finished_at else None,
            }

            yield {
                "event": event_type,
                "data": json.dumps(
                    {
                        "type": event_type,
                        "payload": payload,
                        "timestamp": datetime.now().isoformat(),
                    }
                ),
            }

        return EventSourceResponse(completed_stream())


class CourseEventRecord(BaseModel):
    """Single event from a course."""

    id: int
    event_type: str
    payload: Dict[str, Any]
    created_at: datetime


class CourseEventsResponse(BaseModel):
    """Response for course events query."""

    course_id: int
    events: List[CourseEventRecord]
    total: int


@router.get("/courses/{course_id}/events", response_model=CourseEventsResponse)
def get_course_events(
    course_id: int,
    event_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> CourseEventsResponse:
    """Get events for a specific course.

    Returns events stored during course execution, optionally filtered by type.
    This endpoint is useful for E2E testing to verify tool calls and lifecycle events.

    Args:
        course_id: ID of the course to query
        event_type: Optional filter by event type (e.g., "concierge_tool_started")
        limit: Maximum number of events to return (default 100)
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        List of events for the course

    Raises:
        HTTPException: 404 if course not found or not owned by user
    """
    # Multi-tenant security: only return courses owned by the current user
    course = (
        db.query(Course)
        .join(Fiche, Fiche.id == Course.fiche_id)
        .filter(Course.id == course_id)
        .filter(Fiche.owner_id == current_user.id)
        .first()
    )

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Query events
    query = db.query(CourseEvent).filter(CourseEvent.course_id == course_id)

    if event_type:
        query = query.filter(CourseEvent.event_type == event_type)

    events = query.order_by(CourseEvent.created_at).limit(limit).all()

    return CourseEventsResponse(
        course_id=course_id,
        events=[
            CourseEventRecord(
                id=event.id,
                event_type=event.event_type,
                payload=event.payload or {},
                created_at=event.created_at,
            )
            for event in events
        ],
        total=len(events),
    )


class TimelineEvent(BaseModel):
    """Single event in a timeline with timing information."""

    phase: str
    timestamp: str  # ISO 8601
    offset_ms: int
    metadata: Optional[Dict[str, Any]] = None


class TimelineSummary(BaseModel):
    """Timing summary for a course."""

    total_duration_ms: int
    concierge_thinking_ms: Optional[int] = None
    commis_execution_ms: Optional[int] = None
    tool_execution_ms: Optional[int] = None


class TimelineResponse(BaseModel):
    """Full timeline response for a course."""

    correlation_id: Optional[str]
    course_id: int
    events: List[TimelineEvent]
    summary: TimelineSummary


@router.get("/courses/{course_id}/timeline", response_model=TimelineResponse)
def get_course_timeline(
    course_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> TimelineResponse:
    """Get timing timeline for a specific course.

    Returns structured timing data with phase-based events and summary statistics.
    This endpoint powers performance profiling and observability for Jarvis chat.

    Args:
        course_id: ID of the course to query
        db: Database session
        current_user: Authenticated user (multi-tenant filtered)

    Returns:
        Timeline with events and timing summary

    Raises:
        HTTPException: 404 if course not found or not owned by user
    """
    # Multi-tenant security: only return courses owned by the current user
    course = (
        db.query(Course)
        .join(Fiche, Fiche.id == Course.fiche_id)
        .filter(Course.id == course_id)
        .filter(Fiche.owner_id == current_user.id)
        .first()
    )

    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    # Query all events for this course, ordered by created_at
    events = db.query(CourseEvent).filter(CourseEvent.course_id == course_id).order_by(CourseEvent.created_at).all()

    if not events:
        # No events yet - return empty timeline
        return TimelineResponse(
            correlation_id=course.correlation_id,
            course_id=course_id,
            events=[],
            summary=TimelineSummary(total_duration_ms=0),
        )

    # Calculate offsets from first event
    first_timestamp = events[0].created_at
    timeline_events = []

    for event in events:
        offset_ms = int((event.created_at - first_timestamp).total_seconds() * 1000)
        timeline_events.append(
            TimelineEvent(
                phase=event.event_type,
                timestamp=event.created_at.isoformat(),
                offset_ms=offset_ms,
                metadata=event.payload if event.payload else None,
            )
        )

    # Calculate summary statistics
    last_timestamp = events[-1].created_at
    total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

    # Find key phase transitions for summary
    concierge_started_time: Optional[datetime] = None
    concierge_complete_time: Optional[datetime] = None
    commis_spawned_time: Optional[datetime] = None
    commis_complete_time: Optional[datetime] = None
    first_tool_time: Optional[datetime] = None
    last_tool_time: Optional[datetime] = None

    for event in events:
        if event.event_type == "concierge_started" and not concierge_started_time:
            concierge_started_time = event.created_at
        elif event.event_type == "concierge_complete" and not concierge_complete_time:
            concierge_complete_time = event.created_at
        elif event.event_type == "commis_spawned" and not commis_spawned_time:
            commis_spawned_time = event.created_at
        elif event.event_type == "commis_complete" and not commis_complete_time:
            commis_complete_time = event.created_at
        elif event.event_type == "tool_started" and not first_tool_time:
            first_tool_time = event.created_at
        elif event.event_type in ("tool_completed", "tool_failed"):
            last_tool_time = event.created_at

    # Calculate derived metrics
    concierge_thinking_ms = None
    if concierge_started_time and commis_spawned_time:
        concierge_thinking_ms = int((commis_spawned_time - concierge_started_time).total_seconds() * 1000)

    commis_execution_ms = None
    if commis_spawned_time and commis_complete_time:
        commis_execution_ms = int((commis_complete_time - commis_spawned_time).total_seconds() * 1000)

    tool_execution_ms = None
    if first_tool_time and last_tool_time:
        tool_execution_ms = int((last_tool_time - first_tool_time).total_seconds() * 1000)

    summary = TimelineSummary(
        total_duration_ms=total_duration_ms,
        concierge_thinking_ms=concierge_thinking_ms,
        commis_execution_ms=commis_execution_ms,
        tool_execution_ms=tool_execution_ms,
    )

    return TimelineResponse(
        correlation_id=course.correlation_id,
        course_id=course_id,
        events=timeline_events,
        summary=summary,
    )
