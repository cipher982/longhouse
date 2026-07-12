"""Browser-owned timeline/session archive API routes.

This router is the cookie-auth presentation veneer for user-facing clients.
Most per-session inspection routes intentionally delegate into the canonical
``/api/agents/*`` service layer so browser and machine reads do not drift.
The browser-specific behavior that remains here is thread-card timeline
listing/streaming plus short-lived UI caches.
"""

from __future__ import annotations

import asyncio
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

import zerg.database as database_module
from zerg.database import catalog_db_dependency
from zerg.database import get_catalog_session_factory
from zerg.database import get_db
from zerg.database import get_session_factory
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.dependencies.browser_auth import get_current_browser_user_id_short_lived
from zerg.dependencies.browser_auth import require_current_browser_user_short_lived
from zerg.models.agents import AgentSession
from zerg.models.device_token import DeviceToken
from zerg.routers import agents_demo as _demo_router
from zerg.routers import agents_search as _search_router
from zerg.routers import agents_sessions as _sessions_router
from zerg.schemas.machines import MachineDirectoryEntry
from zerg.schemas.machines import MachineDirectoryResponse
from zerg.schemas.machines import WorkspaceSuggestion
from zerg.schemas.machines import WorkspaceSuggestionsResponse
from zerg.services.agents import AgentsStore
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import enrolled_machines
from zerg.services.catalog_read_gateway import machine_workspaces
from zerg.services.live_catalog_timeline import list_live_catalog_timeline
from zerg.services.live_catalog_timeline import stream_live_catalog_timeline
from zerg.services.machines_directory import build_machines_directory
from zerg.services.session_listing import SessionListingError
from zerg.services.session_shares import SessionShareError
from zerg.services.session_shares import resolve_session_share
from zerg.services.session_views import DemoSeedResponse
from zerg.services.session_views import FiltersResponse
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SemanticSearchResponse
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionActionResponse
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionLoopModeResponse
from zerg.services.session_views import SessionMobileTailResponse
from zerg.services.session_views import SessionNotificationWatchRequest
from zerg.services.session_views import SessionNotificationWatchResponse
from zerg.services.session_views import SessionPreviewResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsSummaryResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionTurnEnvelopeResponse
from zerg.services.session_views import SessionTurnsListResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_workspace import build_session_mobile_tail
from zerg.services.session_workspace import build_session_workspace
from zerg.services.session_workspace import get_legacy_workspace_session_factory
from zerg.services.session_workspace import resolve_session_sharer
from zerg.services.session_workspace_revision import load_session_workspace_revision
from zerg.services.storage_v2_workspace import build_storage_v2_workspace
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import TimelineSessionsListResponse
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser
from zerg.services.timeline_session_stream import stream_timeline_sessions_for_browser
from zerg.services.timeline_session_stream import validate_timeline_stream_contract
from zerg.services.workspace_suggestions import build_workspace_suggestions
from zerg.utils.server_timing import ServerTimingRecorder

logger = logging.getLogger(__name__)
_catalog_db_dependency = catalog_db_dependency()

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


def _legacy_catalog_db():
    if database_module.live_catalog_enabled():
        yield None
        return
    with get_catalog_session_factory()() as db:
        yield db


_catalog_read_db_dependency = get_db if _catalog_db_dependency is get_db else _legacy_catalog_db


