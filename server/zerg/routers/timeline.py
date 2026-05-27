"""Browser-owned timeline/session archive API routes.

This router is the cookie-auth presentation veneer for user-facing clients.
Most per-session inspection routes intentionally delegate into the canonical
``/api/agents/*`` service layer so browser and machine reads do not drift.
The browser-specific behavior that remains here is thread-card timeline
listing/streaming plus short-lived UI caches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from threading import Lock
from time import monotonic
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.database import get_session_factory
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_auth import get_current_browser_user_id_short_lived
from zerg.dependencies.browser_auth import require_current_browser_user_short_lived
from zerg.models.agents import AgentSession
from zerg.routers import agents_demo as _demo_router
from zerg.routers import agents_search as _search_router
from zerg.routers import agents_sessions as _sessions_router
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.services.machines_directory import build_machines_directory
from zerg.services.session_listing import SessionListingError
from zerg.services.session_views import DemoSeedResponse
from zerg.services.session_views import EventsListResponse
from zerg.services.session_views import FiltersResponse
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SemanticSearchResponse
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionActionResponse
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionLoopModeResponse
from zerg.services.session_views import SessionMobileTailResponse
from zerg.services.session_views import SessionPreviewResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsSummaryResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionTurnEnvelopeResponse
from zerg.services.session_views import SessionTurnsListResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_workspace import build_session_mobile_tail
from zerg.services.session_workspace import build_session_workspace
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import TimelineSessionsListResponse
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser
from zerg.services.timeline_session_stream import stream_timeline_sessions_for_browser
from zerg.services.timeline_session_stream import validate_timeline_stream_contract
from zerg.utils.server_timing import ServerTimingRecorder

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/timeline",
    tags=["timeline"],
    dependencies=[Depends(get_current_browser_user), Depends(require_single_tenant)],
)
timeline_stream_router = APIRouter(
    prefix="/timeline",
    tags=["timeline"],
    dependencies=[Depends(require_current_browser_user_short_lived), Depends(require_single_tenant)],
)

TIMELINE_FILTERS_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _TimelineFiltersCacheEntry:
    expires_at: float
    response: FiltersResponse


_timeline_filters_cache: dict[tuple[str, int, str | None], _TimelineFiltersCacheEntry] = {}
_timeline_filters_cache_lock = Lock()


def _browser_owner_id(user) -> int | None:
    raw_owner_id = getattr(user, "id", None)
    if raw_owner_id is None:
        return None
    try:
        return int(raw_owner_id)
    except (TypeError, ValueError):
        return None


def _timeline_filters_cache_key(db: Session, *, days_back: int) -> tuple[str, int, str | None]:
    bind = db.get_bind()
    bind_url = getattr(bind, "url", None)
    bind_key = str(bind_url) if bind_url is not None else f"bind:{id(bind)}"
    latest_session_update = db.query(func.max(AgentSession.updated_at)).scalar()
    latest_update_key = latest_session_update.isoformat() if latest_session_update is not None else None
    return bind_key, days_back, latest_update_key


@router.get("/machines", response_model=MachineDirectoryResponse)
async def list_browser_machines(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> MachineDirectoryResponse:
    """Browser machines directory. Same body shape as ``/api/agents/machines``."""
    entries = build_machines_directory(db, owner_id=int(current_user.id))
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


@router.get("/sessions/semantic", response_model=SemanticSearchResponse)
async def semantic_search_timeline_sessions(
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    days_back: int = Query(14, ge=1, le=365, description="Days to look back"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
):
    return await _search_router.semantic_search_sessions(
        query=query,
        project=project,
        provider=provider,
        environment=environment,
        days_back=days_back,
        limit=limit,
        context_mode=context_mode,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/recall", response_model=RecallResponse)
async def recall_timeline_sessions(
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=20, description="Max matches"),
    context_turns: int = Query(2, ge=0, le=10, description="Context turns before/after match"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
):
    return await _search_router.recall_sessions(
        query=query,
        project=project,
        since_days=since_days,
        max_results=max_results,
        context_turns=context_turns,
        context_mode=context_mode,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions", response_model=TimelineSessionsListResponse)
async def list_timeline_sessions(
    response: Response,
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    hide_autonomous: bool = Query(
        True,
        description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)",
    ),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, description="Max results (server clamps to 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort: Optional[str] = Query(
        None,
        description="Sort order: relevance|recency|balanced. Default: recency if no query, relevance if query present.",
    ),
    mode: Optional[str] = Query("lexical", description="Search mode: lexical|semantic|hybrid. Default: lexical."),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    effective_limit = min(limit, 100)
    response.headers["X-Limit-Cap"] = "100"
    timing = ServerTimingRecorder()
    try:
        result = await list_timeline_sessions_for_browser(
            db=db,
            params=TimelineSessionListParams(
                project=project,
                provider=provider,
                environment=environment,
                include_test=include_test,
                hide_autonomous=hide_autonomous,
                device_id=device_id,
                days_back=days_back,
                query=query,
                limit=effective_limit,
                offset=offset,
                sort=sort,
                mode=mode,
                context_mode=context_mode,
            ),
            timing=timing,
            owner_id=_browser_owner_id(current_user),
        )
    except SessionListingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if result.compatibility_raw:
        compat_response = JSONResponse(
            content=result.response.model_dump(mode="json"),
            headers=result.headers,
        )
        compat_response.headers["X-Limit-Cap"] = "100"
        timing.apply(compat_response)
        return compat_response

    timing.apply(response)
    return result.response


@timeline_stream_router.get("/sessions/stream")
async def stream_timeline_sessions(
    request: Request,
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    hide_autonomous: bool = Query(
        True,
        description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)",
    ),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, description="Max results (server clamps to 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort: Optional[str] = Query(
        None,
        description="Sort order: relevance|recency|balanced. Default: recency if no query, relevance if query present.",
    ),
    mode: Optional[str] = Query("lexical", description="Search mode: lexical|semantic|hybrid. Default: lexical."),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    skip_initial_replay: bool = Query(
        False,
        description="When true, subscribe without immediately replaying the already-fresh default timeline snapshot.",
    ),
    current_user_id: int = Depends(get_current_browser_user_id_short_lived),
) -> EventSourceResponse:
    try:
        validate_timeline_stream_contract(query=query, sort=sort, mode=mode)
    except SessionListingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    effective_limit = min(limit, 100)
    session_factory = get_session_factory()
    params = TimelineSessionListParams(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        hide_autonomous=hide_autonomous,
        device_id=device_id,
        days_back=days_back,
        query=query,
        limit=effective_limit,
        offset=offset,
        sort=sort,
        mode=mode,
        context_mode=context_mode,
    )

    sse_response = EventSourceResponse(
        stream_timeline_sessions_for_browser(
            request,
            session_factory=session_factory,
            params=params,
            skip_initial_replay=skip_initial_replay,
            owner_id=current_user_id if isinstance(current_user_id, int) else None,
        )
    )
    sse_response.headers["X-Limit-Cap"] = "100"
    return sse_response


@router.get("/sessions/summary", response_model=SessionsSummaryResponse)
async def list_timeline_session_summaries(
    response: Response,
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, description="Max results (server clamps to 100)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    hide_autonomous: bool = Query(
        True,
        description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)",
    ),
    db: Session = Depends(get_db),
):
    effective_limit = min(limit, 100)
    response.headers["X-Limit-Cap"] = "100"
    return await _sessions_router.list_session_summaries(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        device_id=device_id,
        days_back=days_back,
        query=query,
        limit=effective_limit,
        offset=offset,
        hide_autonomous=hide_autonomous,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreviewResponse)
async def preview_timeline_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
):
    return await _sessions_router.preview_session(
        session_id=session_id,
        last_n=last_n,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/filters", response_model=FiltersResponse)
async def get_timeline_filters(
    response: Response,
    days_back: int = Query(90, ge=1, le=365, description="Days to look back for distinct values"),
    db: Session = Depends(get_db),
):
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "private, max-age=60"

    cache_key = _timeline_filters_cache_key(db, days_back=days_back)
    now = monotonic()
    with _timeline_filters_cache_lock:
        cached = _timeline_filters_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            timing.record("cache_hit", 0.1)
            timing.apply(response)
            return cached.response

    with timing.span("distinct_filters"):
        filters = await _sessions_router.get_filters(
            days_back=days_back,
            response=response,
            db=db,
            _auth=None,
            _single=None,
        )

    with _timeline_filters_cache_lock:
        _timeline_filters_cache[cache_key] = _TimelineFiltersCacheEntry(
            expires_at=now + TIMELINE_FILTERS_CACHE_TTL_SECONDS,
            response=filters,
        )

    timing.apply(response)
    return filters


@router.post("/demo", response_model=DemoSeedResponse)
async def seed_timeline_demo_sessions(
    replace: bool = Query(False, description="Delete existing demo sessions before seeding fresh demo data"),
    db: Session = Depends(get_db),
):
    return await _demo_router.seed_demo_sessions(replace=replace, db=db, _auth=None, _single=None)


@router.post("/sessions/{session_id}/action", response_model=SessionActionResponse)
async def set_timeline_session_action(
    session_id: UUID,
    body: SessionActionRequest,
    db: Session = Depends(get_db),
):
    return await _sessions_router.set_session_action(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.patch("/sessions/{session_id}/loop-mode", response_model=SessionLoopModeResponse)
async def set_timeline_session_loop_mode(
    session_id: UUID,
    body: SessionLoopModeRequest,
    db: Session = Depends(get_db),
):
    return await _sessions_router.set_session_loop_mode(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_timeline_session(
    session_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    return await _sessions_router.get_session(
        session_id=session_id,
        response=response,
        db=db,
        _auth=None,
        _single=None,
        owner_id=_browser_owner_id(current_user),
    )


@router.get("/sessions/{session_id}/thread", response_model=SessionThreadResponse)
async def get_timeline_session_thread(
    session_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    return await _sessions_router.get_session_thread(
        session_id=session_id,
        response=response,
        db=db,
        _auth=None,
        _single=None,
        owner_id=_browser_owner_id(current_user),
    )


@router.get("/sessions/{session_id}/turns", response_model=SessionTurnsListResponse)
async def get_timeline_session_turns(
    session_id: UUID,
    response: Response,
    limit: int = Query(50, ge=1, description="Max turns to return (server clamps to 100)"),
    offset: int = Query(0, ge=0, description="Offset within the stable per-session turn order"),
    order: str = Query("asc", description="Turn order: asc|desc"),
    db: Session = Depends(get_db),
):
    normalized_order = str(order or "asc").strip().lower()
    if normalized_order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order must be one of: asc, desc")

    effective_limit = min(limit, 100)
    response.headers["X-Limit-Cap"] = "100"
    return await _sessions_router.get_session_turns(
        session_id=session_id,
        limit=effective_limit,
        offset=offset,
        order=normalized_order,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/turns/{turn_id}", response_model=SessionTurnEnvelopeResponse)
async def get_timeline_session_turn(
    session_id: UUID,
    turn_id: int,
    db: Session = Depends(get_db),
):
    return await _sessions_router.get_session_turn_detail(
        session_id=session_id,
        turn_id=turn_id,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/events", response_model=EventsListResponse)
async def get_timeline_session_events(
    session_id: UUID,
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    tool_name: Optional[str] = Query(None, description="Exact tool name filter, e.g. Bash"),
    query: Optional[str] = Query(None, description="Content search within session events"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
):
    return await _sessions_router.get_session_events(
        session_id=session_id,
        roles=roles,
        tool_name=tool_name,
        query=query,
        context_mode=context_mode,
        branch_mode=branch_mode,
        anchor=anchor,
        limit=limit,
        offset=offset,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/projection", response_model=SessionProjectionResponse)
async def get_timeline_session_projection(
    session_id: UUID,
    response: Response,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    db: Session = Depends(get_db),
):
    return await _sessions_router.get_session_projection(
        session_id=session_id,
        branch_mode=branch_mode,
        anchor=anchor,
        limit=limit,
        offset=offset,
        response=response,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/workspace", response_model=SessionWorkspaceResponse)
async def get_timeline_session_workspace(
    session_id: UUID,
    response: Response,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "no-store"
    result = build_session_workspace(
        db=db,
        session_id=session_id,
        branch_mode=branch_mode,
        limit=limit,
        timing=timing,
        owner_id=int(current_user.id),
    )
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/mobile-tail", response_model=SessionMobileTailResponse)
async def get_timeline_session_mobile_tail(
    session_id: UUID,
    response: Response,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(50, ge=1, le=200, description="Max projected tail items"),
    offset: int = Query(0, ge=0, description="Items before the latest tail window"),
    snapshot_event_id: Optional[int] = Query(
        None,
        description="Previous snapshot marker for older-page drift detection",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "no-store"
    result = build_session_mobile_tail(
        db=db,
        session_id=session_id,
        branch_mode=branch_mode,
        limit=limit,
        offset=offset,
        snapshot_event_id=snapshot_event_id,
        timing=timing,
        owner_id=int(current_user.id),
    )
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/export")
async def export_timeline_session(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode for export: head|all"),
    db: Session = Depends(get_db),
) -> Response:
    return await _sessions_router.export_session(
        session_id=session_id,
        branch_mode=branch_mode,
        db=db,
        _auth=None,
        _single=None,
    )


# ---------------------------------------------------------------------------
# Session workspace SSE stream
# ---------------------------------------------------------------------------

WORKSPACE_STREAM_HEARTBEAT_SECONDS = 30.0
WORKSPACE_STREAM_CHANGE_WAIT_SECONDS = 5.0


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_workspace_signature(
    db: Session,
    session_id: UUID,
) -> tuple:
    """Lightweight signature for a session's workspace state.

    Returns a tuple of scalar values that change whenever the workspace has
    new data the browser should display.  Comparing successive tuples is
    cheap — a single DB query touching indexed columns only.
    """
    from zerg.models.agents import AgentEvent
    from zerg.models.agents import AgentSession
    from zerg.models.agents import SessionLivePreview
    from zerg.models.agents import SessionRuntimeState

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return ()

    # Session-identity-kernel cleanup: ``thread_root_session_id`` was dropped.
    # Treat each session as its own thread root for the workspace signature.
    thread_root_id = session.id
    thread_sessions = db.query(AgentSession.id, AgentSession.updated_at).filter(AgentSession.id == thread_root_id).all()
    thread_session_ids = [str(row.id) for row in thread_sessions]
    latest_session_updated = max((row.updated_at for row in thread_sessions if row.updated_at), default=None)

    latest_event_id_query = db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id.in_(thread_session_ids))
    latest_event_id = latest_event_id_query.scalar() or 0

    # Latest event emitted_at (the provider/engine timestamp on the newest event).
    # This feeds client beacons so we can measure true end-to-end latency.
    latest_event_timestamp_column = func.max(AgentEvent.timestamp)
    agent_event_session_filter = AgentEvent.session_id.in_(thread_session_ids)
    latest_event_timestamp_query = db.query(latest_event_timestamp_column).filter(agent_event_session_filter)
    latest_event_timestamp = latest_event_timestamp_query.scalar()

    # Latest runtime signal across thread — the runtime state row advances on every
    # hook-driven phase change, so this replaces the old SessionPresence anchor.
    latest_runtime_signal = (
        db.query(func.max(SessionRuntimeState.updated_at)).filter(SessionRuntimeState.session_id.in_(thread_session_ids)).scalar()
    )

    runtime_version_sum = (
        db.query(func.sum(SessionRuntimeState.runtime_version)).filter(SessionRuntimeState.session_id.in_(thread_session_ids)).scalar()
    ) or 0

    live_preview_updated_at_column = func.max(SessionLivePreview.preview_updated_at)
    live_preview_session_filter = SessionLivePreview.session_id.in_(thread_session_ids)
    live_preview_updated_at = (
        db.query(live_preview_updated_at_column)
        .filter(live_preview_session_filter)
        .filter(SessionLivePreview.superseded_at.is_(None))
        .scalar()
    )

    return (
        str(thread_root_id),
        latest_session_updated,
        latest_event_id,
        latest_runtime_signal,
        runtime_version_sum,
        len(thread_session_ids),
        latest_event_timestamp,
        live_preview_updated_at,
    )


def _load_workspace_transcript_preview_payload(
    db: Session,
    *,
    session_id: UUID,
    latest_event_id: int | None,
    latest_event_timestamp: datetime | None,
    now: datetime,
) -> dict | None:
    """Return a hot-path transcript preview payload for the focused session."""
    from zerg.models.agents import AgentEvent
    from zerg.services.provisional_events import EVENT_ORIGIN_DURABLE
    from zerg.services.provisional_events import TranscriptPreview
    from zerg.services.provisional_events import durable_transcript_event_predicate

    # Default path reads the compact projection; the legacy observation scan is only a kill-switch fallback.
    from zerg.services.provisional_events import load_active_provisional_preview_map
    from zerg.services.session_views import build_session_transcript_preview_response

    preview = load_active_provisional_preview_map(db, [session_id]).get(str(session_id))
    if preview is None and latest_event_id:
        event = (
            db.query(AgentEvent)
            .filter(AgentEvent.id == latest_event_id)
            .filter(durable_transcript_event_predicate())
            .filter(AgentEvent.role == "assistant")
            .filter(AgentEvent.tool_name.is_(None))
            .first()
        )
        if event is not None:
            text = str(event.content_text or "").strip()
            if text:
                preview = TranscriptPreview(
                    event_id=int(event.id),
                    text=text,
                    event_origin=EVENT_ORIGIN_DURABLE,
                    timestamp=event.timestamp,
                    provisional_cursor=None,
                    provisional_complete=True,
                )
    response = build_session_transcript_preview_response(
        preview,
        last_activity_at=latest_event_timestamp,
        now=now,
    )
    if response is None or response.is_stale or not response.text.strip():
        return None
    return response.model_dump(mode="json")


def _workspace_transcript_preview_from_payload(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    preview = payload.get("transcript_preview")
    if not isinstance(preview, dict):
        return None
    text = str(preview.get("text") or "").strip()
    if not text or preview.get("is_stale"):
        return None
    return preview


def _workspace_server_fanout_at_ms(payload: dict | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidate = payload.get("server_fanout_at_ms")
    return candidate if isinstance(candidate, int) else None


def _workspace_latest_event_ts_ms(signature: tuple) -> int | None:
    latest_event_ts = signature[6] if len(signature) > 6 else None
    if latest_event_ts is None:
        return None
    ts = latest_event_ts if latest_event_ts.tzinfo else latest_event_ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def _workspace_render_event_id(signature: tuple, transcript_preview_payload: dict | None) -> int:
    preview_id = _workspace_preview_event_id(transcript_preview_payload)
    if preview_id is not None:
        return -abs(preview_id)
    return int(signature[2] or 0)


def _workspace_render_event_ts_ms(signature: tuple, transcript_preview_payload: dict | None) -> int | None:
    preview_ts = _workspace_preview_ts_ms(transcript_preview_payload)
    if preview_ts is not None:
        return preview_ts
    return _workspace_latest_event_ts_ms(signature)


def _workspace_preview_event_id(transcript_preview_payload: dict | None) -> int | None:
    if not isinstance(transcript_preview_payload, dict):
        return None
    if transcript_preview_payload.get("event_origin") != "live_provisional":
        return None
    try:
        return int(transcript_preview_payload.get("event_id"))
    except (TypeError, ValueError):
        return None


def _workspace_preview_ts_ms(transcript_preview_payload: dict | None) -> int | None:
    if not isinstance(transcript_preview_payload, dict):
        return None
    if transcript_preview_payload.get("event_origin") != "live_provisional":
        return None
    raw = transcript_preview_payload.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


async def _session_workspace_stream(
    request: Request,
    *,
    session_factory: sessionmaker,
    session_id: UUID,
    skip_initial: bool,
    last_event_id: int | None = None,
):
    """SSE generator that emits workspace_changed when a session's data mutates.

    Subscribes to the per-session pubsub topic so wake is O(1) on publish
    instead of waking every SSE subscriber on every write. DB signature is
    still consulted as the source of truth for what changed, avoiding false
    positives from publishes that don't touch workspace-visible state.

    last_event_id: if provided, the subscription starts from ring-buffered
    events after that seq, letting reconnects catch up without a DB hit.
    """
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import topic_session

    previous_sig: tuple | None = None
    last_heartbeat = monotonic()

    yield {
        "event": "connected",
        "data": json.dumps(
            {
                "session_id": str(session_id),
                "server_now_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
            }
        ),
    }

    wait_start: float | None = None
    bus = get_pubsub()
    topic = topic_session(str(session_id))
    # Highest pubsub seq actually consumed by this subscription. Used as the
    # SSE id: so reconnects never skip an event the client hadn't seen.
    consumed_seq: int = 0
    consumed_payload: dict | None = None
    with bus.subscribe(topic, since_seq=last_event_id) as subscription:
        while True:
            if await request.is_disconnected():
                break

            if skip_initial:
                skip_initial = False
                wait_start = monotonic()
                woke_msg = await _wait_for_session_change(subscription)
                if woke_msg:
                    consumed_seq = woke_msg.seq
                    consumed_payload = woke_msg.payload
                else:
                    consumed_payload = None
                continue

            with session_factory() as db:
                current_sig = _load_workspace_signature(db, session_id)

            if not current_sig:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": "session_not_found"}),
                }
                break

            consumed_preview_payload = _workspace_transcript_preview_from_payload(consumed_payload)
            if previous_sig is not None and current_sig == previous_sig:
                if consumed_preview_payload is not None:
                    now = datetime.now(timezone.utc)
                    yield {
                        "event": "workspace_changed",
                        "id": str(consumed_seq) if consumed_seq else None,
                        "data": json.dumps(
                            {
                                "session_id": str(session_id),
                                "latest_event_id": _workspace_render_event_id(current_sig, consumed_preview_payload),
                                "thread_session_count": current_sig[5],
                                "detect_ms": round((monotonic() - wait_start) * 1000, 1) if wait_start else 0,
                                "latest_event_emitted_at_ms": _workspace_render_event_ts_ms(
                                    current_sig,
                                    consumed_preview_payload,
                                ),
                                "server_fanout_at_ms": _workspace_server_fanout_at_ms(consumed_payload),
                                "server_now_ms": int(now.timestamp() * 1000),
                                "pubsub_seq": consumed_seq,
                                "transcript_preview": consumed_preview_payload,
                            }
                        ),
                    }
                    last_heartbeat = monotonic()
                    wait_start = monotonic()
                    woke_msg = await _wait_for_session_change(subscription)
                    if woke_msg:
                        consumed_seq = woke_msg.seq
                        consumed_payload = woke_msg.payload
                    else:
                        consumed_payload = None
                    continue

                now = monotonic()
                if now - last_heartbeat >= WORKSPACE_STREAM_HEARTBEAT_SECONDS:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": _utc_now_z()}),
                    }
                    last_heartbeat = now
                wait_start = monotonic()
                woke_msg = await _wait_for_session_change(subscription)
                if woke_msg:
                    consumed_seq = woke_msg.seq
                    consumed_payload = woke_msg.payload
                else:
                    consumed_payload = None
                continue

            old_sig = previous_sig
            previous_sig = current_sig

            detect_ms = round((monotonic() - wait_start) * 1000, 1) if wait_start else 0
            latest_event_ts = current_sig[6] if len(current_sig) > 6 else None
            latest_event_id = int(current_sig[2] or 0)
            previous_latest_event_id = int(old_sig[2] or 0) if old_sig is not None else latest_event_id
            now = datetime.now(timezone.utc)
            transcript_preview_payload = consumed_preview_payload
            live_preview_signature = current_sig[7] if len(current_sig) > 7 else None
            previous_live_preview_signature = old_sig[7] if old_sig is not None and len(old_sig) > 7 else None
            changed_durable_event = old_sig is not None and latest_event_id != previous_latest_event_id
            initial_pubsub_event = old_sig is None and consumed_seq
            durable_event_changed = bool(latest_event_id and (changed_durable_event or initial_pubsub_event))
            preview_signature_changed = live_preview_signature != previous_live_preview_signature
            live_preview_changed = bool(live_preview_signature and preview_signature_changed)
            if transcript_preview_payload is None and (live_preview_changed or durable_event_changed):
                with session_factory() as db:
                    transcript_preview_payload = _load_workspace_transcript_preview_payload(
                        db,
                        session_id=session_id,
                        latest_event_id=latest_event_id,
                        latest_event_timestamp=latest_event_ts,
                        now=now,
                    )
            server_fanout_at_ms = _workspace_server_fanout_at_ms(consumed_payload)
            # consumed_seq is the seq of the publish that woke this cycle —
            # safer than peek_latest_seq which can race ahead of our DB read.
            # On first emit before any wake (initial snapshot), fall back to 0.
            yield {
                "event": "workspace_changed",
                "id": str(consumed_seq) if consumed_seq else None,
                "data": json.dumps(
                    {
                        "session_id": str(session_id),
                        "latest_event_id": _workspace_render_event_id(current_sig, transcript_preview_payload),
                        "thread_session_count": current_sig[5],
                        "detect_ms": detect_ms,
                        "latest_event_emitted_at_ms": _workspace_render_event_ts_ms(
                            current_sig,
                            transcript_preview_payload,
                        ),
                        "server_fanout_at_ms": server_fanout_at_ms,
                        "server_now_ms": int(now.timestamp() * 1000),
                        "pubsub_seq": consumed_seq,
                        "transcript_preview": transcript_preview_payload,
                    }
                ),
            }

            now = monotonic()
            last_heartbeat = now

            wait_start = monotonic()
            woke_msg = await _wait_for_session_change(subscription)
            if woke_msg:
                consumed_seq = woke_msg.seq
                consumed_payload = woke_msg.payload
            else:
                consumed_payload = None


async def _wait_for_session_change(subscription):
    """Wait for a publish or a short tick. Returns the message that woke us, if any."""
    return await subscription.next_message(timeout=WORKSPACE_STREAM_CHANGE_WAIT_SECONDS)


@timeline_stream_router.get("/sessions/{session_id}/workspace/stream")
async def stream_session_workspace(
    request: Request,
    session_id: UUID,
    skip_initial: bool = Query(
        False,
        description="When true, wait for first change before emitting workspace_changed.",
    ),
) -> EventSourceResponse:
    """SSE stream that emits workspace_changed when the session's data mutates.

    The browser subscribes on session detail page load and uses each event to
    invalidate React Query caches — replacing the 5-second polling interval
    with event-driven refresh.
    """
    session_factory = get_session_factory()

    # SSE Last-Event-ID for replay on reconnect. Header is canonical; ignore
    # malformed values rather than 400ing the reconnect.
    last_event_id: int | None = None
    raw = request.headers.get("Last-Event-ID")
    if raw:
        try:
            last_event_id = max(0, int(raw))
        except ValueError:
            last_event_id = None

    return EventSourceResponse(
        _session_workspace_stream(
            request,
            session_factory=session_factory,
            session_id=session_id,
            skip_initial=skip_initial,
            last_event_id=last_event_id,
        ),
    )


# -----------------------------------------------------------------------------
# Canary-only SSE: token-auth variant of workspace/stream for background probes.
# Lives on a separate router so it doesn't inherit the browser cookie guard.
# -----------------------------------------------------------------------------

canary_stream_router = APIRouter(prefix="/canary", tags=["canary"])


@canary_stream_router.get("/sessions/{session_id}/workspace/stream")
async def stream_canary_workspace(
    request: Request,
    session_id: UUID,
    skip_initial: bool = Query(False),
) -> EventSourceResponse:
    """Canary-only SSE: same generator as the browser endpoint, token-auth.

    The always-on canary observer on cube uses this; requires X-Canary-Token
    matching LONGHOUSE_CANARY_TOKEN. Admin users can still use the browser
    endpoint.
    """
    from zerg.routers.telemetry import canary_token_matches

    if not canary_token_matches(request):
        raise HTTPException(status_code=401, detail="canary token required")

    session_factory = get_session_factory()

    last_event_id: int | None = None
    raw = request.headers.get("Last-Event-ID")
    if raw:
        try:
            last_event_id = max(0, int(raw))
        except ValueError:
            last_event_id = None

    return EventSourceResponse(
        _session_workspace_stream(
            request,
            session_factory=session_factory,
            session_id=session_id,
            skip_initial=skip_initial,
            last_event_id=last_event_id,
        ),
    )
