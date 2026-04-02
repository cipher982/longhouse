"""Session coordination tools for Oikos and operator agents.

These tools expose the same durable session/message primitives that Longhouse
publishes over the machine-facing `/api/agents/*` surface, but as direct
in-process tools for Oikos and other internal agents.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

from zerg.database import db_session
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.services.agents_store import AgentsStore
from zerg.services.session_messages import MESSAGE_STATUS_DELIVERING
from zerg.services.session_messages import MESSAGE_STATUS_FAILED
from zerg.services.session_messages import MESSAGE_STATUS_QUEUED
from zerg.services.session_messages import create_session_message
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.services.session_views import load_presence_map
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.types.tools import Tool as StructuredTool

_PRESENCE_TTL = timedelta(minutes=10)


class SessionPeersInput(BaseModel):
    """Input schema for list_session_peers."""

    repo: str | None = Field(default=None, description="Repo substring to match against git_repo.")
    project: str | None = Field(default=None, description="Optional project filter.")
    days: int = Field(default=7, ge=1, le=90, description="Days to look back.")
    limit: int = Field(default=50, ge=1, le=200, description="Max peers to return.")
    active_only: bool = Field(default=True, description="Only return peers with live presence.")
    exclude_session_id: str | None = Field(default=None, description="Optional session UUID to exclude from results.")


class SessionEventsInput(BaseModel):
    """Input schema for get_session_events."""

    session_id: str = Field(description="Session UUID.")
    roles: list[str] | None = Field(default=None, description="Optional role filter.")
    tool_name: str | None = Field(default=None, description="Optional exact tool-name filter.")
    query: str | None = Field(default=None, description="Optional event content search query.")
    context_mode: str = Field(default="forensic", description="Context mode: forensic or active_context.")
    branch_mode: str = Field(default="head", description="Branch mode: head or all.")
    limit: int = Field(default=100, ge=1, le=200, description="Max events to return.")
    offset: int = Field(default=0, ge=0, description="Offset into the filtered event list.")


class SessionTailInput(BaseModel):
    """Input schema for get_session_tail."""

    session_id: str = Field(description="Session UUID.")
    limit: int = Field(default=30, ge=1, le=100, description="Max recent events to return.")


class MessageSessionInput(BaseModel):
    """Input schema for message_session."""

    from_session_id: str = Field(description="Sender session UUID.")
    to_session_id: str = Field(description="Target session UUID.")
    text: str = Field(description="Directed message body.")
    source_event_id: int | None = Field(default=None, description="Optional source event ID.")


class CheckSessionMessagesInput(BaseModel):
    """Input schema for check_session_messages."""

    session_id: str = Field(description="Session UUID to inspect inbox/outbox for.")
    direction: str = Field(default="inbound", description="Direction: inbound, outbound, or all.")
    unacknowledged_only: bool = Field(default=True, description="Only include messages without acknowledged_at.")
    limit: int = Field(default=50, ge=1, le=200, description="Max messages to return.")


class AcknowledgeSessionMessageInput(BaseModel):
    """Input schema for acknowledge_session_message."""

    message_id: int = Field(description="Numeric message ID.")
    session_id: str = Field(description="Target session UUID acknowledging the message.")


def _parse_uuid(raw: str, *, field_name: str) -> UUID | None:
    try:
        return UUID(str(raw).strip())
    except ValueError:
        return None


def _session_message_payload(message: SessionMessage) -> dict[str, Any]:
    return {
        "id": int(message.id),
        "from_session_id": str(message.from_session_id),
        "to_session_id": str(message.to_session_id),
        "text": message.body,
        "source_event_id": message.source_event_id,
        "delivery_status": message.delivery_status,
        "delivery_attempts": int(message.delivery_attempts or 0),
        "last_error": message.last_error,
        "delivered_via": message.delivered_via,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "acknowledged_at": message.acknowledged_at.isoformat() if message.acknowledged_at else None,
    }


def _session_tail_event_payload(event: AgentEvent) -> dict[str, Any]:
    content = str(event.content_text or "")[:4000]
    return {
        "id": int(event.id),
        "role": event.role,
        "content": content,
        "tool_name": event.tool_name,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
    }


def list_session_peers(
    repo: str | None = None,
    project: str | None = None,
    days: int = 7,
    limit: int = 50,
    active_only: bool = True,
    exclude_session_id: str | None = None,
) -> dict[str, Any]:
    """List recent peer sessions around a repo or project."""
    if not repo and not project:
        return tool_error(ErrorType.VALIDATION_ERROR, "Provide repo or project to scope the peer query")

    excluded_uuid: UUID | None = None
    if exclude_session_id is not None:
        excluded_uuid = _parse_uuid(exclude_session_id, field_name="exclude_session_id")
        if excluded_uuid is None:
            return tool_error(ErrorType.VALIDATION_ERROR, "exclude_session_id must be a valid UUID")

    with db_session() as db:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days)
        fetch_limit = limit * 4 if repo else limit
        sessions, _ = store.list_sessions(
            project=project,
            provider=None,
            environment=None,
            include_test=False,
            device_id=None,
            since=since,
            query=None,
            limit=fetch_limit,
            offset=0,
            anchor_on_activity=True,
        )

        if repo:
            repo_lower = repo.lower()
            sessions = [session for session in sessions if session.git_repo and repo_lower in session.git_repo.lower()]

        if excluded_uuid is not None:
            sessions = [session for session in sessions if session.id != excluded_uuid]

        session_ids = [session.id for session in sessions]
        presence_map = load_presence_map(db, session_ids)
        now = datetime.now(timezone.utc)

        peers: list[dict[str, Any]] = []
        for session in sessions:
            presence = presence_map.get(session.id)
            has_live_presence = False
            presence_state: str | None = None
            if presence is not None:
                presence_updated = getattr(presence, "updated_at", None)
                if presence_updated is not None:
                    if presence_updated.tzinfo is None:
                        presence_updated = presence_updated.replace(tzinfo=timezone.utc)
                    has_live_presence = (now - presence_updated) < _PRESENCE_TTL
                if has_live_presence:
                    presence_state = str(getattr(presence, "state", "") or "").strip() or None

            if active_only and not has_live_presence:
                continue

            peers.append(
                {
                    "session_id": str(session.id),
                    "device_name": getattr(session, "device_name", None)
                    or (session.device_id.replace("shipper-", "") if session.device_id else None),
                    "device_id": session.device_id,
                    "git_repo": session.git_repo,
                    "git_branch": session.git_branch,
                    "project": session.project,
                    "provider": session.provider,
                    "summary_title": getattr(session, "summary_title", None),
                    "started_at": session.started_at.isoformat() if session.started_at else None,
                    "has_live_presence": has_live_presence,
                    "presence_state": presence_state,
                    "user_messages": int(session.user_messages or 0),
                    "assistant_messages": int(session.assistant_messages or 0),
                    "tool_calls": int(session.tool_calls or 0),
                }
            )
            if len(peers) >= limit:
                break

    return tool_success(
        {
            "repo": repo,
            "project": project,
            "active_only": active_only,
            "peers": peers,
            "total": len(peers),
        }
    )


def get_session_events(
    session_id: str,
    roles: list[str] | None = None,
    tool_name: str | None = None,
    query: str | None = None,
    context_mode: str = "forensic",
    branch_mode: str = "head",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch filtered events for a session."""
    session_uuid = _parse_uuid(session_id, field_name="session_id")
    if session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")
    if context_mode not in {"forensic", "active_context"}:
        return tool_error(ErrorType.VALIDATION_ERROR, "context_mode must be forensic or active_context")
    if branch_mode not in {"head", "all"}:
        return tool_error(ErrorType.VALIDATION_ERROR, "branch_mode must be head or all")

    with db_session() as db:
        store = AgentsStore(db)
        session = store.get_session(session_uuid)
        if session is None:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

        events = store.get_session_events(
            session_uuid,
            roles=roles,
            tool_name=tool_name,
            query=query,
            context_mode=context_mode,
            branch_mode=branch_mode,
            limit=limit,
            offset=offset,
        )
        total = store.count_session_events(
            session_uuid,
            roles=roles,
            tool_name=tool_name,
            query=query,
            context_mode=context_mode,
            branch_mode=branch_mode,
        )

    return tool_success(
        {
            "session_id": session_id,
            "events": [
                {
                    "id": event.id,
                    "role": event.role,
                    "content_text": event.content_text,
                    "tool_name": event.tool_name,
                    "tool_input_json": event.tool_input_json,
                    "tool_output_text": event.tool_output_text,
                    "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                }
                for event in events
            ],
            "total": total,
            "returned": len(events),
            "offset": offset,
            "context_mode": context_mode,
            "branch_mode": branch_mode,
        }
    )


def get_session_tail(
    session_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Return the recent tail of a session in chronological order."""
    session_uuid = _parse_uuid(session_id, field_name="session_id")
    if session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")

    with db_session() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
        if session is None:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_uuid)
            .filter(AgentEvent.role.in_(["user", "assistant", "tool"]))
            .filter(AgentEvent.content_text.isnot(None))
            .order_by(AgentEvent.timestamp.desc(), AgentEvent.id.desc())
            .limit(limit)
            .all()
        )
        events.reverse()

    return tool_success(
        {
            "session_id": session_id,
            "events": [_session_tail_event_payload(event) for event in events],
            "total": len(events),
        }
    )


async def message_session_async(
    from_session_id: str,
    to_session_id: str,
    text: str,
    source_event_id: int | None = None,
) -> dict[str, Any]:
    """Create a durable directed message between two sessions."""
    from_session_uuid = _parse_uuid(from_session_id, field_name="from_session_id")
    if from_session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "from_session_id must be a valid UUID")

    to_session_uuid = _parse_uuid(to_session_id, field_name="to_session_id")
    if to_session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "to_session_id must be a valid UUID")

    normalized_text = str(text or "").strip()
    if not normalized_text:
        return tool_error(ErrorType.VALIDATION_ERROR, "text cannot be empty")

    with db_session() as db:
        owner_id = resolve_session_message_owner_id(db, None)
        try:
            outcome = await create_session_message(
                db=db,
                owner_id=owner_id,
                from_session_id=from_session_uuid,
                to_session_id=to_session_uuid,
                text=normalized_text[:4000],
                source_event_id=source_event_id,
            )
        except ValueError as exc:
            detail = str(exc)
            error_type = ErrorType.NOT_FOUND if detail.endswith("not found") else ErrorType.VALIDATION_ERROR
            return tool_error(error_type, detail)

    return tool_success(_session_message_payload(outcome.message))


def message_session(
    from_session_id: str,
    to_session_id: str,
    text: str,
    source_event_id: int | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper for message_session_async."""
    return asyncio.run(
        message_session_async(
            from_session_id=from_session_id,
            to_session_id=to_session_id,
            text=text,
            source_event_id=source_event_id,
        )
    )


def check_session_messages(
    session_id: str,
    direction: str = "inbound",
    unacknowledged_only: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Inspect durable queued or delivered session messages."""
    session_uuid = _parse_uuid(session_id, field_name="session_id")
    if session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")
    if direction not in {"inbound", "outbound", "all"}:
        return tool_error(ErrorType.VALIDATION_ERROR, "direction must be inbound, outbound, or all")

    with db_session() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
        if session is None:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

        query = db.query(SessionMessage)
        if direction == "inbound":
            query = query.filter(SessionMessage.to_session_id == session_uuid)
        elif direction == "outbound":
            query = query.filter(SessionMessage.from_session_id == session_uuid)
        else:
            query = query.filter((SessionMessage.to_session_id == session_uuid) | (SessionMessage.from_session_id == session_uuid))
        if unacknowledged_only:
            query = query.filter(SessionMessage.acknowledged_at.is_(None))

        messages = query.order_by(SessionMessage.created_at.desc(), SessionMessage.id.desc()).limit(limit).all()

    return tool_success(
        {
            "session_id": session_id,
            "direction": direction,
            "unacknowledged_only": unacknowledged_only,
            "messages": [_session_message_payload(message) for message in messages],
            "total": len(messages),
        }
    )


def acknowledge_session_message(
    message_id: int,
    session_id: str,
) -> dict[str, Any]:
    """Acknowledge a delivered message on behalf of the target session."""
    session_uuid = _parse_uuid(session_id, field_name="session_id")
    if session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")

    with db_session() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
        if session is None:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

        message = db.query(SessionMessage).filter(SessionMessage.id == message_id).first()
        if message is None:
            return tool_error(ErrorType.NOT_FOUND, f"Message {message_id} not found")
        if session_uuid != message.to_session_id:
            return tool_error(ErrorType.INVALID_STATE, "Only the target session can acknowledge this message")
        if message.delivery_status in {MESSAGE_STATUS_QUEUED, MESSAGE_STATUS_DELIVERING}:
            return tool_error(ErrorType.INVALID_STATE, "Message has not been delivered to the target session yet")
        if message.delivery_status == MESSAGE_STATUS_FAILED:
            return tool_error(ErrorType.INVALID_STATE, "Failed messages cannot be acknowledged")

        if message.acknowledged_at is None:
            message.acknowledged_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(message)

    return tool_success(_session_message_payload(message))


TOOLS = [
    StructuredTool.from_function(
        func=list_session_peers,
        name="list_session_peers",
        description="List recent peer sessions around a repo or project, with live-presence metadata.",
        args_schema=SessionPeersInput,
    ),
    StructuredTool.from_function(
        func=get_session_events,
        name="get_session_events",
        description="Fetch filtered events for a session with role/tool/query/context filters.",
        args_schema=SessionEventsInput,
    ),
    StructuredTool.from_function(
        func=get_session_tail,
        name="get_session_tail",
        description="Read the recent tail of a session in chronological order.",
        args_schema=SessionTailInput,
    ),
    StructuredTool.from_function(
        func=message_session,
        coroutine=message_session_async,
        name="message_session",
        description="Send a durable directed message from one session to another.",
        args_schema=MessageSessionInput,
    ),
    StructuredTool.from_function(
        func=check_session_messages,
        name="check_session_messages",
        description="Inspect inbound/outbound durable session messages for a given session.",
        args_schema=CheckSessionMessagesInput,
    ),
    StructuredTool.from_function(
        func=acknowledge_session_message,
        name="acknowledge_session_message",
        description="Acknowledge that a target session has handled a delivered message.",
        args_schema=AcknowledgeSessionMessageInput,
    ),
]
