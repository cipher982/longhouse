"""Agents API for session ingest and query.

Provides endpoints for:
- POST /api/agents/ingest - Ingest sessions and events from AI coding tools
- GET /api/agents/sessions - List sessions with filters
- GET /api/agents/sessions/{id} - Get session details
- GET /api/agents/sessions/{id}/events - Get session events
- GET /api/agents/sessions/{id}/export - Export session as JSONL for --resume

Authentication:
- When AUTH_DISABLED=1 (dev mode), endpoints are open
- Otherwise, requires X-Agents-Token header with:
  1. Per-device token (zdt_...) created via /api/devices/tokens
  2. Legacy AGENTS_API_TOKEN env var (for backwards compatibility)

Rate Limiting:
- Ingest endpoint enforces 1000 events/min per device (token-derived when available)
- Returns HTTP 429 with Retry-After header when exceeded
"""

import gzip
import hashlib
import hmac
import logging
from collections import defaultdict
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
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.crud import count_users
from zerg.database import get_db
from zerg.database import is_postgres
from zerg.models.device_token import DeviceToken
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

_settings = get_settings()


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

# In-memory rate limit tracking: device_id -> list of (timestamp, event_count)
# Keyed by device token ID (preferred) or device_id (fallback)
_rate_limits: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
RATE_LIMIT_EVENTS_PER_MIN = 1000  # Soft cap per device


