"""Session coordination tools for runtime and operator agents.

These tools expose the same durable session/message primitives that Longhouse
publishes over the machine-facing `/api/agents/*` surface, but as direct
in-process tools for internal agents.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

from zerg.database import db_session
from zerg.models.agents import AgentSession
from zerg.services.agents import AgentsStore
from zerg.services.session_coordination import acknowledge_session_message as acknowledge_session_message_for_session
from zerg.services.session_coordination import build_peer_payloads
from zerg.services.session_coordination import list_session_messages
from zerg.services.session_coordination import load_session_tail
from zerg.services.session_coordination import query_wall_sessions
from zerg.services.session_coordination import serialize_session_message
from zerg.services.session_messages import create_session_message
from zerg.services.session_messages import resolve_session_message_owner_id
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.types.tools import Tool as StructuredTool


class SessionPeersInput(BaseModel):
    """Input schema for peers."""

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
    """Input schema for session_tail."""

    session_id: str = Field(description="Session UUID.")
    limit: int = Field(default=30, ge=1, le=100, description="Max recent events to return.")


class MessageSessionInput(BaseModel):
    """Input schema for message_session."""

    from_session_id: str = Field(description="Sender session UUID.")
    to_session_id: str = Field(description="Target session UUID.")
    text: str = Field(description="Directed message body.")
    source_event_id: int | None = Field(default=None, description="Optional source event ID.")


class CheckSessionMessagesInput(BaseModel):
    """Input schema for check_messages."""

    session_id: str = Field(description="Session UUID to inspect inbox/outbox for.")
    direction: str = Field(default="inbound", description="Direction: inbound, outbound, or all.")
    unacknowledged_only: bool = Field(default=True, description="Only include messages without acknowledged_at.")
    limit: int = Field(default=50, ge=1, le=200, description="Max messages to return.")


class AcknowledgeSessionMessageInput(BaseModel):
    """Input schema for ack_message."""

    message_id: int = Field(description="Numeric message ID.")
    session_id: str = Field(description="Target session UUID acknowledging the message.")


def _parse_uuid(raw: str, *, field_name: str) -> UUID | None:
    try:
        return UUID(str(raw).strip())
    except ValueError:
        return None


def peers(
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
        sessions = query_wall_sessions(
            db,
            repo=repo,
            project=project,
            days=days,
            limit=limit * 4 if active_only else limit,
        )

    peer_items = build_peer_payloads(
        sessions,
        active_only=active_only,
        exclude_session_id=excluded_uuid,
        limit=limit,
    )

    return tool_success(
        {
            "repo": repo,
            "project": project,
            "active_only": active_only,
            "peers": peer_items,
            "total": len(peer_items),
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


def session_tail(
    session_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Return the recent tail of a session in chronological order."""
    session_uuid = _parse_uuid(session_id, field_name="session_id")
    if session_uuid is None:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")

    with db_session() as db:
        try:
            events = load_session_tail(db, session_id=session_uuid, limit=limit)
        except ValueError:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

    return tool_success(
        {
            "session_id": session_id,
            "events": events,
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

    return tool_success(serialize_session_message(outcome.message, delivery_status=outcome.delivery_status))


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


def check_messages(
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
        messages = list_session_messages(
            db,
            session_id=session_uuid,
            direction=direction,
            unacknowledged_only=unacknowledged_only,
            limit=limit,
        )

    return tool_success(
        {
            "session_id": session_id,
            "direction": direction,
            "unacknowledged_only": unacknowledged_only,
            "messages": [serialize_session_message(message) for message in messages],
            "total": len(messages),
        }
    )


def ack_message(
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
        try:
            message = acknowledge_session_message_for_session(
                db,
                message_id=message_id,
                target_session_id=session_uuid,
            )
        except ValueError as exc:
            return tool_error(ErrorType.NOT_FOUND, str(exc))
        except (PermissionError, RuntimeError) as exc:
            return tool_error(ErrorType.INVALID_STATE, str(exc))

    return tool_success(serialize_session_message(message))


TOOLS = [
    StructuredTool.from_function(
        func=peers,
        name="peers",
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
        func=session_tail,
        name="session_tail",
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
        func=check_messages,
        name="check_messages",
        description="Inspect inbound/outbound durable session messages for a given session.",
        args_schema=CheckSessionMessagesInput,
    ),
    StructuredTool.from_function(
        func=ack_message,
        name="ack_message",
        description="Acknowledge that a target session has handled a delivered message.",
        args_schema=AcknowledgeSessionMessageInput,
    ),
]
