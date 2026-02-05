"""Session discovery tools for Oikos.

Provide FTS-backed search, regex grep, and session detail retrieval.
"""

from __future__ import annotations

import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from zerg.database import db_session
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success
from zerg.types.tools import Tool as StructuredTool

# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------


class SearchSessionsInput(BaseModel):
    """Input schema for search_sessions."""

    query: str = Field(description="Search query across session events")
    limit: int = Field(default=10, ge=1, le=50, description="Max sessions to return")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    days_back: int = Field(default=90, ge=1, le=365, description="Days to look back")
    project: str | None = Field(default=None, description="Filter by project name")
    provider: str | None = Field(default=None, description="Filter by provider")
    include_test: bool = Field(default=False, description="Include test/e2e sessions")


class FilterSessionsInput(BaseModel):
    """Input schema for filter_sessions."""

    project: str | None = Field(default=None, description="Filter by project name")
    provider: str | None = Field(default=None, description="Filter by provider")
    days_back: int = Field(default=90, ge=1, le=365, description="Days to look back")
    include_test: bool = Field(default=False, description="Include test/e2e sessions")
    limit: int = Field(default=20, ge=1, le=100, description="Max sessions to return")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


class GrepSessionsInput(BaseModel):
    """Input schema for grep_sessions."""

    pattern: str = Field(description="Regex pattern to search for")
    limit: int = Field(default=20, ge=1, le=100, description="Max matches to return")
    days_back: int = Field(default=90, ge=1, le=365, description="Days to look back")
    project: str | None = Field(default=None, description="Filter by project name")
    provider: str | None = Field(default=None, description="Filter by provider")
    include_tool_output: bool = Field(default=True, description="Search tool output text")
    case_sensitive: bool = Field(default=False, description="Use case-sensitive regex")


class SessionDetailInput(BaseModel):
    """Input schema for get_session_detail."""

    session_id: str = Field(description="Session UUID")
    roles: list[str] | None = Field(default=None, description="Optional role filter")
    limit: int = Field(default=200, ge=1, le=2000, description="Max events to return")
    offset: int = Field(default=0, ge=0, description="Offset for pagination")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_summary(session: AgentSession, match: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "id": str(session.id),
        "provider": session.provider,
        "project": session.project,
        "device_id": session.device_id,
        "cwd": session.cwd,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "user_messages": session.user_messages or 0,
        "assistant_messages": session.assistant_messages or 0,
        "tool_calls": session.tool_calls or 0,
    }
    if match:
        payload.update(
            {
                "match_event_id": match.get("event_id"),
                "match_snippet": match.get("snippet"),
                "match_role": match.get("role"),
            }
        )
    return payload


def _build_regex_snippet(text: str, match: re.Match[str], radius: int = 80) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def search_sessions(
    query: str,
    limit: int = 10,
    offset: int = 0,
    days_back: int = 90,
    project: str | None = None,
    provider: str | None = None,
    include_test: bool = False,
) -> dict[str, Any]:
    """Search sessions by keyword using FTS5 when available."""
    if not query or not query.strip():
        return tool_error(ErrorType.VALIDATION_ERROR, "query cannot be empty")

    since = datetime.now(timezone.utc) - timedelta(days=days_back)

    with db_session() as db:
        store = AgentsStore(db)
        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            include_test=include_test,
            since=since,
            query=query.strip(),
            limit=limit,
            offset=offset,
        )
        match_map = store.get_session_matches([s.id for s in sessions], query.strip())

        results = [_session_summary(session, match_map.get(session.id)) for session in sessions]

    return tool_success(
        {
            "query": query,
            "total": total,
            "sessions": results,
        }
    )


