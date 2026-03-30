"""Browser-owned timeline/session archive API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
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
from pydantic import Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.models.agents import AgentSession
from zerg.routers import agents_briefings as _briefings_router
from zerg.routers import agents_demo as _demo_router
from zerg.routers import agents_search as _search_router
from zerg.routers import agents_sessions as _sessions_router
from zerg.services.agents_store import AgentsStore
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_views import BriefingResponse
from zerg.services.session_views import DemoSeedResponse
from zerg.services.session_views import EventsListResponse
from zerg.services.session_views import FiltersResponse
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SemanticSearchResponse
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionActionResponse
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionLoopModeResponse
from zerg.services.session_views import SessionPreviewResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsSummaryResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import SessionWorkspaceResponse
from zerg.services.session_views import build_session_response
from zerg.services.session_views import load_presence_map
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.session_views import resolve_runtime_overlay
from zerg.utils.server_timing import ServerTimingRecorder
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/timeline",
    tags=["timeline"],
    dependencies=[Depends(get_current_browser_user), Depends(require_single_tenant)],
)

TIMELINE_STREAM_CHANGE_WAIT_SECONDS = 5.0
TIMELINE_STREAM_HEARTBEAT_SECONDS = 30.0
TIMELINE_FILTERS_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _TimelineFiltersCacheEntry:
    expires_at: float
    response: FiltersResponse


_timeline_filters_cache: dict[tuple[str, int, str | None], _TimelineFiltersCacheEntry] = {}
_timeline_filters_cache_lock = Lock()


class TimelineSessionCardResponse(UTCBaseModel):
    thread_id: str = Field(..., description="Logical thread/task root UUID")
    timeline_anchor_at: datetime | None = Field(None, description="Anchor used for timeline ordering and grouping")
    head: SessionResponse
    detail: SessionResponse
    root: SessionResponse
    continuation_count: int = Field(..., description="Concrete continuation count in this logical thread")
    started_origin_label: str | None = Field(None, description="Origin label for where the thread started")
    head_origin_label: str | None = Field(None, description="Origin label for the current writable head")


class TimelineSessionsListResponse(UTCBaseModel):
    sessions: list[TimelineSessionCardResponse]
    total: int
    has_real_sessions: bool = True


def _session_payload_signature(session: TimelineSessionCardResponse) -> tuple[dict, str]:
    payload = session.model_dump(mode="json")
    return payload, json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _has_real_sessions(db: Session, *, total: int) -> bool:
    if total == 0:
        return True
    return (
        db.query(AgentSession.id).filter((AgentSession.device_id != "demo-mac") | (AgentSession.device_id.is_(None))).limit(1).first()
        is not None
    )


def _build_session_response_map(
    *,
    db: Session,
    session_ids: list[str],
) -> dict[str, SessionResponse]:
    if not session_ids:
        return {}

    store = AgentsStore(db)
    uuid_ids = [UUID(session_id) for session_id in session_ids]
    sessions = db.query(AgentSession).filter(AgentSession.id.in_(uuid_ids)).all()
    if not sessions:
        return {}

    activity_map = store.get_last_activity_map([session.id for session in sessions])
    presence_map = load_presence_map(db, [session.id for session in sessions])
    runtime_state_map = load_runtime_state_map(db, [session.id for session in sessions])
    first_user_map = store.get_first_message_map([session.id for session in sessions], role="user", max_len=80)
    thread_cache: dict[str, tuple[str, int]] = store.batch_thread_meta(sessions)
    now = datetime.now(timezone.utc)

    response_map: dict[str, SessionResponse] = {}
    for session in sessions:
        response = build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            last_activity_at=normalize_utc_datetime(activity_map.get(session.id) or session.ended_at or session.started_at),
            runtime_overlay=resolve_runtime_overlay(
                session,
                last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
                presence_map=presence_map,
                runtime_state_map=runtime_state_map,
                now=now,
            ),
            first_user_message=first_user_map.get(session.id),
        )
        response_map[response.id] = response
    return response_map


def _build_timeline_cards_from_thread_rows(
    *,
    db: Session,
    thread_rows: tuple[tuple[str, str, datetime | None], ...],
) -> list[TimelineSessionCardResponse]:
    if not thread_rows:
        return []

    representative_ids = [session_id for _thread_id, session_id, _thread_anchor in thread_rows]
    response_map = _build_session_response_map(db=db, session_ids=representative_ids)
    representative_rows = [(thread_id, response_map.get(session_id), thread_anchor) for thread_id, session_id, thread_anchor in thread_rows]
    supplemental_ids = sorted(
        (
            {detail.thread_root_session_id for _thread_id, detail, _thread_anchor in representative_rows if detail is not None}
            | {detail.thread_head_session_id for _thread_id, detail, _thread_anchor in representative_rows if detail is not None}
        )
        - response_map.keys()
    )
    response_map.update(_build_session_response_map(db=db, session_ids=supplemental_ids))

    cards: list[TimelineSessionCardResponse] = []
    for thread_id, representative, thread_anchor in representative_rows:
        if representative is None:
            continue
        head = response_map.get(representative.thread_head_session_id, representative)
        root = response_map.get(representative.thread_root_session_id, representative)
        cards.append(
            TimelineSessionCardResponse(
                thread_id=thread_id,
                timeline_anchor_at=(
                    thread_anchor
                    or representative.timeline_anchor_at
                    or head.timeline_anchor_at
                    or representative.last_activity_at
                    or head.last_activity_at
                    or head.started_at
                ),
                head=head,
                detail=head,
                root=root,
                continuation_count=head.thread_continuation_count or representative.thread_continuation_count or 1,
                started_origin_label=root.origin_label or root.environment,
                head_origin_label=head.origin_label or head.environment,
            )
        )
    return cards


def _effective_stream_sort(query: str | None, sort: str | None) -> str:
    return sort or ("relevance" if query else "recency")


def _stream_supports_preflight(*, query: str | None, sort: str | None, mode: str | None) -> bool:
    effective_sort = _effective_stream_sort(query, sort)
    return query is None and mode in (None, "lexical") and effective_sort == "recency"


def _timeline_filters_cache_key(db: Session, *, days_back: int) -> tuple[str, int, str | None]:
    bind = db.get_bind()
    bind_url = getattr(bind, "url", None)
    bind_key = str(bind_url) if bind_url is not None else f"bind:{id(bind)}"
    latest_session_update = db.query(func.max(AgentSession.updated_at)).scalar()
    latest_update_key = latest_session_update.isoformat() if latest_session_update is not None else None
    return bind_key, days_back, latest_update_key


def _validate_timeline_stream_contract(*, query: str | None, sort: str | None, mode: str | None) -> None:
    if _stream_supports_preflight(query=query, sort=sort, mode=mode):
        return
    raise HTTPException(
        status_code=400,
        detail="Timeline session stream only supports the default no-query lexical recency contract.",
    )


def _load_timeline_stream_window_signature(
    *,
    db: Session,
    project: Optional[str],
    provider: Optional[str],
    environment: Optional[str],
    include_test: bool,
    hide_autonomous: bool,
    device_id: Optional[str],
    days_back: int,
    query: Optional[str],
    limit: int,
    offset: int,
    context_mode: str,
) -> tuple[tuple[str, datetime | None, datetime | None, datetime | None, datetime | None, int, datetime | None], ...]:
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    _, rows = store.list_timeline_thread_window_signature(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        device_id=device_id,
        since=since,
        query=query,
        limit=limit,
        offset=offset,
        hide_autonomous=hide_autonomous,
        context_mode=context_mode,
        include_total=False,
    )
    return rows


async def _timeline_sessions_stream(
    request: Request,
    *,
    session_factory: sessionmaker,
    project: Optional[str],
    provider: Optional[str],
    environment: Optional[str],
    include_test: bool,
    hide_autonomous: bool,
    device_id: Optional[str],
    days_back: int,
    query: Optional[str],
    limit: int,
    offset: int,
    sort: Optional[str],
    mode: Optional[str],
    context_mode: str,
    skip_initial_replay: bool,
):
    previous_signatures: dict[str, str] = {}
    previous_window_signature: (
        tuple[tuple[str, datetime | None, datetime | None, datetime | None, datetime | None, int, datetime | None], ...] | None
    ) = None
    last_heartbeat = monotonic()
    preflight_enabled = _stream_supports_preflight(query=query, sort=sort, mode=mode)

    yield {
        "event": "connected",
        "data": json.dumps({"message": "Timeline session stream connected"}),
    }

    while True:
        if await request.is_disconnected():
            logger.info("Timeline sessions SSE disconnected")
            break

        if skip_initial_replay:
            # The browser already has a fresh timeline snapshot from the initial
            # HTTP query. Do not immediately rebuild the same window on stream
            # connect; wait for the next write/heartbeat cycle instead.
            skip_initial_replay = False
            await _wait_for_timeline_change()
            continue

        if preflight_enabled:
            with session_factory() as db:
                current_window_signature = _load_timeline_stream_window_signature(
                    db=db,
                    project=project,
                    provider=provider,
                    environment=environment,
                    include_test=include_test,
                    hide_autonomous=hide_autonomous,
                    device_id=device_id,
                    days_back=days_back,
                    query=query,
                    limit=limit,
                    offset=offset,
                    context_mode=context_mode,
                )
            if previous_window_signature is not None and current_window_signature == previous_window_signature:
                now = monotonic()
                if now - last_heartbeat >= TIMELINE_STREAM_HEARTBEAT_SECONDS:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
                    }
                    last_heartbeat = now
                await _wait_for_timeline_change()
                continue
            previous_window_signature = current_window_signature

        with session_factory() as db:
            response = await list_timeline_sessions(
                response=Response(),
                project=project,
                provider=provider,
                environment=environment,
                include_test=include_test,
                hide_autonomous=hide_autonomous,
                device_id=device_id,
                days_back=days_back,
                query=query,
                limit=limit,
                offset=offset,
                sort=sort,
                mode=mode,
                context_mode=context_mode,
                db=db,
            )

        current_payloads: dict[str, dict] = {}
        current_signatures: dict[str, str] = {}

        for session in response.sessions:
            payload, signature = _session_payload_signature(session)
            current_payloads[session.thread_id] = payload
            current_signatures[session.thread_id] = signature

        removed_ids = previous_signatures.keys() - current_signatures.keys()
        for thread_id in sorted(removed_ids):
            yield {
                "event": "session_remove",
                "data": json.dumps(
                    {
                        "thread_id": thread_id,
                        "total": response.total,
                        "has_real_sessions": response.has_real_sessions,
                    }
                ),
            }

        for session in response.sessions:
            signature = current_signatures[session.thread_id]
            if previous_signatures.get(session.thread_id) == signature:
                continue
            yield {
                "event": "session_upsert",
                "data": json.dumps(
                    {
                        "session": current_payloads[session.thread_id],
                        "total": response.total,
                        "has_real_sessions": response.has_real_sessions,
                    }
                ),
            }

        previous_signatures = current_signatures

        now = monotonic()
        if now - last_heartbeat >= TIMELINE_STREAM_HEARTBEAT_SECONDS:
            yield {
                "event": "heartbeat",
                "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
            }
            last_heartbeat = now

        await _wait_for_timeline_change()


async def _wait_for_timeline_change() -> None:
    """Wait for a DB write or a timeout before the next SSE poll cycle."""
    from zerg.services.write_serializer import get_write_serializer

    ws = get_write_serializer()
    if ws.is_configured:
        await ws.wait_for_change(timeout=TIMELINE_STREAM_CHANGE_WAIT_SECONDS)
    else:
        await asyncio.sleep(TIMELINE_STREAM_CHANGE_WAIT_SECONDS)


@router.get("/briefing", response_model=BriefingResponse)
async def get_timeline_briefing(
    project: str = Query(..., description="Project name to get briefing for"),
    limit: int = Query(5, ge=1, le=20, description="Max sessions to include"),
    db: Session = Depends(get_db),
):
    return await _briefings_router.get_briefing(project=project, limit=limit, db=db, _auth=None, _single=None)


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
    hide_autonomous: bool = Query(True, description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    sort: Optional[str] = Query(
        None,
        description="Sort order: relevance|recency|balanced. Default: recency if no query, relevance if query present.",
    ),
    mode: Optional[str] = Query("lexical", description="Search mode: lexical|semantic|hybrid. Default: lexical."),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
):
    timing = ServerTimingRecorder()
    effective_mode = mode or "lexical"
    if query is not None or effective_mode != "lexical":
        # COMPATIBILITY: Query-driven and hybrid search return raw SessionResponse[]
        # because thread-aware search ranking/paging hasn't been built yet.
        # The frontend reshapes these into TimelineSessionCards client-side via
        # buildCompatibilityTimelineCards(). This is the only remaining non-thread
        # path on the timeline read surface.
        with timing.span("compat_delegate"):
            raw_response = await _sessions_router.list_sessions(
                project=project,
                provider=provider,
                environment=environment,
                include_test=include_test,
                hide_autonomous=hide_autonomous,
                device_id=device_id,
                days_back=days_back,
                query=query,
                limit=limit,
                offset=offset,
                sort=sort,
                mode=mode,
                context_mode=context_mode,
                db=db,
                _auth=None,
                _single=None,
            )
        if isinstance(raw_response, Response):
            timing.apply(raw_response)
            return raw_response
        compat_response = JSONResponse(content=raw_response.model_dump(mode="json"))
        timing.apply(compat_response)
        return compat_response

    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    with timing.span("list_threads"):
        total, thread_rows = store.list_timeline_thread_page(
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            device_id=device_id,
            since=since,
            query=query,
            limit=limit,
            offset=offset,
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
        )
    with timing.span("build_cards"):
        sessions = _build_timeline_cards_from_thread_rows(db=db, thread_rows=thread_rows)
    with timing.span("has_real"):
        has_real_sessions = _has_real_sessions(db, total=total)
    timing.apply(response)
    return TimelineSessionsListResponse(
        sessions=sessions,
        total=total,
        has_real_sessions=has_real_sessions,
    )


@router.get("/sessions/stream")
async def stream_timeline_sessions(
    request: Request,
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    hide_autonomous: bool = Query(True, description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
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
    db: Session = Depends(get_db),
) -> EventSourceResponse:
    _validate_timeline_stream_contract(query=query, sort=sort, mode=mode)
    session_factory = make_sessionmaker(db.get_bind())
    db.close()

    return EventSourceResponse(
        _timeline_sessions_stream(
            request,
            session_factory=session_factory,
            project=project,
            provider=provider,
            environment=environment,
            include_test=include_test,
            hide_autonomous=hide_autonomous,
            device_id=device_id,
            days_back=days_back,
            query=query,
            limit=limit,
            offset=offset,
            sort=sort,
            mode=mode,
            context_mode=context_mode,
            skip_initial_replay=skip_initial_replay,
        )
    )


@router.get("/sessions/summary", response_model=SessionsSummaryResponse)
async def list_timeline_session_summaries(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions (default: False)"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    query: Optional[str] = Query(None, description="Search query for content"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    hide_autonomous: bool = Query(True, description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)"),
    db: Session = Depends(get_db),
):
    return await _sessions_router.list_session_summaries(
        project=project,
        provider=provider,
        environment=environment,
        include_test=include_test,
        device_id=device_id,
        days_back=days_back,
        query=query,
        limit=limit,
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
        filters = await _sessions_router.get_filters(days_back=days_back, response=response, db=db, _auth=None, _single=None)

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
):
    return await _sessions_router.get_session(session_id=session_id, response=response, db=db, _auth=None, _single=None)


@router.get("/sessions/{session_id}/thread", response_model=SessionThreadResponse)
async def get_timeline_session_thread(
    session_id: UUID,
    response: Response,
    db: Session = Depends(get_db),
):
    return await _sessions_router.get_session_thread(session_id=session_id, response=response, db=db, _auth=None, _single=None)


@router.get("/sessions/{session_id}/events", response_model=EventsListResponse)
async def get_timeline_session_events(
    session_id: UUID,
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    tool_name: Optional[str] = Query(None, description="Exact tool name filter, e.g. Bash"),
    query: Optional[str] = Query(None, description="Content search within session events"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
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
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    db: Session = Depends(get_db),
):
    return await _sessions_router.get_session_projection(
        session_id=session_id,
        branch_mode=branch_mode,
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
):
    return await _sessions_router.get_session_workspace(
        session_id=session_id,
        branch_mode=branch_mode,
        limit=limit,
        response=response,
        db=db,
        _auth=None,
        _single=None,
    )


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
    from zerg.models.agents import SessionPresence
    from zerg.models.agents import SessionRuntimeState

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return ()

    # Resolve thread: all session IDs sharing this thread
    thread_root_id = session.thread_root_session_id or session.id
    thread_sessions = (
        db.query(AgentSession.id, AgentSession.updated_at)
        .filter((AgentSession.id == thread_root_id) | (AgentSession.thread_root_session_id == thread_root_id))
        .all()
    )
    thread_session_ids = [str(row.id) for row in thread_sessions]
    latest_session_updated = max((row.updated_at for row in thread_sessions if row.updated_at), default=None)

    # Latest event ID across thread
    latest_event_id = (db.query(func.max(AgentEvent.id)).filter(AgentEvent.session_id.in_(thread_session_ids)).scalar()) or 0

    # Latest presence updated_at across thread
    latest_presence = db.query(func.max(SessionPresence.updated_at)).filter(SessionPresence.session_id.in_(thread_session_ids)).scalar()

    # Latest runtime version across thread
    latest_runtime_version = (
        db.query(func.max(SessionRuntimeState.runtime_version)).filter(SessionRuntimeState.session_id.in_(thread_session_ids)).scalar()
    ) or 0

    return (
        str(thread_root_id),
        latest_session_updated,
        latest_event_id,
        latest_presence,
        latest_runtime_version,
        len(thread_session_ids),
    )


async def _session_workspace_stream(
    request: Request,
    *,
    session_factory: sessionmaker,
    session_id: UUID,
    skip_initial: bool,
):
    """SSE generator that emits workspace_changed when a session's data mutates."""
    previous_sig: tuple | None = None
    last_heartbeat = monotonic()

    yield {
        "event": "connected",
        "data": json.dumps({"session_id": str(session_id)}),
    }

    while True:
        if await request.is_disconnected():
            break

        if skip_initial:
            skip_initial = False
            await _wait_for_timeline_change()
            continue

        with session_factory() as db:
            current_sig = _load_workspace_signature(db, session_id)

        if not current_sig:
            yield {
                "event": "error",
                "data": json.dumps({"error": "session_not_found"}),
            }
            break

        if previous_sig is not None and current_sig == previous_sig:
            now = monotonic()
            if now - last_heartbeat >= WORKSPACE_STREAM_HEARTBEAT_SECONDS:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
                }
                last_heartbeat = now
            await _wait_for_timeline_change()
            continue

        previous_sig = current_sig

        yield {
            "event": "workspace_changed",
            "data": json.dumps(
                {
                    "session_id": str(session_id),
                    "latest_event_id": current_sig[2],
                    "thread_session_count": current_sig[5],
                }
            ),
        }

        now = monotonic()
        last_heartbeat = now

        await _wait_for_timeline_change()


@router.get("/sessions/{session_id}/workspace/stream")
async def stream_session_workspace(
    request: Request,
    session_id: UUID,
    skip_initial: bool = Query(
        False,
        description="When true, wait for first change before emitting workspace_changed.",
    ),
    db: Session = Depends(get_db),
) -> EventSourceResponse:
    """SSE stream that emits workspace_changed when the session's data mutates.

    The browser subscribes on session detail page load and uses each event to
    invalidate React Query caches — replacing the 5-second polling interval
    with event-driven refresh.
    """
    session_factory = make_sessionmaker(db.get_bind())
    db.close()

    return EventSourceResponse(
        _session_workspace_stream(
            request,
            session_factory=session_factory,
            session_id=session_id,
            skip_initial=skip_initial,
        ),
    )
