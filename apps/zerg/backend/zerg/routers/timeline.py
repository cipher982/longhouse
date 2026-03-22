"""Browser-owned timeline/session archive API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from time import monotonic
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.models.agents import AgentSession
from zerg.routers import agents as agents_router
from zerg.services.agents_store import AgentsStore
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/timeline",
    tags=["timeline"],
    dependencies=[Depends(get_current_browser_user), Depends(require_single_tenant)],
)

TIMELINE_STREAM_POLL_SECONDS = 1.0
TIMELINE_STREAM_HEARTBEAT_SECONDS = 30.0


class TimelineSessionCardResponse(UTCBaseModel):
    thread_id: str = Field(..., description="Logical thread/task root UUID")
    timeline_anchor_at: datetime | None = Field(None, description="Anchor used for timeline ordering and grouping")
    head: agents_router.SessionResponse
    detail: agents_router.SessionResponse
    root: agents_router.SessionResponse
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
) -> dict[str, agents_router.SessionResponse]:
    if not session_ids:
        return {}

    store = AgentsStore(db)
    uuid_ids = [UUID(session_id) for session_id in session_ids]
    sessions = db.query(AgentSession).filter(AgentSession.id.in_(uuid_ids)).all()
    if not sessions:
        return {}

    activity_map = store.get_last_activity_map([session.id for session in sessions])
    presence_map = agents_router._load_presence_map(db, [session.id for session in sessions])
    runtime_state_map = agents_router.load_runtime_state_map(db, [session.id for session in sessions])
    first_user_map = store.get_first_message_map([session.id for session in sessions], role="user", max_len=80)
    thread_cache: dict[str, tuple[str, int]] = {}
    now = datetime.now(timezone.utc)

    response_map: dict[str, agents_router.SessionResponse] = {}
    for session in sessions:
        response = agents_router._build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            last_activity_at=agents_router._normalize_utc_datetime(activity_map.get(session.id) or session.ended_at or session.started_at),
            runtime_overlay=agents_router._resolve_runtime_overlay(
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


def _build_timeline_cards_from_detail_rows(
    *,
    db: Session,
    detail_rows: list[agents_router.SessionResponse],
) -> list[TimelineSessionCardResponse]:
    if not detail_rows:
        return []

    response_map = {session.id: session for session in detail_rows}
    supplemental_ids = sorted(
        {detail.thread_root_session_id for detail in detail_rows}
        | {detail.thread_head_session_id for detail in detail_rows} - response_map.keys()
    )
    response_map.update(_build_session_response_map(db=db, session_ids=supplemental_ids))

    cards: list[TimelineSessionCardResponse] = []
    for detail in detail_rows:
        head = response_map.get(detail.thread_head_session_id, detail)
        root = response_map.get(detail.thread_root_session_id, detail)
        cards.append(
            TimelineSessionCardResponse(
                thread_id=detail.thread_root_session_id,
                timeline_anchor_at=head.timeline_anchor_at or head.last_activity_at or head.started_at,
                head=head,
                detail=detail,
                root=root,
                continuation_count=head.thread_continuation_count or detail.thread_continuation_count or 1,
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
                await asyncio.sleep(TIMELINE_STREAM_POLL_SECONDS)
                continue
            previous_window_signature = current_window_signature

        with session_factory() as db:
            response = await list_timeline_sessions(
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

        await asyncio.sleep(TIMELINE_STREAM_POLL_SECONDS)


@router.get("/briefing", response_model=agents_router.BriefingResponse)
async def get_timeline_briefing(
    project: str = Query(..., description="Project name to get briefing for"),
    limit: int = Query(5, ge=1, le=20, description="Max sessions to include"),
    db: Session = Depends(get_db),
):
    return await agents_router.get_briefing(project=project, limit=limit, db=db, _auth=None, _single=None)


@router.get("/sessions/semantic", response_model=agents_router.SemanticSearchResponse)
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
    return await agents_router.semantic_search_sessions(
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


@router.get("/recall", response_model=agents_router.RecallResponse)
async def recall_timeline_sessions(
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=20, description="Max matches"),
    context_turns: int = Query(2, ge=0, le=10, description="Context turns before/after match"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
):
    return await agents_router.recall_sessions(
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
    effective_mode = mode or "lexical"
    if query is not None or effective_mode != "lexical":
        # Hybrid search remains on the legacy raw-session contract for now.
        # Query-driven search remains on the legacy raw-session contract for now.
        # The thread-card contract in this route is only authoritative for the
        # default no-query timeline path.
        raw_response = await agents_router.list_sessions(
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
            return raw_response
        return JSONResponse(content=raw_response.model_dump(mode="json"))

    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
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
    detail_ids = [session_id for _thread_id, session_id, _anchor in thread_rows]
    detail_map = _build_session_response_map(db=db, session_ids=detail_ids)
    detail_rows = [detail_map[session_id] for session_id in detail_ids if session_id in detail_map]

    if query:
        match_map = store.get_session_matches([UUID(session.id) for session in detail_rows], query, context_mode=context_mode)
        detail_rows = [
            detail.model_copy(
                update={
                    "match_event_id": (match_map.get(UUID(detail.id)) or {}).get("event_id"),
                    "match_snippet": (match_map.get(UUID(detail.id)) or {}).get("snippet"),
                    "match_role": (match_map.get(UUID(detail.id)) or {}).get("role"),
                }
            )
            for detail in detail_rows
        ]

    return TimelineSessionsListResponse(
        sessions=_build_timeline_cards_from_detail_rows(db=db, detail_rows=detail_rows),
        total=total,
        has_real_sessions=_has_real_sessions(db, total=total),
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
    db: Session = Depends(get_db),
) -> EventSourceResponse:
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
        )
    )


@router.get("/sessions/summary", response_model=agents_router.SessionsSummaryResponse)
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
    return await agents_router.list_session_summaries(
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


@router.get("/sessions/active", response_model=agents_router.ActiveSessionsResponse)
async def list_timeline_active_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (working, active, idle, completed)"),
    attention: Optional[str] = Query(None, description="Filter by attention (auto)"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    db: Session = Depends(get_db),
):
    return await agents_router.list_active_sessions(
        project=project,
        status_filter=status_filter,
        attention=attention,
        limit=limit,
        days_back=days_back,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}/preview", response_model=agents_router.SessionPreviewResponse)
async def preview_timeline_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
):
    return await agents_router.preview_session(
        session_id=session_id,
        last_n=last_n,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/filters", response_model=agents_router.FiltersResponse)
async def get_timeline_filters(
    days_back: int = Query(90, ge=1, le=365, description="Days to look back for distinct values"),
    db: Session = Depends(get_db),
):
    return await agents_router.get_filters(days_back=days_back, db=db, _auth=None, _single=None)


@router.post("/demo", response_model=agents_router.DemoSeedResponse)
async def seed_timeline_demo_sessions(
    replace: bool = Query(False, description="Delete existing demo sessions before seeding fresh demo data"),
    db: Session = Depends(get_db),
):
    return await agents_router.seed_demo_sessions(replace=replace, db=db, _auth=None, _single=None)


@router.post("/sessions/{session_id}/action", response_model=agents_router.SessionActionResponse)
async def set_timeline_session_action(
    session_id: UUID,
    body: agents_router.SessionActionRequest,
    db: Session = Depends(get_db),
):
    return await agents_router.set_session_action(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.patch("/sessions/{session_id}/loop-mode", response_model=agents_router.SessionLoopModeResponse)
async def set_timeline_session_loop_mode(
    session_id: UUID,
    body: agents_router.SessionLoopModeRequest,
    db: Session = Depends(get_db),
):
    return await agents_router.set_session_loop_mode(
        session_id=session_id,
        body=body,
        db=db,
        _auth=None,
        _single=None,
    )


@router.get("/sessions/{session_id}", response_model=agents_router.SessionResponse)
async def get_timeline_session(
    session_id: UUID,
    db: Session = Depends(get_db),
):
    return await agents_router.get_session(session_id=session_id, db=db, _auth=None, _single=None)


@router.get("/sessions/{session_id}/thread", response_model=agents_router.SessionThreadResponse)
async def get_timeline_session_thread(
    session_id: UUID,
    db: Session = Depends(get_db),
):
    return await agents_router.get_session_thread(session_id=session_id, db=db, _auth=None, _single=None)


@router.get("/sessions/{session_id}/events", response_model=agents_router.EventsListResponse)
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
    return await agents_router.get_session_events(
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


@router.get("/sessions/{session_id}/projection", response_model=agents_router.SessionProjectionResponse)
async def get_timeline_session_projection(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    db: Session = Depends(get_db),
):
    return await agents_router.get_session_projection(
        session_id=session_id,
        branch_mode=branch_mode,
        limit=limit,
        offset=offset,
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
    return await agents_router.export_session(
        session_id=session_id,
        branch_mode=branch_mode,
        db=db,
        _auth=None,
        _single=None,
    )
