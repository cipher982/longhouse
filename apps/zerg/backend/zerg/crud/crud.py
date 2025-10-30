# UTC helper
# Keep stdlib ``datetime`` for type annotations; runtime *now()* comes from
# ``utc_now``.
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone

# Standard library typing helpers
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

# Cron validation helper
from apscheduler.triggers.cron import CronTrigger
from zerg.models.enums import RunStatus
from zerg.models.models import Agent
from zerg.models.models import AgentMessage
from zerg.models.models import AgentRun

# Canvas layout model (Phase-B)
from zerg.models.models import CanvasLayout
from zerg.models.models import Connector
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.models.models import Trigger

# Added for authentication
# NOTE: For return type hints we avoid the newer *PEP 604* union syntax
# ``User | None`` because the SQLAlchemy DeclarativeMeta proxy that backs the
# ``User`` model overrides the bitwise OR operator which leads to a run-time
# ``TypeError`` when the annotation is evaluated during module import on
# Python 3.13.  Using the classic ``Optional[User]`` sidesteps the issue
# without requiring ``from __future__ import annotations``.
from zerg.models.models import User
from zerg.schemas.schemas import RunTrigger
from zerg.utils.time import utc_now_naive


# Our minimum runtime is Python 3.12 so the PEP-604 ``T | None`` syntax is
# available everywhere, but we still use ``Optional`` in a few places to keep
# the signatures short when multiple union members would otherwise be
# required.
# Runtime targets Python ≥3.12 so PEP-604 union syntax is fine.
# Python <3.10 does not support PEP-604 union for `None` at *runtime* inside
def _validate_cron_or_raise(expr: Optional[str]):
    """Raise ``ValueError`` if *expr* is not a valid crontab string."""

    if expr is None:
        return

    try:
        CronTrigger.from_crontab(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Invalid cron expression: {expr} ({exc})") from exc


# Agent CRUD operations


def get_agents(
    db: Session,
    *,
    skip: int = 0,
    limit: int = 100,
    owner_id: Optional[int] = None,
):
    """Return a list of agents.

    If *owner_id* is provided the result is limited to agents owned by that
    user.  Otherwise all agents are returned (paginated).
    """

    # Eager-load relationships that the Pydantic ``Agent`` response model
    # serialises (``owner`` and ``messages``) so that FastAPI's response
    # rendering still works *after* the request-scoped SQLAlchemy Session is
    # closed.  Without this the lazy relationship access attempts to perform a
    # new query on a detached instance which raises ``DetachedInstanceError``
    # and bubbles up as a ``ResponseValidationError``.

    # Always use selectinload to avoid detached instance errors
    query = db.query(Agent).options(
        selectinload(Agent.owner),
        selectinload(Agent.messages),
    )
    if owner_id is not None:
        query = query.filter(Agent.owner_id == owner_id)

    return query.offset(skip).limit(limit).all()


def get_agent(db: Session, agent_id: int):
    """Get a single agent by ID"""
    return (
        db.query(Agent)
        .options(
            selectinload(Agent.owner),
            selectinload(Agent.messages),
        )
        .filter(Agent.id == agent_id)
        .first()
    )


def create_agent(
    db: Session,
    *,
    owner_id: int,
    name: str,
    system_instructions: str,
    task_instructions: str,
    model: str,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Create a new agent row and persist it.

    ``owner_id`` is **required** – every agent belongs to exactly one user.
    """

    # Validate cron expression if provided
    _validate_cron_or_raise(schedule)

    db_agent = Agent(
        owner_id=owner_id,
        name=name,
        system_instructions=system_instructions,
        task_instructions=task_instructions,
        model=model,
        status="idle",
        schedule=schedule,
        config=config,
        next_run_at=None,
        last_run_at=None,
    )
    db.add(db_agent)
    db.commit()
    db.refresh(db_agent)

    # Force load relationships to avoid detached instance errors
    # This ensures they're available even after session closes
    _ = db_agent.owner  # Load owner relationship
    _ = db_agent.messages  # Load messages relationship (should be empty for new agent)

    return db_agent


def update_agent(
    db: Session,
    agent_id: int,
    name: Optional[str] = None,
    system_instructions: Optional[str] = None,
    task_instructions: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    schedule: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    allowed_tools: Optional[list] = None,
    next_run_at: Optional[datetime] = None,
    last_run_at: Optional[datetime] = None,
    last_error: Optional[str] = None,
):
    """Update an existing agent"""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if db_agent is None:
        return None

    # Update provided fields
    if name is not None:
        db_agent.name = name
    if system_instructions is not None:
        db_agent.system_instructions = system_instructions
    if task_instructions is not None:
        db_agent.task_instructions = task_instructions
    if model is not None:
        db_agent.model = model
    if status is not None:
        db_agent.status = status
    if schedule is not None:
        _validate_cron_or_raise(schedule)
        db_agent.schedule = schedule
    if config is not None:
        db_agent.config = config
    if allowed_tools is not None:
        db_agent.allowed_tools = allowed_tools
    if next_run_at is not None:
        db_agent.next_run_at = next_run_at
    if last_run_at is not None:
        db_agent.last_run_at = last_run_at
    if last_error is not None:
        db_agent.last_error = last_error

    db_agent.updated_at = utc_now_naive()
    db.commit()
    db.refresh(db_agent)
    return db_agent


def delete_agent(db: Session, agent_id: int):
    """Delete an agent and all its messages"""
    db_agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if db_agent is None:
        return False
    db.delete(db_agent)
    db.commit()
    return True


# ------------------------------------------------------------
# User CRUD operations (Stage 1 – Auth MVP)
# ------------------------------------------------------------


def get_user(db: Session, user_id: int) -> Optional[User]:
    """Return user by primary key."""
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    """Return user by e-mail address (case-insensitive)."""
    return (
        db.query(User)
        .filter(User.email.ilike(email))  # type: ignore[arg-type]
        .first()
    )


def count_users(db: Session) -> int:
    """Return total number of users in the system."""
    return db.query(User).count()


def create_user(
    db: Session,
    *,
    email: str,
    provider: Optional[str] = None,
    provider_user_id: Optional[str] = None,
    role: str = "USER",
) -> User:
    """Insert new user row.

    Caller is expected to ensure uniqueness beforehand; we do not upsert here.
    """
    new_user = User(
        email=email,
        provider=provider,
        provider_user_id=provider_user_id,
        role=role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Send Discord notification for new user signup
    import asyncio
    import threading

    from zerg.services.ops_discord import send_user_signup_alert

    # Get total user count for the notification
    try:
        total_users = count_users(db)
    except Exception:
        total_users = None

    # Fire-and-forget Discord notification in background thread
    def _send_discord_notification():
        try:
            asyncio.run(send_user_signup_alert(email, total_users))
        except Exception:
            # Don't fail user creation if Discord notification fails
            pass

    try:
        threading.Thread(target=_send_discord_notification, daemon=True).start()
    except Exception:
        # Don't fail user creation if Discord notification fails
        pass

    return new_user


# ------------------------------------------------------------
# User update helper (Stage 2 – profile editing)
# ------------------------------------------------------------


def update_user(
    db: Session,
    user_id: int,
    *,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    prefs: Optional[Dict[str, Any]] = None,
    gmail_refresh_token: Optional[str] = None,
) -> Optional[User]:
    """Partial update for the *User* table.

    Only the provided fields are modified – `None` leaves the column unchanged.
    Returns the updated user row or ``None`` if the record was not found.
    """

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return None

    if display_name is not None:
        user.display_name = display_name
    if avatar_url is not None:
        user.avatar_url = avatar_url
    if prefs is not None:
        user.prefs = prefs
    if gmail_refresh_token is not None:
        from zerg.utils import crypto  # local import to avoid top-level dependency in non-auth paths

        user.gmail_refresh_token = crypto.encrypt(gmail_refresh_token)

    db.commit()
    db.refresh(user)
    return user


# ------------------------------------------------------------
# Trigger CRUD operations
# ------------------------------------------------------------


def create_trigger(
    db: Session,
    *,
    agent_id: int,
    trigger_type: str = "webhook",
    secret: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """Create a new trigger for an agent.

    The **secret** is only relevant for webhook triggers.  For future trigger
    types we still persist a secret so external systems can authenticate when
    hitting the generic `/events` endpoint (or skip when not applicable).
    """

    from uuid import uuid4

    if secret is None:
        secret = uuid4().hex

    db_trigger = Trigger(
        agent_id=agent_id,
        type=trigger_type,
        secret=secret,
        config=config,
    )
    db.add(db_trigger)
    db.commit()
    db.refresh(db_trigger)
    return db_trigger


def get_trigger(db: Session, trigger_id: int):
    return db.query(Trigger).filter(Trigger.id == trigger_id).first()


def delete_trigger(db: Session, trigger_id: int):
    trg = get_trigger(db, trigger_id)
    if trg is None:
        return False
    db.delete(trg)
    db.commit()
    return True


def get_triggers(db: Session, agent_id: Optional[int] = None) -> List[Trigger]:
    """
    Retrieve triggers, optionally filtered by agent_id.
    """
    query = db.query(Trigger)
    if agent_id is not None:
        query = query.filter(Trigger.agent_id == agent_id)
    return query.order_by(Trigger.id).all()


# ------------------------------------------------------------
# Connector CRUD operations
# ------------------------------------------------------------


def create_connector(
    db: Session,
    *,
    owner_id: int,
    type: str,
    provider: str,
    config: Optional[Dict[str, Any]] = None,
) -> Connector:
    connector = Connector(owner_id=owner_id, type=type, provider=provider, config=config or {})
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def get_connector(db: Session, connector_id: int) -> Optional[Connector]:
    return db.query(Connector).filter(Connector.id == connector_id).first()


def get_connectors(
    db: Session,
    *,
    owner_id: Optional[int] = None,
    type: Optional[str] = None,
    provider: Optional[str] = None,
) -> List[Connector]:
    q = db.query(Connector)
    if owner_id is not None:
        q = q.filter(Connector.owner_id == owner_id)
    if type is not None:
        q = q.filter(Connector.type == type)
    if provider is not None:
        q = q.filter(Connector.provider == provider)
    return q.order_by(Connector.id).all()


def update_connector(
    db: Session,
    connector_id: int,
    *,
    config: Optional[Dict[str, Any]] = None,
    type: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[Connector]:
    conn = get_connector(db, connector_id)
    if not conn:
        return None
    if type is not None:
        conn.type = type
    if provider is not None:
        conn.provider = provider
    if config is not None:
        conn.config = config  # type: ignore[assignment]
    db.commit()
    db.refresh(conn)
    return conn


def delete_connector(db: Session, connector_id: int) -> bool:
    conn = get_connector(db, connector_id)
    if not conn:
        return False
    db.delete(conn)
    db.commit()
    return True


# Agent Message CRUD operations
def get_agent_messages(db: Session, agent_id: int, skip: int = 0, limit: int = 100):
    """Get all messages for a specific agent"""
    return (
        db.query(AgentMessage)
        .filter(AgentMessage.agent_id == agent_id)
        .order_by(AgentMessage.timestamp)
        .offset(skip)
        .limit(limit)
        .all()
    )


def create_agent_message(db: Session, agent_id: int, role: str, content: str):
    """Create a new message for an agent"""
    db_message = AgentMessage(agent_id=agent_id, role=role, content=content)
    db.add(db_message)
    db.commit()
    db.refresh(db_message)
    return db_message


# Thread CRUD operations
def get_threads(db: Session, agent_id: Optional[int] = None, skip: int = 0, limit: int = 100):
    """Get all threads, filtered by agent_id if provided"""
    query = db.query(Thread).options(selectinload(Thread.messages))
    if agent_id is not None:
        query = query.filter(Thread.agent_id == agent_id)
    return query.offset(skip).limit(limit).all()


def get_active_thread(db: Session, agent_id: int):
    """Get the active thread for an agent, if it exists"""
    return db.query(Thread).filter(Thread.agent_id == agent_id, Thread.active).first()


def get_thread(db: Session, thread_id: int):
    """Get a specific thread by ID"""
    return db.query(Thread).options(selectinload(Thread.messages)).filter(Thread.id == thread_id).first()


def create_thread(
    db: Session,
    agent_id: int,
    title: str,
    active: bool = True,
    agent_state: Optional[Dict[str, Any]] = None,
    memory_strategy: Optional[str] = "buffer",
    thread_type: Optional[str] = "chat",
):
    """Create a new thread for an agent"""
    # If this is set as active, deactivate any other active threads
    if active:
        db.query(Thread).filter(Thread.agent_id == agent_id, Thread.active).update({"active": False})

    db_thread = Thread(
        agent_id=agent_id,
        title=title,
        active=active,
        agent_state=agent_state,
        memory_strategy=memory_strategy,
        thread_type=thread_type,
    )
    db.add(db_thread)
    db.commit()
    db.refresh(db_thread)
    return db_thread


def update_thread(
    db: Session,
    thread_id: int,
    title: Optional[str] = None,
    active: Optional[bool] = None,
    agent_state: Optional[Dict[str, Any]] = None,
    memory_strategy: Optional[str] = None,
):
    """Update a thread"""
    db_thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if db_thread is None:
        return None

    # Update provided fields
    if title is not None:
        db_thread.title = title
    if active is not None:
        if active:
            # Deactivate other threads for this agent
            db.query(Thread).filter(Thread.agent_id == db_thread.agent_id, Thread.id != thread_id).update(
                {"active": False}
            )
        db_thread.active = active
    if agent_state is not None:
        db_thread.agent_state = agent_state
    if memory_strategy is not None:
        db_thread.memory_strategy = memory_strategy

    db_thread.updated_at = utc_now_naive()
    db.commit()
    db.refresh(db_thread)
    return db_thread


def delete_thread(db: Session, thread_id: int):
    """Delete a thread and all its messages"""
    db_thread = db.query(Thread).filter(Thread.id == thread_id).first()
    if db_thread is None:
        return False
    db.delete(db_thread)
    db.commit()
    return True


# Thread Message CRUD operations
def get_thread_messages(db: Session, thread_id: int, skip: int = 0, limit: int = 100):
    """
    Get all messages for a specific thread, ordered strictly by database ID.
    
    IMPORTANT: This function returns messages ordered by ThreadMessage.id (insertion order).
    This ordering is authoritative and must be preserved by clients. The client MUST NOT
    sort these messages client-side; the server ordering is the source of truth.
    
    Rationale: Use the *id* column for deterministic chronological ordering. SQLite
    timestamps have a resolution of 1 second which can lead to two messages inserted within
    the same second being returned in undefined order if sorted by timestamp. The
    auto-incrementing primary-key is strictly monotonic, therefore provides a stable
    ordering even when multiple rows share the same timestamp.
    
    See the API endpoint documentation in zerg.routers.threads.read_thread_messages().
    """
    return (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id)
        .order_by(ThreadMessage.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def create_thread_message(
    db: Session,
    thread_id: int,
    role: str,
    content: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_call_id: Optional[str] = None,
    name: Optional[str] = None,
    processed: bool = False,
    parent_id: Optional[int] = None,
    sent_at: Optional[datetime] = None,
    *,
    commit: bool = True,
):
    """
    Create a new message for a thread.

    Args:
        sent_at: Optional client-provided send timestamp. If provided, must be within ±5 minutes
                 of server time, otherwise uses server time. Timezone-aware datetime in UTC.
    """
    # Validate and normalize sent_at
    if sent_at is not None:
        # Ensure it's timezone-aware (UTC)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=dt_timezone.utc)

        # Check it's within ±5 minutes of server time
        now_utc = datetime.now(dt_timezone.utc)
        time_diff = abs((now_utc - sent_at).total_seconds())
        if time_diff > 300:  # 5 minutes in seconds
            # Reject obviously wrong timestamps
            sent_at = now_utc
    else:
        # Use server time if not provided
        sent_at = datetime.now(dt_timezone.utc)

    db_message = ThreadMessage(
        thread_id=thread_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        name=name,
        processed=processed,
        parent_id=parent_id,
        sent_at=sent_at,
    )
    db.add(db_message)

    # For callers that batch-insert multiple messages we allow skipping the
    # commit so they can flush/commit once at the end.  When *commit* is
    # False we rely on the caller to perform a ``session.flush()`` so that
    # primary keys are assigned (required for subsequent parent_id linking).

    if commit:
        db.commit()
        db.refresh(db_message)
    else:
        # Ensure primary key is assigned so callers can reference ``row.id``
        db.flush([db_message])
    return db_message


def mark_message_processed(db: Session, message_id: int):
    """Mark a message as processed"""
    db_message = db.query(ThreadMessage).filter(ThreadMessage.id == message_id).first()
    if db_message:
        db_message.processed = True
        db.commit()
        db.refresh(db_message)
        return db_message
    return None


# ---------------------------------------------------------------------------
# Bulk helpers – performance critical paths
# ---------------------------------------------------------------------------


def mark_messages_processed_bulk(db: Session, message_ids: List[int]):
    """Set processed=True for the given message IDs in one UPDATE."""

    if not message_ids:
        return 0

    updated = (
        db.query(ThreadMessage)
        .filter(ThreadMessage.id.in_(message_ids))
        .update({ThreadMessage.processed: True}, synchronize_session=False)
    )

    db.commit()
    return updated


def get_unprocessed_messages(db: Session, thread_id: int):
    """Get unprocessed messages for a thread"""
    # ------------------------------------------------------------------
    # SQLAlchemy filter helpers
    # ------------------------------------------------------------------
    #
    # Using Python's boolean *not* operator on an InstrumentedAttribute
    # (`not ThreadMessage.processed`) evaluates the *truthiness* of the
    # attribute **eagerly** which yields a plain ``False`` value instead of a
    # SQL expression.  The resulting ``WHERE false`` clause caused the query
    # to **always** return an empty result set so the AgentRunner never saw
    # any *unprocessed* user messages – the UI therefore stayed silent after
    # every prompt.
    #
    # The correct approach is to build an explicit boolean comparison that
    # SQLAlchemy can translate into the appropriate SQL (`processed = 0`).
    # The `is_(False)` helper generates portable SQL across dialects.
    # ------------------------------------------------------------------

    return (
        db.query(ThreadMessage)
        .filter(ThreadMessage.thread_id == thread_id, ThreadMessage.processed.is_(False))
        .order_by(ThreadMessage.id)
        .all()
    )


# ---------------------------------------------------------------------------
# AgentRun CRUD helpers (Run History feature)
# ---------------------------------------------------------------------------


def create_run(
    db: Session,
    *,
    agent_id: int,
    thread_id: int,
    trigger: str = "manual",
    status: str = "queued",
) -> AgentRun:
    """Insert a new *AgentRun* row.

    Minimal helper to keep service layers free from SQLAlchemy internals.
    """

    # Validate trigger and status enum values
    try:
        trigger_enum = RunTrigger(trigger)
    except ValueError:
        raise ValueError(f"Invalid run trigger: {trigger}")
    try:
        status_enum = RunStatus(status)
    except ValueError:
        raise ValueError(f"Invalid run status: {status}")
    run_row = AgentRun(
        agent_id=agent_id,
        thread_id=thread_id,
        trigger=trigger_enum,
        status=status_enum,
    )
    db.add(run_row)
    db.commit()
    db.refresh(run_row)
    return run_row


def mark_running(db: Session, run_id: int, *, started_at: Optional[datetime] = None) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    started_at = started_at or utc_now_naive()
    # Set to running status
    row.status = RunStatus.RUNNING
    row.started_at = started_at
    db.commit()
    db.refresh(row)
    return row


def mark_finished(
    db: Session,
    run_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    total_tokens: Optional[int] = None,
    total_cost_usd: Optional[float] = None,
    summary: Optional[str] = None,
) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # If no summary provided, extract from thread's first assistant message
    if summary is None and row.thread_id:
        summary = _extract_run_summary(db, row.thread_id)
        if summary:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Auto-extracted summary for run {run_id}: {summary[:100]}...")
        else:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"No summary extracted for run {run_id} (thread {row.thread_id})")

    # Set to success status
    row.status = RunStatus.SUCCESS
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.total_tokens = total_tokens
    row.total_cost_usd = total_cost_usd
    row.summary = summary

    db.commit()
    db.refresh(row)
    return row


def _extract_run_summary(db: Session, thread_id: int, max_length: int = 500) -> str:
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


def mark_failed(
    db: Session,
    run_id: int,
    *,
    finished_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> Optional[AgentRun]:
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if row is None:
        return None

    finished_at = finished_at or utc_now_naive()
    if row.started_at and duration_ms is None:
        duration_ms = int((finished_at - row.started_at).total_seconds() * 1000)

    # Set to failed status
    row.status = RunStatus.FAILED
    row.finished_at = finished_at
    row.duration_ms = duration_ms
    row.error = error

    db.commit()
    db.refresh(row)
    return row


def list_runs(db: Session, agent_id: int, *, limit: int = 20):
    """Return the most recent runs for *agent_id* ordered DESC by id."""
    return db.query(AgentRun).filter(AgentRun.agent_id == agent_id).order_by(AgentRun.id.desc()).limit(limit).all()


# ---------------------------------------------------------------------------
# Canvas layout helpers (Phase-B)
# ---------------------------------------------------------------------------


def upsert_canvas_layout(
    db: Session,
    user_id: Optional[int],
    nodes: dict,
    viewport: Optional[dict],
    workflow_id: Optional[int] = None,
):
    """Insert **or** update the *canvas layout* for *(user_id, workflow_id)*.

    Uses database-agnostic upsert logic that works with both SQLite and PostgreSQL.
    Relies on the UNIQUE(user_id, workflow_id) constraint declared on the CanvasLayout model.
    """

    from sqlalchemy.sql import func

    if user_id is None:
        raise ValueError("upsert_canvas_layout: `user_id` must not be None, auth dependency failed?")

    # First, try to find an existing record
    existing = (
        db.query(CanvasLayout).filter(CanvasLayout.user_id == user_id, CanvasLayout.workflow_id == workflow_id).first()
    )

    if existing:
        # Update existing record
        existing.nodes_json = nodes
        existing.viewport = viewport
        existing.updated_at = func.now()
    else:
        # Create new record
        new_layout = CanvasLayout(
            user_id=user_id,
            workflow_id=workflow_id,
            nodes_json=nodes,
            viewport=viewport,
        )
        db.add(new_layout)

    db.commit()

    # Return the *current* row so callers can inspect the stored payload.
    return db.query(CanvasLayout).filter_by(user_id=user_id, workflow_id=workflow_id).first()


def get_canvas_layout(db: Session, user_id: Optional[int], workflow_id: Optional[int] = None):
    """Return the persisted canvas layout for *(user_id, workflow_id)*."""

    if user_id is None:
        return None

    return db.query(CanvasLayout).filter_by(user_id=user_id, workflow_id=workflow_id).first()


def create_workflow(
    db: Session, *, owner_id: int, name: str, description: Optional[str] = None, canvas: Dict[str, Any]
):
    """Create a new workflow."""
    from zerg.models.models import Workflow

    # Check for existing active workflow with the same name for the same owner
    existing_workflow = (
        db.query(Workflow)
        .filter(
            Workflow.owner_id == owner_id,
            Workflow.name == name,
            Workflow.is_active.is_(True),
        )
        .first()
    )
    if existing_workflow:
        raise HTTPException(status_code=409, detail="A workflow with this name already exists.")

    db_workflow = Workflow(
        owner_id=owner_id,
        name=name,
        description=description,
        canvas=canvas,
    )
    db.add(db_workflow)
    db.commit()
    db.refresh(db_workflow)
    return db_workflow


# -------------------------------------------------------------------
# Workflow helpers – list / fetch by id (active only)
# -------------------------------------------------------------------


def get_workflows(
    db: Session,
    *,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
):
    """Return active workflows owned by *owner_id*."""

    from zerg.models.models import Workflow as WorkflowModel

    return db.query(WorkflowModel).filter_by(owner_id=owner_id, is_active=True).offset(skip).limit(limit).all()


def get_workflow(db: Session, workflow_id: int):
    from zerg.models.models import Workflow as WorkflowModel

    return db.query(WorkflowModel).filter_by(id=workflow_id).first()


def get_workflow_execution(db: Session, execution_id: int):
    from zerg.models.models import WorkflowExecution

    return db.query(WorkflowExecution).filter_by(id=execution_id).first()


def get_workflow_executions(db: Session, workflow_id: int, skip: int = 0, limit: int = 100):
    from zerg.models.models import WorkflowExecution

    return db.query(WorkflowExecution).filter_by(workflow_id=workflow_id).offset(skip).limit(limit).all()


def get_waiting_execution_for_workflow(db: Session, workflow_id: int):
    """Get the first waiting execution for a workflow, if any exists."""
    from zerg.models.models import WorkflowExecution

    return db.query(WorkflowExecution).filter_by(workflow_id=workflow_id, phase="waiting").first()


def create_workflow_execution(
    db: Session, *, workflow_id: int, phase: str = "running", triggered_by: str = "manual", result: str = None
):
    """Create a new workflow execution record."""
    from datetime import datetime
    from datetime import timezone

    from zerg.models.models import WorkflowExecution

    # Validate phase/result consistency
    if phase == "finished" and result is None:
        raise ValueError("result parameter is required when phase='finished'")
    if phase != "finished" and result is not None:
        raise ValueError("result parameter should only be provided when phase='finished'")

    execution = WorkflowExecution(
        workflow_id=workflow_id,
        phase=phase,
        result=result,
        started_at=datetime.now(timezone.utc),
        triggered_by=triggered_by,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


# -------------------------------------------------------------------
# Workflow Template CRUD operations
# -------------------------------------------------------------------


def create_workflow_template(
    db: Session,
    *,
    created_by: int,
    name: str,
    description: Optional[str] = None,
    category: str,
    canvas: Dict[str, Any],
    tags: Optional[List[str]] = None,
    preview_image_url: Optional[str] = None,
    is_public: bool = True,
):
    """Create a new workflow template."""
    from zerg.models.models import WorkflowTemplate

    db_template = WorkflowTemplate(
        created_by=created_by,
        name=name,
        description=description,
        category=category,
        canvas=canvas,
        tags=tags or [],
        preview_image_url=preview_image_url,
        is_public=is_public,
    )
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template


def get_workflow_templates(
    db: Session,
    *,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    created_by: Optional[int] = None,
    public_only: bool = True,
):
    """Get workflow templates with optional filtering."""
    from zerg.models.models import WorkflowTemplate

    query = db.query(WorkflowTemplate)

    if public_only and created_by is None:
        query = query.filter(WorkflowTemplate.is_public.is_(True))
    elif created_by is not None:
        # If user is specified, show their templates regardless of public status
        query = query.filter(WorkflowTemplate.created_by == created_by)

    if category:
        query = query.filter(WorkflowTemplate.category == category)

    return query.offset(skip).limit(limit).all()


def get_workflow_template(db: Session, template_id: int):
    """Get a specific workflow template by ID."""
    from zerg.models.models import WorkflowTemplate

    return db.query(WorkflowTemplate).filter_by(id=template_id).first()


def get_workflow_template_by_name(db: Session, template_name: str):
    """Get a specific workflow template by name."""
    from zerg.models.models import WorkflowTemplate

    return db.query(WorkflowTemplate).filter_by(name=template_name, is_public=True).first()


def get_template_categories(db: Session):
    """Get all unique template categories."""
    from zerg.models.models import WorkflowTemplate

    result = db.query(WorkflowTemplate.category).distinct().all()
    return [r[0] for r in result]


def deploy_workflow_template(
    db: Session, *, template_id: int, owner_id: int, name: Optional[str] = None, description: Optional[str] = None
):
    """Deploy a template as a new workflow for the user."""
    from zerg.models.models import WorkflowTemplate

    # Get the template
    template = db.query(WorkflowTemplate).filter_by(id=template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if not template.is_public and template.created_by != owner_id:
        raise HTTPException(status_code=403, detail="Access denied to this template")

    # Create workflow from template
    workflow_name = name or f"{template.name} (Copy)"
    workflow_description = description or template.description

    return create_workflow(
        db=db, owner_id=owner_id, name=workflow_name, description=workflow_description, canvas=template.canvas
    )
