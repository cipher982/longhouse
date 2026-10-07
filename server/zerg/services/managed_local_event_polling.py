"""Managed local session event polling and hydration.

Extracted from routers/session_chat.py -- event fetching, snapshot hydration,
and async polling for managed local turn events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session as SQLAlchemySession

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSessionBranch
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.provisional_events import durable_transcript_event_predicate

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


def latest_durable_head_event_id(db: SQLAlchemySession, session_id: UUID) -> int:
    """Latest durable transcript event id on the session's head branch.

    Managed-local turn baselines are compared against events fetched without a
    branch filter, so the head-branch scope here is what keeps a rewound branch
    from advancing the baseline past events the caller will still be shown.
    """
    head_branch_row = (
        db.query(AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id == session_id)
        .filter(AgentSessionBranch.is_head == 1)
        .order_by(AgentSessionBranch.id.desc())
        .first()
    )
    stmt = db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id == session_id)
    if head_branch_row is not None:
        stmt = stmt.filter(AgentEvent.branch_id == int(head_branch_row[0]))
    return int(stmt.filter(durable_transcript_event_predicate()).scalar() or 0)


def get_managed_local_latest_event_id(*, db_bind, session_id: UUID) -> int:
    with SQLAlchemySession(bind=db_bind) as poll_db:
        return latest_durable_head_event_id(poll_db, session_id)


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
