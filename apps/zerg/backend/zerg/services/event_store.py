"""Event store service for durable event streaming.

This module provides the core infrastructure for Resumable SSE v1 by persisting
all run events to the database. Events can be replayed on reconnect, enabling
clients to catch up on missed events without losing context.

Key features:
- Single emit path for all run events (eliminates duplicates)
- JSON validation at emit time (fail fast, not at stream time)
- Atomic sequence numbering per run
- Efficient query methods for replay and snapshot
"""

import json
import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.events.event_bus import EventType
from zerg.events.event_bus import event_bus
from zerg.models.agent_run_event import AgentRunEvent

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON serializer for objects not serializable by default json module."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


async def append_run_event(
    run_id: int,
    event_type: str,
    payload: Dict[str, Any],
) -> int:
    """Emit a run event with durable storage, opening its own DB session.

    This is the preferred way to emit run events. The function owns its DB
    lifecycle - it opens a short-lived session, persists the event, commits,
    and closes the session before returning.

    This pattern:
    - Eliminates DB session crossing async/thread boundaries
    - Prevents session leakage in contextvars
    - Ensures clean transaction boundaries per event

    Args:
        run_id: Run identifier
        event_type: Event type (supervisor_started, worker_complete, etc.)
        payload: Event data (must be JSON-serializable)

    Returns:
        event_id: Database ID of the persisted event

    Raises:
        ValueError: If payload is not JSON-serializable
    """
    from zerg.database import db_session

    # 1. Validate JSON serializability (fail fast)
    try:
        json_payload = jsonable_encoder(payload)
        json.dumps(json_payload, default=_json_default)
    except (TypeError, ValueError, RecursionError) as e:
        logger.error(f"Event payload not JSON-serializable for {event_type}: {e}")
        raise ValueError(f"Invalid event payload for {event_type}: {e}") from e

    # 2. Insert into database using a SHORT-LIVED session
    event_id: int
    with db_session() as db:
        event = AgentRunEvent(
            run_id=run_id,
            event_type=event_type,
            payload=json_payload,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_id = event.id
    # Session is now closed - no lingering DB connections

    # 3. Publish to live subscribers
    publish_payload = {**json_payload, "run_id": run_id, "event_type": event_type, "event_id": event_id}
    try:
        await event_bus.publish(EventType(event_type), publish_payload)
    except ValueError:
        logger.warning(f"Event type {event_type} not in EventType enum, skipping event_bus publish")

    if event_type != "supervisor_token":
        logger.debug(f"Emitted {event_type} (id={event_id}) for run {run_id}")

    return event_id


async def emit_run_event(
    db: Session,
    run_id: int,
    event_type: str,
    payload: Dict[str, Any],
) -> int:
    """Emit a run event with durable storage (legacy - uses provided session).

    DEPRECATED: Prefer append_run_event() which manages its own DB session.
    This function is kept for backwards compatibility during migration.

    Args:
        db: Database session (DEPRECATED - will be removed)
        run_id: Run identifier
        event_type: Event type (supervisor_started, worker_complete, etc.)
        payload: Event data (must be JSON-serializable)

    Returns:
        event_id: Database ID of the persisted event

    Raises:
        ValueError: If payload is not JSON-serializable
    """
    # 1. Validate JSON serializability (fail fast)
    try:
        # Use fastapi.encoders.jsonable_encoder to handle Pydantic models and other types
        json_payload = jsonable_encoder(payload)
        # Verify it's actually JSON-serializable
        json.dumps(json_payload, default=_json_default)
    except (TypeError, ValueError, RecursionError) as e:
        logger.error(f"Event payload not JSON-serializable for {event_type}: {e}")
        raise ValueError(f"Invalid event payload for {event_type}: {e}") from e

    # 2. Insert into database (id auto-increments, no sequence needed)
    event = AgentRunEvent(
        run_id=run_id,
        event_type=event_type,
        payload=json_payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    # 4. Publish to live subscribers (for backward compatibility with existing SSE)
    # Add run_id, event_type, and event_id to the payload
    # event_id is critical for the new resumable stream to avoid duplicates
    publish_payload = {**json_payload, "run_id": run_id, "event_type": event_type, "event_id": event.id}
    try:
        await event_bus.publish(EventType(event_type), publish_payload)
    except ValueError:
        # Event type not in EventType enum - log but don't fail
        # This allows for custom event types not yet in the enum
        logger.warning(f"Event type {event_type} not in EventType enum, skipping event_bus publish")

    # Only log non-token events to avoid spam
    if event_type != "supervisor_token":
        logger.debug(f"Emitted {event_type} (id={event.id}) for run {run_id}")

    return event.id


class EventStore:
    """Service for querying persisted run events."""

    @staticmethod
    def get_events_after(
        db: Session,
        run_id: int,
        after_id: int = 0,
        include_tokens: bool = True,
    ) -> List[AgentRunEvent]:
        """Get events for a run after a specific event ID.

        Args:
            db: Database session
            run_id: Run identifier
            after_id: Return events with ID > this value (0 = all events)
            include_tokens: Whether to include SUPERVISOR_TOKEN events

        Returns:
            List of events ordered by id
        """
        query = db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id)

        if after_id > 0:
            query = query.filter(AgentRunEvent.id > after_id)

        if not include_tokens:
            query = query.filter(AgentRunEvent.event_type != "supervisor_token")

        return query.order_by(AgentRunEvent.id).all()

    @staticmethod
    def get_latest_event_id(db: Session, run_id: int) -> Optional[int]:
        """Get the latest event ID for a run (for snapshot/checkpoint).

        Args:
            db: Database session
            run_id: Run identifier

        Returns:
            Latest event ID or None if no events exist
        """
        result = db.query(func.max(AgentRunEvent.id)).filter(AgentRunEvent.run_id == run_id).scalar()

        return result

    @staticmethod
    def delete_events_for_run(db: Session, run_id: int) -> int:
        """Delete all events for a run (for cleanup/testing).

        Args:
            db: Database session
            run_id: Run identifier

        Returns:
            Number of events deleted
        """
        count = db.query(AgentRunEvent).filter(AgentRunEvent.run_id == run_id).delete()
        db.commit()
        return count

    @staticmethod
    def get_event_count(db: Session, run_id: int, event_type: Optional[str] = None) -> int:
        """Get count of events for a run, optionally filtered by type.

        Args:
            db: Database session
            run_id: Run identifier
            event_type: Optional event type filter

        Returns:
            Number of events matching criteria
        """
        query = db.query(func.count(AgentRunEvent.id)).filter(AgentRunEvent.run_id == run_id)

        if event_type:
            query = query.filter(AgentRunEvent.event_type == event_type)

        return query.scalar() or 0
