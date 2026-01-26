"""CRUD operations for Fiches."""

from datetime import datetime
from typing import Any
from typing import Dict
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from zerg.models import CommisJob
from zerg.models import Course
from zerg.models import Fiche
from zerg.models import FicheMessage
from zerg.models import Thread
from zerg.models import ThreadMessage
from zerg.models import Trigger
from zerg.utils.time import utc_now_naive


def _validate_cron_or_raise(expr: Optional[str]):
    """Raise ``ValueError`` if *expr* is not a valid crontab string."""

    if expr is None:
        return

    try:
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid cron expression: {expr} ({exc})") from exc


def get_fiches(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 100,
    owner_id: Optional[int] = None,
):
    """Return a list of fiches.

    If *owner_id* is provided the result is limited to fiches owned by that
    user.  Otherwise all fiches are returned (paginated).
    """

    # Eager-load relationships that the Pydantic ``Fiche`` response model
    # serialises (``owner`` and ``messages``) so that FastAPI's response
    # rendering still works *after* the request-scoped SQLAlchemy Session is
    # closed.  Without this the lazy relationship access attempts to perform a
    # new query on a detached instance which raises ``DetachedInstanceError``
    # and bubbles up as a ``ResponseValidationError``.

    # Always use selectinload to avoid detached instance errors
    query = db.query(Fiche).options(
        selectinload(Fiche.owner),
        selectinload(Fiche.messages),
    )
    if owner_id is not None:
        query = query.filter(Fiche.owner_id == owner_id)

    return query.offset(skip).limit(limit).all()


def get_fiche(db: Session, fiche_id: int):
    """Get a single fiche by ID"""
    return (
        db.query(Fiche)
        .options(
            selectinload(Fiche.owner),
            selectinload(Fiche.messages),
        )
        .filter(Fiche.id == fiche_id)
        .first()
    )


def create_fiche(
    db: Session,
    *,
    owner_id: int,
    name: Optional[str] = None,
    system_instructions: str,
    task_instructions: str,
    model: str,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Create a new fiche.

    ``owner_id`` is **required** – every fiche belongs to exactly one user.
    ``name`` defaults to "New Fiche" if not provided.
    """

    # Validate cron expression if provided
    _validate_cron_or_raise(schedule)

    # Create fiche
    db_fiche = Fiche(
        owner_id=owner_id,
        name=name or "New Fiche",
        system_instructions=system_instructions,
        task_instructions=task_instructions,
        model=model,
        status="idle",
        schedule=schedule,
        config=config,
        next_course_at=None,
        last_course_at=None,
    )
    db.add(db_fiche)
    db.commit()
    db.refresh(db_fiche)

    # Force load relationships to avoid detached instance errors
    _ = db_fiche.owner
    _ = db_fiche.messages

    return db_fiche


def update_fiche(
    db: Session,
    fiche_id: int,
    name: Optional[str] = None,
    system_instructions: Optional[str] = None,
    task_instructions: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[list] = None,
    next_course_at: Optional[datetime] = None,
    last_course_at: Optional[datetime] = None,
    last_error: Optional[str] = None,
):
    """Update an existing fiche"""
    db_fiche = db.query(Fiche).filter(Fiche.id == fiche_id).first()
    if db_fiche is None:
        return None

    # Update provided fields
    if name is not None:
        db_fiche.name = name
    if system_instructions is not None:
        db_fiche.system_instructions = system_instructions
    if task_instructions is not None:
        db_fiche.task_instructions = task_instructions
    if model is not None:
        db_fiche.model = model
    if status is not None:
        db_fiche.status = status
    if schedule is not None:
        _validate_cron_or_raise(schedule)
        db_fiche.schedule = schedule
    if config is not None:
        db_fiche.config = config
    if allowed_tools is not None:
        db_fiche.allowed_tools = allowed_tools
    if next_course_at is not None:
        db_fiche.next_course_at = next_course_at
    if last_course_at is not None:
        db_fiche.last_course_at = last_course_at
    if last_error is not None:
        db_fiche.last_error = last_error

    db_fiche.updated_at = utc_now_naive()
    db.commit()
    db.refresh(db_fiche)
    return db_fiche


def delete_fiche(db: Session, fiche_id: int):
    """Delete a fiche and all dependent rows.

    NOTE: In production (Postgres), an Fiche can be referenced by:
    - threads / thread_messages
    - courses (and commis_jobs.concierge_course_id)
    - fiche_messages (legacy)
    - triggers
    Deleting the Fiche row directly can violate FK constraints, especially for
    temporary commis fiches that create threads/messages during execution.
    """
    exists = db.query(Fiche.id).filter(Fiche.id == fiche_id).first()
    if exists is None:
        return False

    # Triggers are linked via backref and do not cascade by default.
    db.query(Trigger).filter(Trigger.fiche_id == fiche_id).delete(synchronize_session=False)

    # Runs must be deleted before threads (Course.thread_id FK).
    course_ids = [row[0] for row in db.query(Course.id).filter(Course.fiche_id == fiche_id).all()]
    if course_ids:
        # Commis jobs may reference concierge runs; preserve jobs but remove correlation.
        db.query(CommisJob).filter(CommisJob.concierge_course_id.in_(course_ids)).update(
            {CommisJob.concierge_course_id: None},
            synchronize_session="fetch",
        )
        db.query(Course).filter(Course.id.in_(course_ids)).delete(synchronize_session=False)

    # Delete thread messages + threads for this fiche.
    thread_ids = [row[0] for row in db.query(Thread.id).filter(Thread.fiche_id == fiche_id).all()]
    if thread_ids:
        db.query(ThreadMessage).filter(ThreadMessage.thread_id.in_(thread_ids)).delete(synchronize_session=False)
        db.query(Thread).filter(Thread.id.in_(thread_ids)).delete(synchronize_session=False)

    # Legacy fiche_messages table.
    db.query(FicheMessage).filter(FicheMessage.fiche_id == fiche_id).delete(synchronize_session=False)

    # Finally delete the fiche itself.
    db.query(Fiche).filter(Fiche.id == fiche_id).delete(synchronize_session=False)
    db.commit()
    return True


def get_fiche_messages(db: Session, fiche_id: int, skip: int = 0, limit: int = 100):
    """Get all messages for a specific fiche"""
    return db.query(FicheMessage).filter(FicheMessage.fiche_id == fiche_id).order_by(FicheMessage.timestamp).offset(skip).limit(limit).all()


def create_fiche_message(db: Session, fiche_id: int, role: str, content: str):
    """Create a new message for a fiche"""
    db_message = FicheMessage(fiche_id=fiche_id, role=role, content=content)
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message
