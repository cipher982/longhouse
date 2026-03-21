"""Browser-owned timeline/session archive API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from time import monotonic
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.routers import agents as agents_router

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/timeline",
    tags=["timeline"],
    dependencies=[Depends(get_current_browser_user), Depends(require_single_tenant)],
)

TIMELINE_STREAM_POLL_SECONDS = 1.0
TIMELINE_STREAM_HEARTBEAT_SECONDS = 30.0


def _session_payload_signature(session: agents_router.SessionResponse) -> tuple[dict, str]:
    payload = session.model_dump(mode="json")
    return payload, json.dumps(payload, sort_keys=True, separators=(",", ":"))


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
    last_heartbeat = monotonic()

    yield {
        "event": "connected",
        "data": json.dumps({"message": "Timeline session stream connected"}),
    }

    while True:
        if await request.is_disconnected():
            logger.info("Timeline sessions SSE disconnected")
            break

        with session_factory() as db:
            response = await agents_router.list_sessions(
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

        current_payloads: dict[str, dict] = {}
        current_signatures: dict[str, str] = {}

        for session in response.sessions:
            payload, signature = _session_payload_signature(session)
            current_payloads[session.id] = payload
            current_signatures[session.id] = signature

        removed_ids = previous_signatures.keys() - current_signatures.keys()
        for session_id in sorted(removed_ids):
            yield {
                "event": "session_remove",
                "data": json.dumps(
                    {
                        "session_id": session_id,
                        "total": response.total,
                        "has_real_sessions": response.has_real_sessions,
                    }
                ),
            }

        for session in response.sessions:
            signature = current_signatures[session.id]
            if previous_signatures.get(session.id) == signature:
                continue
            yield {
                "event": "session_upsert",
                "data": json.dumps(
                    {
                        "session": current_payloads[session.id],
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


@router.get("/sessions", response_model=agents_router.SessionsListResponse)
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
    return await agents_router.list_sessions(
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
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (working, idle, completed)"),
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
