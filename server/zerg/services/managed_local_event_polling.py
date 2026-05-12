"""Managed local session event polling and hydration.

Extracted from routers/session_chat.py -- event fetching, snapshot hydration,
and async polling for managed local turn events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session as SQLAlchemySession

from zerg.models.agents import AgentEvent
from zerg.services.agents_store import AgentsStore
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_turns import get_session_turn_snapshot

logger = logging.getLogger(__name__)

MANAGED_LOCAL_EVENT_TIMEOUT_SECS = 150.0
MANAGED_LOCAL_POLL_INTERVAL_SECS = 0.1
MANAGED_LOCAL_STABLE_POLLS = 1


def fetch_managed_local_events_since(*, db_bind, session_id: UUID, after_event_id: int) -> list[AgentEvent]:
    with SQLAlchemySession(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id > after_event_id)
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def fetch_managed_local_events_between_ids(
    *,
    db_bind,
    session_id: UUID,
    start_event_id: int,
    end_event_id: int,
) -> list[AgentEvent]:
    with SQLAlchemySession(bind=db_bind) as poll_db:
        return (
            poll_db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.id >= int(start_event_id))
            .filter(AgentEvent.id <= int(end_event_id))
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )


def get_session_turn_snapshot_best_effort(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
):
    try:
        return get_session_turn_snapshot(
            db_bind=db_bind,
            session_id=session_id,
            request_id=request_id,
        )
    except SQLAlchemyTimeoutError:
        logger.warning(
            "Session turn snapshot timed out for %s; falling back to direct evidence",
            session_id,
        )
    except Exception:
        logger.warning(
            "Session turn snapshot read failed for %s; falling back to direct evidence",
            session_id,
            exc_info=True,
        )
    return None


def hydrate_turn_events_from_snapshot(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
    expected_user_message: str,
) -> tuple[object | None, list[AgentEvent]]:
    snapshot = get_session_turn_snapshot_best_effort(
        db_bind=db_bind,
        session_id=session_id,
        request_id=request_id,
    )
    if snapshot is None or snapshot.durable_at is None or snapshot.user_event_id is None or snapshot.durable_assistant_event_id is None:
        return snapshot, []

    try:
        events = fetch_managed_local_events_between_ids(
            db_bind=db_bind,
            session_id=session_id,
            start_event_id=int(snapshot.user_event_id),
            end_event_id=int(snapshot.durable_assistant_event_id),
        )
    except SQLAlchemyTimeoutError:
        logger.warning(
            "Session turn event hydration timed out for %s; falling back to direct evidence",
            session_id,
        )
        return snapshot, []
    except Exception:
        logger.warning(
            "Session turn event hydration failed for %s; falling back to direct evidence",
            session_id,
            exc_info=True,
        )
        return snapshot, []
    if expected_user_message and not managed_local_events_include_expected_turn(
        events=events,
        expected_user_message=expected_user_message,
    ):
        return snapshot, []
    return snapshot, events


def get_managed_local_latest_event_id(*, db_bind, session_id: UUID) -> int:
    with SQLAlchemySession(bind=db_bind) as poll_db:
        latest = AgentsStore(poll_db).get_latest_event_id(session_id)
        return int(latest or 0)


def managed_local_events_include_expected_turn(*, events: list[AgentEvent], expected_user_message: str) -> bool:
    saw_expected_user_prompt = False

    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")
        tool_name = str(getattr(event, "tool_name", "") or "").strip()
        if role == "user" and strip_claude_channel_wrapper(content_text) == expected_user_message:
            saw_expected_user_prompt = True
            continue
        if not saw_expected_user_prompt:
            continue
        if tool_name:
            return True
        if role == "assistant" and content_text.strip():
            return True

    return False


async def await_managed_local_turn_events(
    *,
    db_bind,
    session_id: UUID,
    after_event_id: int,
    expected_user_message: str | None = None,
    timeout_secs: float = MANAGED_LOCAL_EVENT_TIMEOUT_SECS,
    poll_interval_secs: float = MANAGED_LOCAL_POLL_INTERVAL_SECS,
) -> list[AgentEvent]:
    deadline = time.monotonic() + timeout_secs
    latest_seen = after_event_id
    stable_polls = 0
    saw_pool_timeout = False

    while time.monotonic() < deadline:
        try:
            latest_event_id = get_managed_local_latest_event_id(db_bind=db_bind, session_id=session_id)
            if latest_event_id > after_event_id:
                if latest_event_id == latest_seen:
                    stable_polls += 1
                else:
                    latest_seen = latest_event_id
                    stable_polls = 0

                if stable_polls >= MANAGED_LOCAL_STABLE_POLLS:
                    events = fetch_managed_local_events_since(
                        db_bind=db_bind,
                        session_id=session_id,
                        after_event_id=after_event_id,
                    )
                    if expected_user_message and not managed_local_events_include_expected_turn(
                        events=events,
                        expected_user_message=expected_user_message,
                    ):
                        await asyncio.sleep(poll_interval_secs)
                        continue
                    return events
        except SQLAlchemyTimeoutError:
            if not saw_pool_timeout:
                logger.warning(
                    "Managed-local event poll for %s timed out waiting for a DB connection; retrying",
                    session_id,
                )
                saw_pool_timeout = True

        await asyncio.sleep(poll_interval_secs)

    return []


async def await_managed_local_events_task(
    events_task: asyncio.Task[list[AgentEvent]],
    *,
    timeout_secs: float,
) -> list[AgentEvent]:
    if events_task.done():
        return events_task.result() or []
    try:
        return await asyncio.wait_for(asyncio.shield(events_task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        return []


async def await_managed_local_terminal_task(
    terminal_task: asyncio.Task,
    *,
    timeout_secs: float,
):
    if terminal_task.done():
        try:
            return terminal_task.result()
        except asyncio.CancelledError:
            return None
        except Exception:
            logger.warning("Managed-local terminal waiter failed after durable events", exc_info=True)
            return None
    try:
        return await asyncio.wait_for(asyncio.shield(terminal_task), timeout=timeout_secs)
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        return None
    except Exception:
        logger.warning("Managed-local terminal waiter failed after durable events", exc_info=True)
        return None