def filter_sessions(
    project: str | None = None,
    provider: str | None = None,
    days_back: int = 90,
    include_test: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Filter sessions by metadata (project/provider/date)."""
    since = datetime.now(timezone.utc) - timedelta(days=days_back)

    with db_session() as db:
        store = AgentsStore(db)
        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            include_test=include_test,
            since=since,
            limit=limit,
            offset=offset,
        )

        results = [_session_summary(session) for session in sessions]

    return tool_success(
        {
            "total": total,
            "sessions": results,
        }
    )


def grep_sessions(
    pattern: str,
    limit: int = 20,
    days_back: int = 90,
    project: str | None = None,
    provider: str | None = None,
    include_tool_output: bool = True,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Regex search across session events (content + tool output)."""
    if not pattern or not pattern.strip():
        return tool_error(ErrorType.VALIDATION_ERROR, "pattern cannot be empty")

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags=flags)
    except re.error as exc:
        return tool_error(ErrorType.VALIDATION_ERROR, f"Invalid regex pattern: {exc}")

    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    max_scan = min(max(limit * 50, 500), 5000)

    matches: list[dict[str, Any]] = []

    with db_session() as db:
        stmt = (
            select(
                AgentEvent,
                AgentSession.project,
                AgentSession.provider,
                AgentSession.started_at,
            )
            .join(AgentSession, AgentSession.id == AgentEvent.session_id)
            .where(AgentEvent.timestamp >= since)
            .order_by(AgentEvent.timestamp.desc())
        )

        if project:
            stmt = stmt.where(AgentSession.project.ilike(f"%{project}%"))
        if provider:
            stmt = stmt.where(AgentSession.provider == provider)

        stmt = stmt.limit(max_scan)
        rows = db.execute(stmt).fetchall()

        for event, session_project, session_provider, started_at in rows:
            if event.content_text:
                match = regex.search(event.content_text)
                if match:
                    matches.append(
                        {
                            "session_id": str(event.session_id),
                            "event_id": event.id,
                            "role": event.role,
                            "field": "content_text",
                            "snippet": _build_regex_snippet(event.content_text, match),
                            "project": session_project,
                            "provider": session_provider,
                            "started_at": started_at.isoformat() if started_at else None,
                        }
                    )
            if include_tool_output and event.tool_output_text:
                match = regex.search(event.tool_output_text)
                if match:
                    matches.append(
                        {
                            "session_id": str(event.session_id),
                            "event_id": event.id,
                            "role": event.role,
                            "field": "tool_output_text",
                            "snippet": _build_regex_snippet(event.tool_output_text, match),
                            "project": session_project,
                            "provider": session_provider,
                            "started_at": started_at.isoformat() if started_at else None,
                        }
                    )

            if len(matches) >= limit:
                break

    return tool_success(
        {
            "pattern": pattern,
            "matches": matches[:limit],
            "scanned_events": len(rows),
        }
    )


def get_session_detail(
    session_id: str,
    roles: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Fetch a session and its events."""
    try:
        session_uuid = UUID(session_id)
    except ValueError:
        return tool_error(ErrorType.VALIDATION_ERROR, "session_id must be a valid UUID")

    with db_session() as db:
        store = AgentsStore(db)
        session = store.get_session(session_uuid)
        if not session:
            return tool_error(ErrorType.NOT_FOUND, f"Session not found: {session_id}")

        events = store.get_session_events(
            session_uuid,
            roles=roles,
            limit=limit,
            offset=offset,
        )

        event_payloads = [
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
        ]

    return tool_success(
        {
            "session": _session_summary(session),
            "events": event_payloads,
            "total_events": len(event_payloads),
        }
    )


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOLS = [
    StructuredTool.from_function(
        func=search_sessions,
        name="search_sessions",
        description="Search sessions by keyword across event content. Returns matching sessions with snippets.",
        args_schema=SearchSessionsInput,
    ),
    StructuredTool.from_function(
        func=grep_sessions,
        name="grep_sessions",
        description="Regex search across session events (content + tool output). Returns matched snippets.",
        args_schema=GrepSessionsInput,
    ),
    StructuredTool.from_function(
        func=filter_sessions,
        name="filter_sessions",
        description="Filter sessions by project/provider/date without keyword search.",
        args_schema=FilterSessionsInput,
    ),
    StructuredTool.from_function(
        func=get_session_detail,
        name="get_session_detail",
        description="Fetch a session and its events by session_id.",
        args_schema=SessionDetailInput,
    ),
]
