"""CRUD operations for Courses."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models import Course
from zerg.models import Fiche
from zerg.models import ThreadMessage
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.utils.time import utc_now_naive


def create_course(
    db: Session,
    *,
    fiche_id: int,
    thread_id: int,
    trigger: str = "manual",
    status: str = "queued",
    trace_id: str | uuid.UUID | None = None,
) -> Course:
    """Insert a new *Course* row.

    Minimal helper to keep service layers free from SQLAlchemy internals.
    """

    # Validate trigger and status enum values
    try:
        trigger_enum = CourseTrigger(trigger)
    except ValueError:
        raise ValueError(f"Invalid course trigger: {trigger}")
    try:
        status_enum = CourseStatus(status)
    except ValueError:
        raise ValueError(f"Invalid course status: {status}")
    resolved_trace_id = trace_id
    if resolved_trace_id is None:
        resolved_trace_id = uuid.uuid4()
    elif isinstance(resolved_trace_id, str):
        resolved_trace_id = uuid.UUID(resolved_trace_id)

    course_row = Course(
        fiche_id=fiche_id,
        thread_id=thread_id,
        trigger=trigger_enum,
        status=status_enum,
        trace_id=resolved_trace_id,
    )
    db.add(course_row)
    db.commit()
    db.refresh(course_row)
    return course_row


def mark_course_running(db: Session, course_id: int, *, started_at: Optional[datetime] = None) -> Optional[Course]:
    row = db.query(Course).filter(Course.id == course_id).first()
    if row is None:
        return None

    started_at = started_at or utc_now_naive()
    # Set to running status
    row.status = CourseStatus.RUNNING
    row.started_at = started_at
    db.commit()
    db.refresh(row)
    return row


def mark_course_finished(
    db: Session,
    course_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    total_tokens: Optional[int] = None,
    total_cost_usd: Optional[float] = None,
    summary: Optional[str] = None,
) -> Optional[Course]:
    row = db.query(Course).filter(Course.id == course_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # If no summary provided, extract from thread's first assistant message
    if summary is None and row.thread_id:
        summary = _extract_course_summary(db, row.thread_id)
        if summary:
            import logging

            logger = logging.getLogger(__name__)
            logger.info(f"Auto-extracted summary for course {course_id}: {summary[:100]}...")
        else:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"No summary extracted for course {course_id} (thread {row.thread_id})")

    # Set to success status
    row.status = CourseStatus.SUCCESS
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.total_tokens = total_tokens
    row.total_cost_usd = total_cost_usd
    row.summary = summary

    db.commit()
    db.refresh(row)
    return row


def _extract_course_summary(db: Session, thread_id: int, max_length: int = 500) -> str:
    """Extract summary from thread's first assistant message.

    Args:
        db: Database session
        thread_id: Thread ID to extract from
        max_length: Maximum summary length (default 500 chars)

    Returns:
        Summary text (truncated if needed) or empty string if no assistant messages
    """
    # Get first assistant message from thread
    first_assistant_msg = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .filter(ThreadMessage.role == "assistant")
        .order_by(ThreadMessage.id.asc())
        .first()
    )

    if not first_assistant_msg or not first_assistant_msg.content:
        return ""

    # Extract text content
    content = first_assistant_msg.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Handle array of content blocks (might be JSON)
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        text = " ".join(text_parts)
    elif isinstance(content, dict):
        # Handle single content block
        text = content.get("text", str(content))
    else:
        text = str(content)

    # Truncate if needed
    if len(text) > max_length:
        text = text[:max_length].strip() + "..."

    return text.strip()


def mark_course_failed(
    db: Session,
    course_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[Course]:
    row = db.query(Course).filter(Course.id == course_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # Set to failed status
    row.status = CourseStatus.FAILED
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.error = error

    db.commit()
    db.refresh(row)
    return row


def list_courses(db: Session, fiche_id: int, *, limit: int = 20, owner_id: Optional[int] = None):
    """Return the most recent courses for *fiche_id* ordered DESC by id.

    If *owner_id* is provided, the fiche must be owned by that user.
    """
    query = db.query(Course).filter(Course.fiche_id == fiche_id)
    if owner_id is not None:
        query = query.join(Fiche, Fiche.id == Course.fiche_id).filter(Fiche.owner_id == owner_id)
    return query.order_by(Course.id.desc()).limit(limit).all()
