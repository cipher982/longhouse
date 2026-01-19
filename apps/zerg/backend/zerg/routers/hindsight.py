"""API router for Hindsight session analysis.

Provides webhook endpoints for Life Hub to notify Zerg when sessions end,
triggering hindsight analysis that extracts insights and creates tasks.

Architecture:
- Life Hub ingests session events from Claude Code, Codex, Gemini, etc.
- When a session ends, Life Hub calls POST /api/hindsight/session-ended
- Zerg spawns an async hindsight worker to analyze the session
- Results are shipped back to Life Hub's work.* schema
"""

from __future__ import annotations

import logging
from typing import Any
from typing import Optional

from fastapi import APIRouter
from fastapi import Body
from fastapi import Header
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field

from zerg.config import get_settings
from zerg.events import EventType
from zerg.events import event_bus
from zerg.services.hindsight_service import schedule_hindsight_analysis

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/hindsight",
    tags=["hindsight"],
)


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class SessionEndedPayload(BaseModel):
    """Payload for session-ended webhook from Life Hub."""

    session_id: str = Field(..., description="UUID of the session in Life Hub")
    provider: str = Field(..., description="Agent provider: claude, codex, gemini, etc.")
    project: Optional[str] = Field(None, description="Project name if detected")

    # Summary statistics
    user_messages: int = Field(0, description="Count of user messages")
    assistant_messages: int = Field(0, description="Count of assistant messages")
    tool_calls: int = Field(0, description="Count of tool calls")
    duration_minutes: float = Field(0, description="Session duration in minutes")

    # Optional detailed data
    errors: list[str] = Field(default_factory=list, description="Error messages encountered")
    tools_used: list[str] = Field(default_factory=list, description="Tools used in session")
    content_summary: Optional[str] = Field(None, description="Brief summary of session content")

    # Metadata
    device_id: Optional[str] = Field(None, description="Device that ran the session")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git repository")
    git_branch: Optional[str] = Field(None, description="Git branch")


class SessionEndedResponse(BaseModel):
    """Response for session-ended webhook."""

    status: str
    session_id: str
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Webhook Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/session-ended",
    response_model=SessionEndedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def handle_session_ended(
    payload: SessionEndedPayload = Body(...),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
):
    """Webhook endpoint for Life Hub to notify when a session ends.

    This triggers asynchronous hindsight analysis that:
    1. Analyzes the session for patterns, failures, and improvements
    2. Creates insights in Life Hub's work.insights table
    3. Optionally creates tasks in work.tasks for actionable items

    Security: Requires API key authentication.

    Request format:
        POST /api/hindsight/session-ended
        X-API-Key: <api_key>
        Content-Type: application/json

        {
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "provider": "claude",
            "project": "zerg",
            "user_messages": 15,
            "assistant_messages": 14,
            "tool_calls": 42,
            "duration_minutes": 25.5,
            "errors": ["Failed to read file: /path/to/file"],
            "tools_used": ["Edit", "Read", "Bash", "Grep"],
            "content_summary": "Implemented new feature X"
        }

    Returns:
        202 Accepted: Analysis scheduled successfully
        401 Unauthorized: Missing or invalid API key
        500 Internal Server Error: Failed to schedule analysis
    """
    settings = get_settings()

    # Validate API key
    api_key = x_api_key or (authorization[7:] if authorization and authorization.startswith("Bearer ") else None)

    if not settings.testing:
        if not api_key or api_key != settings.lifehub_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )

    logger.info(
        "Session ended webhook received: session=%s provider=%s project=%s",
        payload.session_id,
        payload.provider,
        payload.project,
    )

    # Publish session-ended event to internal bus
    await event_bus.publish(
        EventType.SESSION_ENDED,
        {
            "session_id": payload.session_id,
            "provider": payload.provider,
            "project": payload.project,
            "user_messages": payload.user_messages,
            "assistant_messages": payload.assistant_messages,
            "tool_calls": payload.tool_calls,
        },
    )

    # Build events summary for analysis
    events_summary: dict[str, Any] = {
        "user_messages": payload.user_messages,
        "assistant_messages": payload.assistant_messages,
        "tool_calls": payload.tool_calls,
        "duration_minutes": payload.duration_minutes,
        "errors": payload.errors,
        "tools_used": payload.tools_used,
        "content_summary": payload.content_summary or "",
        "device_id": payload.device_id,
        "cwd": payload.cwd,
        "git_repo": payload.git_repo,
        "git_branch": payload.git_branch,
    }

    # Schedule async hindsight analysis (fire-and-forget)
    schedule_hindsight_analysis(
        session_id=payload.session_id,
        project=payload.project,
        provider=payload.provider,
        events_summary=events_summary,
    )

    return SessionEndedResponse(
        status="accepted",
        session_id=payload.session_id,
        message="Hindsight analysis scheduled",
    )


@router.get("/health")
async def hindsight_health():
    """Health check for hindsight service."""
    return {
        "status": "healthy",
        "service": "hindsight",
    }
