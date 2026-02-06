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
from zerg.database import get_db
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.demo_sessions import build_demo_agent_sessions

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
    """Verify the agents API token for write operations (ingest).

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


def verify_agents_read_access(request: Request, db: Session = Depends(get_db)) -> None:
    """Verify read access for agents endpoints (sessions list, detail, events).

    Accepts:
    1. Browser cookie auth (longhouse_session) - for UI access
    2. Device tokens (zdt_...) - for programmatic access

    In dev mode (AUTH_DISABLED=1), allows all requests.
    """
    # Dev mode - allow all
    if _settings.auth_disabled:
        return

    # Check for browser cookie auth first
    if "longhouse_session" in request.cookies:
        from zerg.dependencies.auth import get_current_user

        try:
            # This will validate the cookie and return user or raise 401
            get_current_user(request, db)
            return  # Cookie auth successful
        except HTTPException:
            pass  # Fall through to token auth

    # Fall back to device token / API token auth
    verify_agents_token(request, db)


def require_single_tenant(db: Session = Depends(get_db)) -> None:
    """Enforce single-tenant mode for agents endpoints.

    In single-tenant mode (SINGLE_TENANT=1, the default), agents endpoints are
    accessible without owner scoping - the deployment is trusted to have one owner.

    In multi-tenant mode (SINGLE_TENANT=0), agents endpoints require owner scoping
    which isn't implemented yet, so we block access.
    """
    settings = get_settings()

    # Testing mode: always allow
    if settings.testing:
        return

    # Single-tenant mode (default): trust the deployment, allow access
    if settings.single_tenant:
        return

    # Multi-tenant mode: agents tables aren't owner-scoped yet, block access
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Multi-tenant agents API not implemented. Set SINGLE_TENANT=1 or contact support.",
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
    match_event_id: Optional[int] = Field(None, description="Matching event id for search queries")
    match_snippet: Optional[str] = Field(None, description="Snippet of matching content")
    match_role: Optional[str] = Field(None, description="Role for matching event")


class SessionSummaryResponse(BaseModel):
    """Response for session summaries (picker UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    duration_minutes: Optional[int] = Field(None, description="Duration in minutes")
    turn_count: int = Field(..., description="Total user + assistant messages")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_ai_message: Optional[str] = Field(None, description="Last assistant message (truncated)")


class SessionsSummaryResponse(BaseModel):
    """Response for session summary list."""

    sessions: List[SessionSummaryResponse]
    total: int


class SessionsListResponse(BaseModel):
    """Response for session list."""

    sessions: List[SessionResponse]
    total: int


class SessionPreviewMessage(BaseModel):
    """Preview message entry for session picker."""

    role: str = Field(..., description="Message role")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")


class SessionPreviewResponse(BaseModel):
    """Response for session preview endpoint."""

    id: str = Field(..., description="Session UUID")
    messages: List[SessionPreviewMessage] = Field(..., description="Recent messages")
    total_messages: int = Field(..., description="Total message count")


class ActiveSessionResponse(BaseModel):
    """Response for active session summary (Forum UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    last_activity_at: datetime = Field(..., description="Most recent event timestamp")
    status: str = Field(..., description="Session status (working, idle, completed)")
    attention: str = Field(..., description="Attention level (auto by default)")
    duration_minutes: int = Field(..., description="Duration in minutes")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_assistant_message: Optional[str] = Field(None, description="Last assistant message (truncated)")
    message_count: int = Field(..., description="Total user + assistant messages")
    tool_calls: int = Field(..., description="Tool call count")


class ActiveSessionsResponse(BaseModel):
    """Response for active session list."""

    sessions: List[ActiveSessionResponse]
    total: int
    last_refresh: datetime


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


class FiltersResponse(BaseModel):
    """Response for filters endpoint."""

    projects: List[str]
    providers: List[str]


class DemoSeedResponse(BaseModel):
    """Response for demo session seeding."""

    seeded: bool
    sessions_created: int


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
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> SessionsListResponse:
    """List sessions with optional filters.

    Returns sessions sorted by start time (most recent first).
    By default, test and e2e sessions are excluded.
    """
    try:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            query=query,
            limit=limit,
            offset=offset,
        )

        match_map = store.get_session_matches([s.id for s in sessions], query) if query else {}

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
                    match_event_id=(match_map.get(s.id) or {}).get("event_id"),
                    match_snippet=(match_map.get(s.id) or {}).get("snippet"),
                    match_role=(match_map.get(s.id) or {}).get("role"),
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


@router.get("/sessions/summary", response_model=SessionsSummaryResponse)
async def list_session_summaries(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> SessionsSummaryResponse:
    """List session summaries for picker UI."""
    try:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        sessions, total = store.list_sessions(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            query=query,
            limit=limit,
            offset=offset,
        )

        session_ids = [s.id for s in sessions]
        last_user = store.get_last_message_map(session_ids, role="user", max_len=200)
        last_ai = store.get_last_message_map(session_ids, role="assistant", max_len=200)

        summaries: List[SessionSummaryResponse] = []
        now = datetime.now(timezone.utc)
        for s in sessions:
            end_time = s.ended_at or now
            duration_minutes = int((end_time - s.started_at).total_seconds() / 60) if s.started_at else None
            turn_count = (s.user_messages or 0) + (s.assistant_messages or 0)

            summaries.append(
                SessionSummaryResponse(
                    id=str(s.id),
                    project=s.project,
                    provider=s.provider,
                    cwd=s.cwd,
                    git_branch=s.git_branch,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    duration_minutes=duration_minutes,
                    turn_count=turn_count,
                    last_user_message=last_user.get(s.id),
                    last_ai_message=last_ai.get(s.id),
                )
            )

        return SessionsSummaryResponse(sessions=summaries, total=total)

    except Exception as e:
        logger.exception("Failed to list session summaries")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list session summaries: {e}",
        )


@router.get("/sessions/active", response_model=ActiveSessionsResponse)
async def list_active_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    status: Optional[str] = Query(None, description="Filter by status (working, idle, completed)"),
    attention: Optional[str] = Query(None, description="Filter by attention (auto)"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> ActiveSessionsResponse:
    """Return session summaries for Forum live mode."""
    try:
        store = AgentsStore(db)
        since = datetime.now(timezone.utc) - timedelta(days=days_back)

        sessions, _total = store.list_sessions(
            project=project,
            provider=None,
            environment=None,
            include_test=False,
            device_id=None,
            since=since,
            query=None,
            limit=limit,
            offset=0,
        )

        session_ids = [s.id for s in sessions]
        last_activity = store.get_last_activity_map(session_ids)
        last_user = store.get_last_message_map(session_ids, role="user", max_len=300)
        last_ai = store.get_last_message_map(session_ids, role="assistant", max_len=300)

        now = datetime.now(timezone.utc)
        items: List[ActiveSessionResponse] = []
        for s in sessions:
            last_activity_at = last_activity.get(s.id) or s.ended_at or s.started_at
            if not last_activity_at:
                last_activity_at = now

            if s.ended_at:
                derived_status = "completed"
            else:
                idle_for = now - last_activity_at
                derived_status = "working" if idle_for <= timedelta(minutes=5) else "idle"

            attention_level = "auto"

            if status and derived_status != status:
                continue
            if attention and attention_level != attention:
                continue

            end_time = s.ended_at or now
            duration_minutes = int((end_time - s.started_at).total_seconds() / 60) if s.started_at else 0
            message_count = (s.user_messages or 0) + (s.assistant_messages or 0)

            items.append(
                ActiveSessionResponse(
                    id=str(s.id),
                    project=s.project,
                    provider=s.provider,
                    cwd=s.cwd,
                    git_branch=s.git_branch,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    last_activity_at=last_activity_at,
                    status=derived_status,
                    attention=attention_level,
                    duration_minutes=duration_minutes,
                    last_user_message=last_user.get(s.id),
                    last_assistant_message=last_ai.get(s.id),
                    message_count=message_count,
                    tool_calls=s.tool_calls or 0,
                )
            )

        return ActiveSessionsResponse(
            sessions=items,
            total=len(items),
            last_refresh=now,
        )

    except Exception as e:
        logger.exception("Failed to list active sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list active sessions: {e}",
        )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreviewResponse)
async def preview_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> SessionPreviewResponse:
    """Get a preview of a session's recent messages."""
    store = AgentsStore(db)
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    events = store.get_session_preview(session_id, last_n)
    messages = [
        SessionPreviewMessage(
            role=e.role,
            content=e.content_text or "",
            timestamp=e.timestamp,
        )
        for e in events
    ]
    total_messages = (session.user_messages or 0) + (session.assistant_messages or 0)

    return SessionPreviewResponse(
        id=str(session_id),
        messages=messages,
        total_messages=total_messages,
    )


@router.get("/filters", response_model=FiltersResponse)
async def get_filters(
    days_back: int = Query(90, ge=1, le=365, description="Days to look back for distinct values"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> FiltersResponse:
    """Get distinct filter values for UI dropdowns.

    Returns lists of distinct projects and providers found in sessions
    from the specified time range.
    """
    try:
        store = AgentsStore(db)
        filters = store.get_distinct_filters(days_back=days_back)
        return FiltersResponse(
            projects=filters["projects"],
            providers=filters["providers"],
        )
    except Exception as e:
        logger.exception("Failed to get filters")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get filters: {e}",
        )


@router.post("/demo", response_model=DemoSeedResponse)
async def seed_demo_sessions(
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> DemoSeedResponse:
    """Seed demo sessions for the timeline (idempotent)."""
    existing = db.query(AgentSession).filter(AgentSession.provider_session_id.like("demo-%")).first()
    if existing:
        return DemoSeedResponse(seeded=False, sessions_created=0)

    store = AgentsStore(db)
    sessions = build_demo_agent_sessions(datetime.now(timezone.utc))
    for session in sessions:
        store.ingest_session(session)

    # Rebuild FTS5 index so timeline search works on demo data
    store.rebuild_fts()
    db.commit()

    return DemoSeedResponse(seeded=True, sessions_created=len(sessions))


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
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
    _auth: None = Depends(verify_agents_read_access),
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
    _auth: None = Depends(verify_agents_read_access),
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


class CleanupRequest(BaseModel):
    """Request for test cleanup."""

    project_patterns: List[str] = Field(
        ...,
        description="LIKE patterns to match (e.g., 'test-%', 'ratelimit-%')",
    )


class CleanupResponse(BaseModel):
    """Response for test cleanup."""

    deleted: int


@router.delete("/test-cleanup", response_model=CleanupResponse)
async def cleanup_test_sessions(
    body: CleanupRequest,
    db: Session = Depends(get_db),
) -> CleanupResponse:
    """Delete test sessions by project pattern (dev-only).

    Only available when AUTH_DISABLED=1. Used by E2E tests to clean up
    test data after runs.
    """
    if not _settings.auth_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Test cleanup only available in dev mode (AUTH_DISABLED=1)",
        )

    store = AgentsStore(db)
    deleted = store.delete_sessions_by_project_patterns(body.project_patterns)

    return CleanupResponse(deleted=deleted)
