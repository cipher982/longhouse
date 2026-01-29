"""Agents API for session ingest and query.

Provides endpoints for:
- POST /api/agents/ingest - Ingest sessions and events from AI coding tools
- GET /api/agents/sessions - List sessions with filters
- GET /api/agents/sessions/{id} - Get session details
- GET /api/agents/sessions/{id}/events - Get session events
- GET /api/agents/sessions/{id}/export - Export session as JSONL for --resume
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    """Response for a single session."""

    id: str = Field(..., description="Session UUID")
    provider: str = Field(..., description="AI provider")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device ID")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    user_messages: int = Field(..., description="User message count")
    assistant_messages: int = Field(..., description="Assistant message count")
    tool_calls: int = Field(..., description="Tool call count")


class SessionsListResponse(BaseModel):
    """Response for session list."""

    sessions: List[SessionResponse]
    total: int


class EventResponse(BaseModel):
    """Response for a single event."""

    id: int = Field(..., description="Event ID")
    role: str = Field(..., description="Message role")
    content_text: Optional[str] = Field(None, description="Message content")
    tool_name: Optional[str] = Field(None, description="Tool name")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool input")
    tool_output_text: Optional[str] = Field(None, description="Tool output")
    timestamp: datetime = Field(..., description="Event timestamp")


class EventsListResponse(BaseModel):
    """Response for events list."""

    events: List[EventResponse]
    total: int


class IngestResponse(BaseModel):
    """Response for ingest endpoint."""

    session_id: str
    events_inserted: int
    events_skipped: int
    session_created: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(
    data: SessionIngest,
    db: Session = Depends(get_db),
) -> IngestResponse:
    """Ingest a session with events.

    Creates or updates a session and inserts events, handling deduplication
    automatically via event hashing.

    This endpoint is called by the shipper to sync local session files
    (e.g., ~/.claude/projects/...) to Zerg.
    """
    try:
        store = AgentsStore(db)
        result = store.ingest_session(data)

        return IngestResponse(
            session_id=str(result.session_id),
            events_inserted=result.events_inserted,
            events_skipped=result.events_skipped,
            session_created=result.session_created,
        )

    except Exception as e:
        logger.exception("Failed to ingest session")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to ingest session: {e}",
        )


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
) -> SessionsListResponse:
    """List sessions with optional filters.

    Returns sessions sorted by start time (most recent first).
    """
    try:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            device_id=device_id,
            since=since,
            query=query,
            limit=limit,
            offset=offset,
        )

        return SessionsListResponse(
            sessions=[
                SessionResponse(
                    id=str(s.id),
                    provider=s.provider,
                    project=s.project,
                    device_id=s.device_id,
                    cwd=s.cwd,
                    git_repo=s.git_repo,
                    git_branch=s.git_branch,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    user_messages=s.user_messages or 0,
                    assistant_messages=s.assistant_messages or 0,
                    tool_calls=s.tool_calls or 0,
                )
                for s in sessions
            ],
            total=total,
        )

    except Exception as e:
        logger.exception("Failed to list sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {e}",
        )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    db: Session = Depends(get_db),
) -> SessionResponse:
    """Get a single session by ID."""
    store = AgentsStore(db)
    session = store.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    return SessionResponse(
        id=str(session.id),
        provider=session.provider,
        project=session.project,
        device_id=session.device_id,
        cwd=session.cwd,
        git_repo=session.git_repo,
        git_branch=session.git_branch,
        started_at=session.started_at,
        ended_at=session.ended_at,
        user_messages=session.user_messages or 0,
        assistant_messages=session.assistant_messages or 0,
        tool_calls=session.tool_calls or 0,
    )


@router.get("/sessions/{session_id}/events", response_model=EventsListResponse)
async def get_session_events(
    session_id: UUID,
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
) -> EventsListResponse:
    """Get events for a session."""
    store = AgentsStore(db)

    # Check session exists
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    # Parse roles filter
    role_list = [r.strip() for r in roles.split(",")] if roles else None

    events = store.get_session_events(
        session_id,
        roles=role_list,
        limit=limit,
        offset=offset,
    )

    # Get total count (approximate)
    total = session.user_messages + session.assistant_messages

    return EventsListResponse(
        events=[
            EventResponse(
                id=e.id,
                role=e.role,
                content_text=e.content_text,
                tool_name=e.tool_name,
                tool_input_json=e.tool_input_json,
                tool_output_text=e.tool_output_text,
                timestamp=e.timestamp,
            )
            for e in events
        ],
        total=total,
    )


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Export session as JSONL for Claude Code --resume.

    Returns the session as a JSONL file with headers containing
    session metadata for the session continuity service.
    """
    store = AgentsStore(db)
    result = store.export_session_jsonl(session_id)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    jsonl_bytes, session = result

    headers = {
        "Content-Disposition": f"attachment; filename={session_id}.jsonl",
        "X-Session-CWD": session.cwd or "",
        "X-Provider-Session-ID": str(session.id),
        "X-Session-Provider": session.provider,
        "X-Session-Project": session.project or "",
    }

    return Response(
        content=jsonl_bytes,
        media_type="application/x-ndjson",
        headers=headers,
    )
