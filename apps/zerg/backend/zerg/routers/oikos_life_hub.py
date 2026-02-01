"""Session integration for Oikos.

Provides endpoints to list and preview past AI sessions.
Used by the Session Picker modal to enable resuming past sessions.

Queries the sessions and events tables which are populated by the
shipper service which syncs session data from local AI coding tools.
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.routers.agents import require_single_tenant
from zerg.routers.oikos_auth import get_current_oikos_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/life-hub", tags=["life-hub"])


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    """Summary of an AI session."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name (e.g., 'zerg', 'life-hub')")
    provider: str = Field(..., description="AI provider (claude, codex, gemini)")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    duration_minutes: Optional[int] = Field(None, description="Duration in minutes")
    turn_count: int = Field(..., description="Number of turns (messages)")
    last_user_message: Optional[str] = Field(None, description="Truncated last user message")
    last_ai_message: Optional[str] = Field(None, description="Truncated last AI message")


class SessionsListResponse(BaseModel):
    """Response for session list endpoint."""

    sessions: List[SessionSummary] = Field(..., description="List of sessions")
    total: int = Field(..., description="Total number of matching sessions")


class SessionMessage(BaseModel):
    """Single message in session preview."""

    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")


class SessionPreview(BaseModel):
    """Preview of a session's recent messages."""

    id: str = Field(..., description="Session UUID")
    messages: List[SessionMessage] = Field(..., description="Recent messages")
    total_messages: int = Field(..., description="Total message count")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
    query: Optional[str] = Query(None, description="Search query for content"),
    project: Optional[str] = Query(None, description="Filter by project name"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    limit: int = Query(20, ge=1, le=100, description="Max results to return"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
    _single: None = Depends(require_single_tenant),
) -> SessionsListResponse:
    """List past AI sessions.

    Returns session summaries for the session picker modal.
    Sessions are filtered by the authenticated user's context and sorted
    by most recent first.
    """
    try:
        # Check if sessions table exists
        table_check = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"))
        if not table_check.fetchone():
            # Return empty results if sessions table doesn't exist yet
            logger.warning("sessions table not found - returning empty session list")
            return SessionsListResponse(sessions=[], total=0)

        # Build the SQL query
        since_date = datetime.now(timezone.utc) - timedelta(days=days_back)

        # Base query with optional filters
        where_clauses = ["s.started_at >= :since_date"]
        params = {"since_date": since_date.isoformat(), "limit": limit}

        if project:
            where_clauses.append("s.project LIKE :project")
            params["project"] = f"%{project}%"

        if provider:
            where_clauses.append("s.provider = :provider")
            params["provider"] = provider

        if device_id:
            where_clauses.append("s.device_id = :device_id")
            params["device_id"] = device_id

        # Query-based content search (searches in events)
        if query:
            params["query_pattern"] = f"%{query}%"
            where_clauses.append("EXISTS (SELECT 1 FROM events eq WHERE eq.session_id = s.id AND eq.content_text LIKE :query_pattern)")

        where_sql = " AND ".join(where_clauses)

        # Main query - SQLite-compatible
        sql = text(f"""
            WITH last_user AS (
                SELECT
                    session_id,
                    SUBSTR(content_text, 1, 200) as content_preview
                FROM events
                WHERE role = 'user' AND content_text IS NOT NULL
                GROUP BY session_id
                HAVING timestamp = MAX(timestamp)
            ),
            last_ai AS (
                SELECT
                    session_id,
                    SUBSTR(content_text, 1, 200) as content_preview
                FROM events
                WHERE role = 'assistant' AND content_text IS NOT NULL
                GROUP BY session_id
                HAVING timestamp = MAX(timestamp)
            )
            SELECT
                s.id,
                s.project,
                s.provider,
                s.cwd,
                s.git_branch,
                s.started_at,
                s.ended_at,
                CAST((julianday(COALESCE(s.ended_at, datetime('now'))) - julianday(s.started_at)) * 24 * 60 AS INTEGER) as duration_minutes,
                COALESCE(s.user_messages, 0) + COALESCE(s.assistant_messages, 0) as turn_count,
                last_user.content_preview as last_user_message,
                last_ai.content_preview as last_ai_message
            FROM sessions s
            LEFT JOIN last_user ON last_user.session_id = s.id
            LEFT JOIN last_ai ON last_ai.session_id = s.id
            WHERE {where_sql}
            ORDER BY s.started_at DESC
            LIMIT :limit
        """)

        # Execute query
        result = db.execute(sql, params)
        rows = result.fetchall()

        # Convert to response models
        sessions = []
        for row in rows:
            sessions.append(
                SessionSummary(
                    id=str(row[0]),
                    project=row[1],
                    provider=row[2],
                    cwd=row[3],
                    git_branch=row[4],
                    started_at=row[5] if isinstance(row[5], datetime) else datetime.fromisoformat(row[5]),
                    ended_at=row[6] if row[6] is None or isinstance(row[6], datetime) else datetime.fromisoformat(row[6]),
                    duration_minutes=int(row[7]) if row[7] else None,
                    turn_count=row[8] or 0,
                    last_user_message=row[9],
                    last_ai_message=row[10],
                )
            )

        # Get total count (for pagination info)
        count_sql = text(f"""
            SELECT COUNT(DISTINCT s.id)
            FROM sessions s
            WHERE {where_sql}
        """)
        count_result = db.execute(count_sql, params)
        total = count_result.scalar() or 0

        logger.debug(f"Listed {len(sessions)} sessions for user {current_user.id}")

        return SessionsListResponse(sessions=sessions, total=total)

    except Exception as e:
        logger.exception("Failed to list sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {e}",
        )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreview)
async def preview_session(
    session_id: str,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
    _single: None = Depends(require_single_tenant),
) -> SessionPreview:
    """Get a preview of a session's recent messages.

    Returns the last N messages from the session for preview in the picker.
    Default is 6 messages (3 exchanges).
    """
    try:
        # Check if sessions table exists
        table_check = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"))
        if not table_check.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found (sessions table not initialized)",
            )

        # Validate session exists
        session_sql = text("""
            SELECT id, COALESCE(user_messages, 0) + COALESCE(assistant_messages, 0) as total_messages
            FROM sessions
            WHERE id = :session_id
        """)
        session_result = db.execute(session_sql, {"session_id": session_id})
        session_row = session_result.fetchone()

        if not session_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session {session_id} not found",
            )

        total_messages = session_row[1] or 0

        # Get recent messages
        messages_sql = text("""
            SELECT
                role,
                SUBSTR(content_text, 1, 500) as content,
                timestamp
            FROM events
            WHERE session_id = :session_id
                AND role IN ('user', 'assistant')
                AND content_text IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT :limit
        """)
        result = db.execute(messages_sql, {"session_id": session_id, "limit": last_n})
        rows = result.fetchall()

        # Convert to response (reverse to get chronological order)
        messages = []
        for row in reversed(rows):
            ts = row[2]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            messages.append(
                SessionMessage(
                    role=row[0],
                    content=row[1] or "",
                    timestamp=ts,
                )
            )

        logger.debug(f"Previewed session {session_id} ({len(messages)} messages)")

        return SessionPreview(
            id=session_id,
            messages=messages,
            total_messages=total_messages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to preview session {session_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to preview session: {e}",
        )