def check_rate_limit(rate_key: str, event_count: int) -> tuple[bool, int]:
    """Check if request would exceed rate limit.

    Args:
        rate_key: Stable key for the caller (device token or device_id)
        event_count: Number of events in this request

    Returns:
        Tuple of (exceeded: bool, retry_after_seconds: int)
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=1)

    # Clean old entries
    _rate_limits[rate_key] = [(ts, count) for ts, count in _rate_limits[rate_key] if ts > cutoff]

    # Sum events in the last minute
    current_events = sum(count for _, count in _rate_limits[rate_key])

    # Check if this request would exceed the limit
    if current_events + event_count > RATE_LIMIT_EVENTS_PER_MIN:
        # Calculate retry-after based on oldest entry expiration
        if _rate_limits[rate_key]:
            oldest_ts = min(ts for ts, _ in _rate_limits[rate_key])
            retry_after = int((oldest_ts + timedelta(minutes=1) - now).total_seconds())
            retry_after = max(1, retry_after)  # At least 1 second
        else:
            retry_after = 60
        return True, retry_after

    # Record this request
    _rate_limits[rate_key].append((now, event_count))
    return False, 0


def reset_rate_limits() -> None:
    """Reset all rate limits. Used for testing."""
    global _rate_limits
    _rate_limits = defaultdict(list)


# ---------------------------------------------------------------------------
# Auth Dependency
# ---------------------------------------------------------------------------


def verify_agents_token(request: Request, db: Session = Depends(get_db)) -> DeviceToken | None:
    """Verify the agents API token.

    Accepts two types of tokens:
    1. Per-device tokens (zdt_...) created via /api/devices/tokens
    2. Legacy AGENTS_API_TOKEN env var (for backwards compatibility)

    In dev mode (AUTH_DISABLED=1), allows all requests.

    Raises:
        HTTPException(401): If token is missing, invalid, or revoked
        HTTPException(403): If no auth method is configured in production
    """
    # Dev mode - allow all
    if _settings.auth_disabled:
        return

    # Try X-Agents-Token header first, then Authorization: Bearer
    provided_token = request.headers.get("X-Agents-Token")
    if not provided_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided_token = auth_header[7:]

    if not provided_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication - provide X-Agents-Token header",
        )

    # Check if this is a per-device token (starts with zdt_)
    if provided_token.startswith("zdt_"):
        from zerg.routers.device_tokens import validate_device_token

        device_token = validate_device_token(provided_token, db)
        if device_token:
            # Valid device token - auth successful
            logger.debug(f"Device token validated for device {device_token.device_id}")
            request.state.agents_rate_key = f"device:{device_token.id}"
            return device_token

        # Device token exists but is invalid/revoked
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked device token",
        )

    # Fall back to legacy AGENTS_API_TOKEN comparison
    expected_token = _settings.agents_api_token
    if not expected_token:
        # In production without any auth method configured, deny access
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agents API not configured - create a device token or set AGENTS_API_TOKEN env var",
        )

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agents API token",
        )

    token_hash = hashlib.sha256(provided_token.encode()).hexdigest()
    request.state.agents_rate_key = f"token:{token_hash}"


def require_postgres() -> None:
    """Ensure agents endpoints only work with PostgreSQL.

    The agents schema uses PostgreSQL-specific features (UUID, JSONB, partial indexes).
    This check provides a clear error message for OSS users running with SQLite.

    Raises:
        HTTPException(501): If the database is not PostgreSQL
    """
    if not is_postgres():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Agents API requires PostgreSQL. SQLite is not supported for this feature.",
        )


def require_single_tenant(db: Session = Depends(get_db)) -> None:
    """Enforce single-tenant mode for agents endpoints.

    Blocks access if more than one user exists in the instance. This prevents
    data leakage because the agents schema is not owner-scoped.
    """
    settings = get_settings()
    if not settings.single_tenant or settings.testing:
        return

    try:
        total_users = count_users(db)
    except Exception:
        total_users = 0

    if total_users <= 1:
        return

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Single-tenant mode: multiple users detected. This instance supports a single user only.",
    )


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


async def decompress_if_gzipped(request: Request) -> bytes:
    """Decompress request body if gzip-encoded.

    Checks Content-Encoding header and decompresses if needed.

    Returns:
        Decompressed request body as bytes
    """
    body = await request.body()
    content_encoding = request.headers.get("Content-Encoding", "").lower()

    if content_encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except gzip.BadGzipFile as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid gzip content: {e}",
            )

    return body


@router.post("/ingest", response_model=IngestResponse)
async def ingest_session(
    request: Request,
    db: Session = Depends(get_db),
    device_token: DeviceToken | None = Depends(verify_agents_token),
    _pg: None = Depends(require_postgres),
    _single: None = Depends(require_single_tenant),
) -> IngestResponse:
    """Ingest a session with events.

    Creates or updates a session and inserts events, handling deduplication
    automatically via event hashing.

    This endpoint is called by the shipper to sync local session files
    (e.g., ~/.claude/projects/...) to Zerg.

    Features:
    - Accepts gzip-compressed payloads (Content-Encoding: gzip)
    - Rate limiting: 1000 events/min per device_id (returns 429 if exceeded)
    """
    try:
        # Decompress if gzip-encoded
        body = await decompress_if_gzipped(request)

        # Parse JSON
        import json

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON: {e}",
            )

        # Validate payload with Pydantic
        try:
            data = SessionIngest(**payload)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid payload: {e}",
            )

        # Normalize device_id from token when available (prevents spoofing)
        if device_token:
            if data.device_id and data.device_id != device_token.device_id:
                logger.debug(
                    "Device ID mismatch: payload %s != token %s, using token device_id",
                    data.device_id,
                    device_token.device_id,
                )
            data.device_id = device_token.device_id

        # Check rate limit (prefer token-derived key)
        rate_key = getattr(request.state, "agents_rate_key", None)
        device_id = data.device_id or "unknown"
        if not rate_key:
            rate_key = f"device:{device_id}"

        event_count = len(data.events) if data.events else 0
        exceeded, retry_after = check_rate_limit(rate_key, event_count)

        if exceeded:
            logger.warning(f"Rate limit exceeded for device {device_id}: {event_count} events")
            return Response(
                content=json.dumps(
                    {
                        "detail": f"Rate limit exceeded. Max {RATE_LIMIT_EVENTS_PER_MIN} events/min per device.",
                        "device_id": device_id,
                    }
                ),
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        store = AgentsStore(db)
        result = store.ingest_session(data)

        return IngestResponse(
            session_id=str(result.session_id),
            events_inserted=result.events_inserted,
            events_skipped=result.events_skipped,
            session_created=result.session_created,
        )

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
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
    _auth: DeviceToken | None = Depends(verify_agents_token),
    _pg: None = Depends(require_postgres),
    _single: None = Depends(require_single_tenant),
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
    _auth: DeviceToken | None = Depends(verify_agents_token),
    _pg: None = Depends(require_postgres),
    _single: None = Depends(require_single_tenant),
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
    _auth: DeviceToken | None = Depends(verify_agents_token),
    _pg: None = Depends(require_postgres),
    _single: None = Depends(require_single_tenant),
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
    _auth: DeviceToken | None = Depends(verify_agents_token),
    _pg: None = Depends(require_postgres),
    _single: None = Depends(require_single_tenant),
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

    # Use provider_session_id for resume fidelity, fall back to Zerg session ID
    provider_session_id = session.provider_session_id or str(session.id)

    headers = {
        "Content-Disposition": f"attachment; filename={session_id}.jsonl",
        "X-Session-CWD": session.cwd or "",
        "X-Provider-Session-ID": provider_session_id,
        "X-Session-Provider": session.provider,
        "X-Session-Project": session.project or "",
    }

    return Response(
        content=jsonl_bytes,
        media_type="application/x-ndjson",
        headers=headers,
    )
