"""CRUD operations for Thread Messages."""

from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from sqlalchemy.orm import Session

from zerg.models.models import ThreadMessage


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
    return db.query(ThreadMessage).filter(ThreadMessage.thread_id == thread_id).order_by(ThreadMessage.id).offset(skip).limit(limit).all()


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


def mark_messages_processed_bulk(db: Session, message_ids: List[int]):
    """Set processed=True for the given message IDs in one UPDATE."""

    if not message_ids:
        return 0

    updated = (
        db.query(ThreadMessage).filter(ThreadMessage.id.in_(message_ids)).update({ThreadMessage.processed: True}, synchronize_session=False)
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