def _legacy_machine_enrollments(db: Session, *, owner_id: int) -> list[dict[str, object]]:
    rows = db.query(DeviceToken).filter(DeviceToken.owner_id == owner_id, DeviceToken.revoked_at.is_(None)).all()
    return [
        {
            "device_id": row.device_id,
            "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]


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
def list_browser_machines(
    db: Session | None = Depends(_catalog_read_db_dependency),
    current_user=Depends(get_current_browser_user),
) -> MachineDirectoryResponse:
    """Browser machines directory. Same body shape as ``/api/agents/machines``."""
    owner_id = int(current_user.id)
    try:
        if database_module.live_catalog_enabled():
            enrollments = enrolled_machines(owner_id).get("enrollments", [])
        else:
            assert db is not None
            enrollments = _legacy_machine_enrollments(db, owner_id=owner_id)
    except CatalogReadError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
    entries = build_machines_directory(owner_id=owner_id, enrollments=enrollments)
    return MachineDirectoryResponse(machines=[MachineDirectoryEntry(**entry.to_response()) for entry in entries])


@router.get("/machines/{device_id}/workspaces", response_model=WorkspaceSuggestionsResponse)
def list_browser_machine_workspaces(
    device_id: str,
    limit: int = Query(12, ge=1, le=50, description="Max ranked workspaces to return"),
    days_back: int = Query(45, ge=1, le=180, description="Lookback window for recent sessions"),
    db: Session | None = Depends(_catalog_read_db_dependency),
    current_user=Depends(get_current_browser_user),
) -> WorkspaceSuggestionsResponse:
    """Browser launch-picker workspaces. Same body shape as ``/api/agents/machines/{id}/workspaces``."""
    owner_id = int(current_user.id)
    if database_module.live_catalog_enabled():
        try:
            payload = machine_workspaces(
                owner_id=owner_id,
                device_id=device_id,
                limit=limit,
                days_back=days_back,
            )
        except CatalogReadError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
        return WorkspaceSuggestionsResponse(
            device_id=device_id,
            workspaces=[WorkspaceSuggestion(**item) for item in payload.get("workspaces", [])],
        )

    assert db is not None
    entries = build_workspace_suggestions(
        db,
        owner_id=owner_id,
        device_id=device_id,
        limit=limit,
        days_back=days_back,
    )
    return WorkspaceSuggestionsResponse(
        device_id=device_id,
        workspaces=[WorkspaceSuggestion(**entry.to_response()) for entry in entries],
    )


@router.get("/sessions/semantic", response_model=SemanticSearchResponse)
async def semantic_search_timeline_sessions(
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
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
        include_test=include_test,
        days_back=days_back,
        limit=limit,
        context_mode=context_mode,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/recall", response_model=RecallResponse)
async def recall_timeline_sessions(
    request: Request,
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=20, description="Max matches"),
    context_turns: int = Query(2, ge=0, le=10, description="Context turns before/after match"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
):
    return await _search_router.recall_sessions(
        request=request,
        query=query,
        project=project,
        provider=provider,
        include_test=include_test,
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
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in otherwise default-hidden lists",
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
    db: Session | None = Depends(_catalog_read_db_dependency),
    current_user=Depends(get_current_browser_user),
):
    effective_limit = min(limit, 100)
    response.headers["X-Limit-Cap"] = "100"
    timing = ServerTimingRecorder()
    params = TimelineSessionListParams(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        hide_autonomous=hide_autonomous,
        include_automation=include_automation,
        device_id=device_id,
        days_back=days_back,
        query=query,
        limit=effective_limit,
        offset=offset,
        sort=sort,
        mode=mode,
        context_mode=context_mode,
    )
    if database_module.live_catalog_enabled():
        try:
            return await asyncio.to_thread(list_live_catalog_timeline, params=params)
        except CatalogReadError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        except ValueError as exc:
            if str(exc) == "search_requires_archive":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "archive_search_unavailable",
                        "message": "Timeline search is temporarily unavailable while the archive worker is offline.",
                    },
                ) from exc
            raise
    try:
        assert db is not None
        result = await list_timeline_sessions_for_browser(
            db=db,
            params=params,
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
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in otherwise default-hidden streams",
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
    params = TimelineSessionListParams(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        hide_autonomous=hide_autonomous,
        include_automation=include_automation,
        device_id=device_id,
        days_back=days_back,
        query=query,
        limit=effective_limit,
        offset=offset,
        sort=sort,
        mode=mode,
        context_mode=context_mode,
    )

    if database_module.live_catalog_enabled():
        stream = stream_live_catalog_timeline(
            request,
            params=params,
            skip_initial_replay=skip_initial_replay,
        )
    else:
        stream = stream_timeline_sessions_for_browser(
            request,
            session_factory=get_session_factory(),
            params=params,
            skip_initial_replay=skip_initial_replay,
            owner_id=current_user_id if isinstance(current_user_id, int) else None,
        )
    sse_response = EventSourceResponse(stream)
    sse_response.headers["X-Limit-Cap"] = "100"
    return sse_response


@router.get("/sessions/summary", response_model=SessionsSummaryResponse)
def list_timeline_session_summaries(
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
    include_automation: bool = Query(
        False,
        description="Include Hatch automation sessions in otherwise default-hidden summaries",
    ),
    db: Session = Depends(get_db),
):
    effective_limit = min(limit, 100)
    response.headers["X-Limit-Cap"] = "100"
    return _sessions_router.list_session_summaries(
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
        include_automation=include_automation,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreviewResponse)
def preview_timeline_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
):
    return _sessions_router.preview_session(
        session_id=session_id,
        last_n=last_n,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/filters", response_model=FiltersResponse)
def get_timeline_filters(
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
        filters = _sessions_router.get_filters(
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
    db: Session | None = Depends(_sessions_router.session_preferences_db_dependency),
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
    db: Session | None = Depends(_sessions_router.session_preferences_db_dependency),
):
    return await _sessions_router.set_session_loop_mode(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.patch("/sessions/{session_id}/notification-watch", response_model=SessionNotificationWatchResponse)
async def set_timeline_session_notification_watch(
    session_id: UUID,
    body: SessionNotificationWatchRequest,
    db: Session | None = Depends(_sessions_router.session_preferences_db_dependency),
):
    return await _sessions_router.set_session_notification_watch(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_timeline_session(
    session_id: UUID,
    response: Response,
    db: Session | None = Depends(_sessions_router.session_detail_db_dependency),
    current_user=Depends(get_current_browser_user),
):
    return _sessions_router.get_session(
        session_id=session_id,
        response=response,
        db=db,
        _auth=None,
        _single=None,
        owner_id=_browser_owner_id(current_user),
    )


@router.get("/sessions/{session_id}/thread", response_model=SessionThreadResponse)
def get_timeline_session_thread(
    session_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
):
    return _sessions_router.get_session_thread(
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


@router.get("/sessions/{session_id}/workflows")
def get_timeline_session_workflow_runs(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> dict:
    """List dynamic-workflow runs whose subagent threads live under this session.

    Browser-cookie-authenticated mirror of the machine-facing
    ``/agents/sessions/{id}/workflows``. Each entry is one collapsible workflow
    run node for the session detail UI.
    """
    store = AgentsStore(db)
    runs = store.list_workflow_runs_for_session(session_id)
    return {"session_id": str(session_id), "workflow_runs": runs}


@router.get("/sessions/{session_id}/graph")
def get_timeline_session_graph(
    session_id: UUID,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> dict:
    return _sessions_router.get_session_graph(
        session_id=session_id,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/workflows/{workflow_run_id}")
def get_timeline_workflow_run(
    workflow_run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_browser_user),
) -> dict:
    """Return one dynamic-workflow run's subagent threads (browser-auth mirror of
    ``/agents/workflows/{run_id}``)."""
    store = AgentsStore(db)
    run = store.get_workflow_run(workflow_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown workflow run: {workflow_run_id}")
    return run


@router.get("/sessions/{session_id}/turns/{turn_id}", response_model=SessionTurnEnvelopeResponse)
def get_timeline_session_turn(
    session_id: UUID,
    turn_id: int,
    db: Session = Depends(get_db),
):
    return _sessions_router.get_session_turn_detail(
        session_id=session_id,
        turn_id=turn_id,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/events")
async def get_timeline_session_events(
    session_id: UUID,
    thread_id: Optional[UUID] = Query(
        None,
        description="Thread lane to inspect; defaults to the primary session thread",
    ),
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    tool_name: Optional[str] = Query(None, description="Exact tool name filter, e.g. Bash"),
    query: Optional[str] = Query(None, description="Content search within session events"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next older page"),
    legacy_session_factory=Depends(get_legacy_workspace_session_factory),
    current_user=Depends(get_current_browser_user),
):
    storage_workspace = await build_storage_v2_workspace(
        session_id=session_id,
        owner_id=int(current_user.id),
        branch_mode=branch_mode,
        limit=limit,
        cursor=cursor,
    )
    if storage_workspace is not None:
        projection = storage_workspace["projection"]
        role_filter = {value.strip() for value in roles.split(",") if value.strip()} if roles else None
        events = [item["event"] for item in projection["items"] if item.get("kind") == "event" and item.get("event")]
        if role_filter is not None:
            events = [event for event in events if event.get("role") in role_filter]
        if tool_name is not None:
            events = [event for event in events if event.get("tool_name") == tool_name]
        if query is not None:
            needle = query.casefold()
            events = [event for event in events if needle in str(event.get("content_text") or "").casefold()]
        return {
            "events": events,
            "total": projection["total"],
            "branch_mode": branch_mode,
            "abandoned_events": projection["abandoned_events"],
            "generation_id": projection.get("generation_id"),
            "next_cursor": projection.get("next_cursor"),
            "has_more": projection.get("has_more", False),
        }

    def build_legacy_events():
        with legacy_session_factory() as db:
            return _sessions_router.get_session_events(
                session_id=session_id,
                thread_id=thread_id,
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

    return await asyncio.to_thread(build_legacy_events)


@router.get("/sessions/{session_id}/projection")
async def get_timeline_session_projection(
    session_id: UUID,
    response: Response,
    thread_id: Optional[UUID] = Query(
        None,
        description="Thread lane to project; defaults to the primary session thread",
    ),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    anchor: str = Query("start", description="Page anchor: start|tail"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next older page"),
    legacy_session_factory=Depends(get_legacy_workspace_session_factory),
    current_user=Depends(get_current_browser_user),
):
    storage_workspace = await build_storage_v2_workspace(
        session_id=session_id,
        owner_id=int(current_user.id),
        branch_mode=branch_mode,
        limit=limit,
        cursor=cursor,
    )
    if storage_workspace is not None:
        return storage_workspace["projection"]

    def build_legacy_projection():
        with legacy_session_factory() as db:
            return _sessions_router.get_session_projection(
                session_id=session_id,
                thread_id=thread_id,
                branch_mode=branch_mode,
                anchor=anchor,
                limit=limit,
                offset=offset,
                response=response,
                db=db,
                _auth=None,
                _single=None,
            )

    return await asyncio.to_thread(build_legacy_projection)


@router.get("/sessions/{session_id}/workspace")
async def get_timeline_session_workspace(
    session_id: UUID,
    response: Response,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next older page"),
    shared_by: Optional[int] = Query(
        None,
        ge=1,
        description=(
            "User id who shared this link. When set, the response includes a "
            "``sharer`` block with their display name for the 'Shared by' "
            "header pill. Ignored when the user no longer exists."
        ),
    ),
    share_token: Optional[str] = Query(
        None,
        description="Signed share token. When valid, this supersedes unsigned shared_by attribution.",
    ),
    legacy_session_factory=Depends(get_legacy_workspace_session_factory),
    current_user=Depends(get_current_browser_user),
):
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "no-store"
    storage_workspace = await build_storage_v2_workspace(
        session_id=session_id,
        owner_id=int(current_user.id),
        branch_mode=branch_mode,
        limit=limit,
        cursor=cursor,
    )
    if storage_workspace is not None:
        timing.apply(response)
        return storage_workspace

    def build_legacy_workspace() -> SessionWorkspaceResponse:
        with legacy_session_factory() as db:
            sharer = None
            if share_token:
                try:
                    resolved_share = resolve_session_share(
                        db,
                        token=share_token,
                        actor_user_id=int(current_user.id),
                        expected_session_id=session_id,
                        record_access=False,
                    )
                except SessionShareError as exc:
                    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
                sharer = resolved_share.sharer
            elif shared_by is not None:
                sharer = resolve_session_sharer(db, shared_by)
            if sharer is not None and int(current_user.id) == sharer.id:
                sharer = None
            return build_session_workspace(
                db=db,
                session_id=session_id,
                branch_mode=branch_mode,
                limit=limit,
                timing=timing,
                owner_id=int(current_user.id),
                sharer=sharer,
            )

    result = await asyncio.to_thread(build_legacy_workspace)
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/mobile-tail")
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
    cursor: Optional[str] = Query(None, description="Exclusive storage-v2 cursor for the next older page"),
    legacy_session_factory=Depends(get_legacy_workspace_session_factory),
    current_user=Depends(get_current_browser_user),
):
    timing = ServerTimingRecorder()
    response.headers["Cache-Control"] = "no-store"
    storage_workspace = await build_storage_v2_workspace(
        session_id=session_id,
        owner_id=int(current_user.id),
        branch_mode=branch_mode,
        limit=limit,
        cursor=cursor,
    )
    if storage_workspace is not None:
        timing.apply(response)
        return {
            "session": storage_workspace["session"],
            "projection": storage_workspace["projection"],
            "snapshot_event_id": storage_workspace["workspace_revision"]["latest_event_id"],
            "workspace_revision": storage_workspace["workspace_revision"],
        }

    def build_legacy_tail() -> SessionMobileTailResponse:
        with legacy_session_factory() as db:
            return build_session_mobile_tail(
                db=db,
                session_id=session_id,
                branch_mode=branch_mode,
                limit=limit,
                offset=offset,
                snapshot_event_id=snapshot_event_id,
                timing=timing,
                owner_id=int(current_user.id),
            )

    result = await asyncio.to_thread(build_legacy_tail)
    timing.apply(response)
    return result


@router.get("/sessions/{session_id}/export")
def export_timeline_session(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode for export: head|all"),
    db: Session = Depends(get_db),
) -> Response:
    return _sessions_router.export_session(
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
    """Lightweight signature for session workspace-visible state."""

    # The stream handshake seeds previous_sig from revision.signature and the
    # main loop compares against this helper, so this tuple shape must stay
    # identical to SessionWorkspaceRevision.signature.
    revision = load_session_workspace_revision(db, session_id)
    return revision.signature if revision is not None else ()


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
    known_workspace_fingerprint: str | None = None,
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
    replay_gap = bus.replay_gap(topic, since_seq=last_event_id)
    # Highest pubsub seq actually consumed by this subscription. Used as the
    # SSE id: so reconnects never skip an event the client hadn't seen.
    consumed_seq: int = 0
    consumed_payload: dict | None = None
    # If the requested cursor cannot be faithfully replayed, do not send a
    # partial ring replay. The client should reconcile from durable state, then
    # stay attached for future live messages.
    subscribe_since_seq = None if replay_gap else last_event_id
    with bus.subscribe(topic, since_seq=subscribe_since_seq) as subscription:
        if skip_initial and known_workspace_fingerprint:
            with session_factory() as db:
                revision = load_session_workspace_revision(db, session_id)
            if revision is None:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": "session_not_found"}),
                }
                return
            if revision.fingerprint == known_workspace_fingerprint:
                previous_sig = revision.signature
            else:
                skip_initial = False
        elif skip_initial:
            # Unsafe legacy callers do not get to skip without proving the
            # snapshot they rendered. First-party clients now send a known
            # workspace fingerprint; no-fingerprint callers get an initial
            # invalidation instead of waiting for a future pubsub wake.
            skip_initial = False

        if replay_gap:
            yield {
                "event": "replay_gap",
                "id": str(replay_gap.latest_seq) if replay_gap.latest_seq else None,
                "data": json.dumps(
                    {
                        "session_id": str(session_id),
                        "requested_seq": replay_gap.requested_seq,
                        "earliest_seq": replay_gap.earliest_seq,
                        "latest_seq": replay_gap.latest_seq,
                        "reason": replay_gap.reason,
                    }
                ),
            }
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


async def _live_catalog_workspace_stream(
    request: Request,
    *,
    session_id: UUID,
    skip_initial: bool,
    last_event_id: int | None,
):
    """Live-only invalidation stream; archive detail is fetched via a child."""

    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import topic_session

    yield {
        "event": "connected",
        "data": json.dumps(
            {
                "session_id": str(session_id),
                "server_now_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
            }
        ),
    }
    bus = get_pubsub()
    topic = topic_session(str(session_id))
    replay_gap = bus.replay_gap(topic, since_seq=last_event_id)
    subscribe_since_seq = None if replay_gap else last_event_id
    with bus.subscribe(topic, since_seq=subscribe_since_seq) as subscription:
        if replay_gap:
            yield {
                "event": "replay_gap",
                "id": str(replay_gap.latest_seq) if replay_gap.latest_seq else None,
                "data": json.dumps(
                    {
                        "session_id": str(session_id),
                        "requested_seq": replay_gap.requested_seq,
                        "earliest_seq": replay_gap.earliest_seq,
                        "latest_seq": replay_gap.latest_seq,
                        "reason": replay_gap.reason,
                    }
                ),
            }
        if not skip_initial:
            yield {
                "event": "workspace_changed",
                "data": json.dumps(
                    {
                        "session_id": str(session_id),
                        "server_now_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "pubsub_seq": 0,
                    }
                ),
            }
        while not await request.is_disconnected():
            message = await _wait_for_session_change(subscription)
            if message is None:
                yield {"event": "heartbeat", "data": json.dumps({"timestamp": _utc_now_z()})}
                continue
            preview = _workspace_transcript_preview_from_payload(message.payload)
            yield {
                "event": "workspace_changed",
                "id": str(message.seq),
                "data": json.dumps(
                    {
                        "session_id": str(session_id),
                        "server_now_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "server_fanout_at_ms": _workspace_server_fanout_at_ms(message.payload),
                        "pubsub_seq": message.seq,
                        "transcript_preview": preview,
                    }
                ),
            }


@timeline_stream_router.get("/sessions/{session_id}/workspace/stream")
async def stream_session_workspace(
    request: Request,
    session_id: UUID,
    skip_initial: bool = Query(
        False,
        description="When true, wait for first change before emitting workspace_changed.",
    ),
    known_workspace_fingerprint: str | None = Query(
        None,
        description="Fingerprint from the client's rendered workspace snapshot; when stale, skip_initial is ignored.",
    ),
) -> EventSourceResponse:
    """SSE stream that emits workspace_changed when the session's data mutates.

    The browser subscribes on session detail page load and uses each event to
    invalidate React Query caches — replacing the 5-second polling interval
    with event-driven refresh.
    """
    # SSE Last-Event-ID for replay on reconnect. Header is canonical; ignore
    # malformed values rather than 400ing the reconnect.
    last_event_id: int | None = None
    raw = request.headers.get("Last-Event-ID")
    if raw:
        try:
            last_event_id = max(0, int(raw))
        except ValueError:
            last_event_id = None

    if database_module.live_catalog_enabled():
        return EventSourceResponse(
            _live_catalog_workspace_stream(
                request,
                session_id=session_id,
                skip_initial=skip_initial,
                last_event_id=last_event_id,
            )
        )
    return EventSourceResponse(
        _session_workspace_stream(
            request,
            session_factory=get_session_factory(),
            session_id=session_id,
            skip_initial=skip_initial,
            last_event_id=last_event_id,
            known_workspace_fingerprint=known_workspace_fingerprint,
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
    known_workspace_fingerprint: str | None = Query(None),
) -> EventSourceResponse:
    """Canary-only SSE: same generator as the browser endpoint, token-auth.

    The always-on canary observer on the build host uses this; requires X-Canary-Token
    matching LONGHOUSE_CANARY_TOKEN. Admin users can still use the browser
    endpoint.
    """
    from zerg.routers.telemetry import canary_token_matches

    if not canary_token_matches(request):
        raise HTTPException(status_code=401, detail="canary token required")

    last_event_id: int | None = None
    raw = request.headers.get("Last-Event-ID")
    if raw:
        try:
            last_event_id = max(0, int(raw))
        except ValueError:
            last_event_id = None

    if database_module.live_catalog_enabled():
        return EventSourceResponse(
            _live_catalog_workspace_stream(
                request,
                session_id=session_id,
                skip_initial=skip_initial,
                last_event_id=last_event_id,
            )
        )
    return EventSourceResponse(
        _session_workspace_stream(
            request,
            session_factory=get_session_factory(),
            session_id=session_id,
            skip_initial=skip_initial,
            last_event_id=last_event_id,
            known_workspace_fingerprint=known_workspace_fingerprint,
        ),
    )
