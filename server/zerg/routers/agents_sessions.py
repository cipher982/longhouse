"""Agents API — session CRUD, listing, and export endpoints."""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_views import ActiveSessionResponse
from zerg.services.session_views import ActiveSessionsResponse
from zerg.services.session_views import EventsListResponse
from zerg.services.session_views import FiltersResponse
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionActionResponse
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionLoopModeResponse
from zerg.services.session_views import SessionPreviewMessage
from zerg.services.session_views import SessionPreviewResponse
from zerg.services.session_views import SessionProjectionItemResponse
from zerg.services.session_views import SessionProjectionResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.services.session_views import SessionsSummaryResponse
from zerg.services.session_views import SessionSummaryResponse
from zerg.services.session_views import SessionThreadResponse
from zerg.services.session_views import _coerce_managed_transport
from zerg.services.session_views import _coerce_session_loop_mode
from zerg.services.session_views import build_event_response
from zerg.services.session_views import build_session_response
from zerg.services.session_views import load_presence_map
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.session_views import resolve_execution_home
from zerg.services.session_views import resolve_runtime_overlay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_USER_STATES = {"active", "parked", "snoozed", "archived"}


@router.get("/sessions", response_model=SessionsListResponse)
async def list_sessions(
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
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionsListResponse:
    """List sessions with optional filters."""
    try:
        if isinstance(_auth, ManagedLocalHookToken):
            if project != _auth.project:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managed-local hook token requires a matching project filter",
                )
            if (
                provider is not None
                or environment is not None
                or include_test
                or device_id is not None
                or query is not None
                or offset != 0
                or limit > 5
                or days_back > 7
                or sort not in {None, "recency"}
                or mode != "lexical"
                or context_mode != "forensic"
                or not hide_autonomous
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managed-local hook token only supports bounded recent project lookup",
                )
        if context_mode not in {"forensic", "active_context"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="context_mode must be one of: forensic, active_context",
            )

        effective_sort = sort
        if effective_sort is None:
            effective_sort = "relevance" if query else "recency"
        elif effective_sort == "balanced" and not query:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sort=balanced requires a search query (q param)",
            )

        # Hybrid mode: RRF fusion
        if mode == "hybrid":
            if offset > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Pagination (offset) is not supported for mode=hybrid",
                )
            from sqlalchemy import or_

            from zerg.models_config import get_embedding_config_with_db_fallback
            from zerg.services.search import SessionFilters
            from zerg.services.search import lexical_search
            from zerg.services.search import rrf_fuse

            _filters = SessionFilters(
                project=project,
                provider=provider,
                environment=environment,
                include_test=include_test,
                device_id=device_id,
                days_back=days_back,
                exclude_user_states=["archived"],
                hide_autonomous=hide_autonomous,
                context_mode=context_mode,
            )

            lex_hits = lexical_search(query or "", db, _filters, limit, over_fetch=True)

            config = get_embedding_config_with_db_fallback(db=db)
            sem_hits: list[tuple[AgentSession, float]] = []
            x_search_mode_header = None
            query_vec = None
            if context_mode == "active_context":
                x_search_mode_header = "active-context-lexical"
            elif config and query:
                from zerg.services.embedding_cache import EmbeddingCache
                from zerg.services.session_processing.embeddings import generate_embedding

                fetch_limit = min(limit * 3, 200)
                query_vec = await generate_embedding(query, config)
                cache = EmbeddingCache()
                if not cache._session_loaded:
                    cache.load_session_embeddings(db, config.model, config.dims)

                since = datetime.now(timezone.utc) - timedelta(days=days_back)
                filter_q = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
                if project:
                    filter_q = filter_q.filter(AgentSession.project == project)
                if provider:
                    filter_q = filter_q.filter(AgentSession.provider == provider)
                if environment:
                    filter_q = filter_q.filter(AgentSession.environment == environment)
                if hide_autonomous:
                    filter_q = filter_q.filter(AgentSession.user_messages > 0).filter(AgentSession.is_sidechain == 0)
                valid_ids = {str(row[0]) for row in filter_q.all()}

                sem_results = cache.search_sessions(query_vec, limit=fetch_limit, session_filter=valid_ids)
                for sid, score in sem_results:
                    session = db.query(AgentSession).filter(AgentSession.id == sid).first()
                    if session:
                        sem_hits.append((session, score))
            else:
                x_search_mode_header = "lexical-fallback"

            fused = rrf_fuse(lex_hits, sem_hits, limit)

            store = AgentsStore(db)
            match_map = {}
            if query and lex_hits:
                try:
                    match_map = store.get_session_matches([s.id for s in lex_hits], query, context_mode=context_mode)
                except Exception:
                    pass

            semantic_snippet_map: dict[int, str] = {}
            if config and query and query_vec is not None:
                no_snippet_ids = {str(s.id) for s in fused if not (match_map.get(s.id) or {}).get("snippet")}
                if no_snippet_ids:
                    try:
                        if not cache._turn_loaded:
                            cache.load_turn_embeddings(db, config.model, config.dims)
                        turn_hits = cache.search_turns(
                            query_vec,
                            limit=len(no_snippet_ids) * 3,
                            session_filter=no_snippet_ids,
                        )
                        best_turn: dict[str, tuple[int | None, int | None]] = {}
                        for tsid, _chunk, _score, estart, eend in turn_hits:
                            if tsid not in best_turn:
                                best_turn[tsid] = (estart, eend)
                        for tsid, (estart, eend) in best_turn.items():
                            if estart is None:
                                continue
                            count = max(1, (eend or estart) - estart + 1)
                            events = (
                                db.query(AgentEvent)
                                .filter(AgentEvent.session_id == int(tsid))
                                .order_by(AgentEvent.id)
                                .offset(estart)
                                .limit(count)
                                .all()
                            )
                            for ev in events:
                                ct = (ev.content_text or "").strip()
                                if len(ct) > 20:
                                    semantic_snippet_map[int(tsid)] = ct[:200]
                                    break
                    except Exception:
                        pass

            session_ids = [s.id for s in fused]
            activity_map = store.get_last_activity_map(session_ids)
            presence_map = load_presence_map(db, session_ids)
            runtime_state_map = load_runtime_state_map(db, session_ids)
            now = datetime.now(timezone.utc)
            first_user_map = store.get_first_message_map([s.id for s in fused], role="user", max_len=80)
            sem_score_map = {s.id: score for s, score in sem_hits}
            thread_cache: dict[str, tuple[str, int]] = {}

            response_sessions = [
                build_session_response(
                    store,
                    s,
                    thread_cache=thread_cache,
                    last_activity_at=normalize_utc_datetime(activity_map.get(s.id) or s.ended_at or s.started_at),
                    runtime_overlay=resolve_runtime_overlay(
                        s,
                        last_activity_at=activity_map.get(s.id) or s.ended_at or s.started_at,
                        presence_map=presence_map,
                        runtime_state_map=runtime_state_map,
                        now=now,
                    ),
                    first_user_message=first_user_map.get(s.id),
                    match_event_id=(match_map.get(s.id) or {}).get("event_id"),
                    match_snippet=(match_map.get(s.id) or {}).get("snippet") or semantic_snippet_map.get(s.id),
                    match_role=(match_map.get(s.id) or {}).get("role"),
                    match_score=sem_score_map.get(s.id),
                )
                for s in fused
            ]

            has_real = (
                db.query(AgentSession.id)
                .filter(
                    or_(
                        AgentSession.device_id != "demo-mac",
                        AgentSession.device_id.is_(None),
                    )
                )
                .limit(1)
                .first()
                is not None
            )

            response = SessionsListResponse(sessions=response_sessions, total=len(fused), has_real_sessions=has_real)
            if x_search_mode_header:
                from fastapi.responses import JSONResponse

                return JSONResponse(content=response.model_dump(mode="json"), headers={"X-Search-Mode": x_search_mode_header})
            return response

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
            hide_autonomous=hide_autonomous,
            context_mode=context_mode,
            anchor_on_activity=effective_sort == "recency",
        )

        if query or effective_sort != "recency":
            from zerg.services.search import apply_sort

            bm25_order = [str(s.id) for s in sessions]
            sessions = apply_sort(sessions, effective_sort, bm25_order=bm25_order)

        session_ids = [s.id for s in sessions]
        match_map = store.get_session_matches(session_ids, query, context_mode=context_mode) if query else {}
        activity_map = store.get_last_activity_map(session_ids)
        presence_map = load_presence_map(db, session_ids)
        runtime_state_map = load_runtime_state_map(db, session_ids)
        first_user_map = store.get_first_message_map(session_ids, role="user", max_len=80)
        thread_cache: dict[str, tuple[str, int]] = {}
        now = datetime.now(timezone.utc)

        response_sessions = [
            build_session_response(
                store,
                s,
                thread_cache=thread_cache,
                last_activity_at=normalize_utc_datetime(activity_map.get(s.id) or s.ended_at or s.started_at),
                runtime_overlay=resolve_runtime_overlay(
                    s,
                    last_activity_at=activity_map.get(s.id) or s.ended_at or s.started_at,
                    presence_map=presence_map,
                    runtime_state_map=runtime_state_map,
                    now=now,
                ),
                first_user_message=first_user_map.get(s.id),
                match_event_id=(match_map.get(s.id) or {}).get("event_id"),
                match_snippet=(match_map.get(s.id) or {}).get("snippet"),
                match_role=(match_map.get(s.id) or {}).get("role"),
            )
            for s in sessions
        ]

        if effective_sort == "recency":
            response_sessions.sort(
                key=lambda r: r.timeline_anchor_at or r.last_activity_at or r.started_at,
                reverse=True,
            )

        from sqlalchemy import or_

        has_real = total == 0 or (
            db.query(AgentSession.id)
            .filter(
                or_(
                    AgentSession.device_id != "demo-mac",
                    AgentSession.device_id.is_(None),
                )
            )
            .limit(1)
            .first()
            is not None
        )

        return SessionsListResponse(
            sessions=response_sessions,
            total=total,
            has_real_sessions=has_real,
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list sessions",
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
    hide_autonomous: bool = Query(True, description="Hide autonomous sessions (Task sub-agents and sessions with no user messages)"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
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
            hide_autonomous=hide_autonomous,
            anchor_on_activity=query is None,
        )

        session_ids = [s.id for s in sessions]
        last_user = store.get_last_message_map(session_ids, role="user", max_len=200)
        last_ai = store.get_last_message_map(session_ids, role="assistant", max_len=200)

        summaries: List[SessionSummaryResponse] = []
        now = datetime.now(timezone.utc)
        for s in sessions:
            end_time = s.ended_at or now
            duration_minutes = int((end_time - s.started_at).total_seconds() / 60) if s.started_at else None
            turn_count = s.user_messages or 0

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

    except Exception:
        logger.exception("Failed to list session summaries")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list session summaries",
        )


@router.get("/sessions/active", response_model=ActiveSessionsResponse)
async def list_active_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status (working, active, idle, completed)"),
    attention: Optional[str] = Query(None, description="Filter by attention (auto)"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    days_back: int = Query(14, ge=1, le=90, description="Days to look back"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ActiveSessionsResponse:
    """Return active/recent session summaries for the live sessions surface."""
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
            exclude_user_states=["archived", "snoozed"],
            anchor_on_activity=True,
        )

        session_ids = [s.id for s in sessions]
        last_activity = store.get_last_activity_map(session_ids)
        last_user = store.get_last_message_map(session_ids, role="user", max_len=300)
        last_ai = store.get_last_message_map(session_ids, role="assistant", max_len=300)
        presence_map = load_presence_map(db, session_ids)
        runtime_state_map = load_runtime_state_map(db, session_ids)

        now = datetime.now(timezone.utc)
        items: List[ActiveSessionResponse] = []
        for s in sessions:
            last_activity_at = normalize_utc_datetime(last_activity.get(s.id) or s.ended_at or s.started_at) or now
            runtime_overlay = resolve_runtime_overlay(
                s,
                last_activity_at=last_activity.get(s.id) or s.ended_at or s.started_at,
                presence_map=presence_map,
                runtime_state_map=runtime_state_map,
                now=now,
            )

            attention_level = "auto"

            if status_filter and runtime_overlay.status != status_filter:
                continue
            if attention and attention_level != attention:
                continue

            _started = s.started_at.replace(tzinfo=timezone.utc) if s.started_at and s.started_at.tzinfo is None else s.started_at
            _ended = s.ended_at.replace(tzinfo=timezone.utc) if s.ended_at and s.ended_at.tzinfo is None else s.ended_at
            end_time = _ended or now
            duration_minutes = int((end_time - _started).total_seconds() / 60) if _started else 0
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
                    timeline_anchor_at=runtime_overlay.timeline_anchor_at,
                    runtime_phase=runtime_overlay.runtime_phase,
                    phase_started_at=runtime_overlay.phase_started_at,
                    last_progress_at=runtime_overlay.last_progress_at,
                    runtime_source=runtime_overlay.runtime_source,
                    terminal_state=runtime_overlay.terminal_state,
                    runtime_version=runtime_overlay.runtime_version,
                    status=runtime_overlay.status,
                    attention=attention_level,
                    duration_minutes=duration_minutes,
                    last_user_message=last_user.get(s.id),
                    last_assistant_message=last_ai.get(s.id),
                    message_count=message_count,
                    tool_calls=s.tool_calls or 0,
                    presence_state=runtime_overlay.presence_state,
                    presence_tool=runtime_overlay.presence_tool,
                    presence_updated_at=runtime_overlay.presence_updated_at,
                    last_live_at=runtime_overlay.last_live_at,
                    display_phase=runtime_overlay.display_phase,
                    active_tool=runtime_overlay.active_tool,
                    confidence=runtime_overlay.confidence,
                    user_state=s.user_state or "active",
                    execution_home=resolve_execution_home(s),
                    managed_transport=_coerce_managed_transport(getattr(s, "managed_transport", None)),
                    source_runner_id=getattr(s, "source_runner_id", None),
                    source_runner_name=getattr(s, "source_runner_name", None),
                    loop_mode=_coerce_session_loop_mode(getattr(s, "loop_mode", None)),
                )
            )

        return ActiveSessionsResponse(
            sessions=items,
            total=len(items),
            last_refresh=now,
        )

    except Exception:
        logger.exception("Failed to list active sessions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list active sessions",
        )


@router.get("/sessions/{session_id}/preview", response_model=SessionPreviewResponse)
async def preview_session(
    session_id: UUID,
    last_n: int = Query(6, ge=2, le=20, description="Number of messages to return"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
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
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> FiltersResponse:
    """Get distinct filter values for UI dropdowns."""
    try:
        store = AgentsStore(db)
        filters = store.get_distinct_filters(days_back=days_back)
        return FiltersResponse(
            projects=filters["projects"],
            providers=filters["providers"],
            machines=filters["machines"],
        )
    except Exception:
        logger.exception("Failed to get filters")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get filters",
        )


@router.post("/sessions/{session_id}/action", response_model=SessionActionResponse)
async def set_session_action(
    session_id: UUID,
    body: SessionActionRequest,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionActionResponse:
    """Set user-driven bucket state for a session (park/snooze/archive/resume)."""
    action_to_state = {"park": "parked", "snooze": "snoozed", "archive": "archived", "resume": "active"}
    if body.action not in action_to_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action '{body.action}'. Must be one of: {', '.join(sorted(action_to_state))}",
        )

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    new_state = action_to_state[body.action]
    session.user_state = new_state
    session.user_state_at = datetime.now(timezone.utc)
    db.commit()

    return SessionActionResponse(session_id=str(session_id), user_state=new_state)


@router.patch("/sessions/{session_id}/loop-mode", response_model=SessionLoopModeResponse)
async def set_session_loop_mode(
    session_id: UUID,
    body: SessionLoopModeRequest,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionLoopModeResponse:
    """Set the explicit loop mode for a coding session."""
    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    session.loop_mode = body.loop_mode.value
    db.commit()

    return SessionLoopModeResponse(session_id=str(session_id), loop_mode=body.loop_mode)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
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

    activity_map = store.get_last_activity_map([session.id])
    presence_map = load_presence_map(db, [session.id])
    runtime_state_map = load_runtime_state_map(db, [session.id])
    first_user_map = store.get_first_message_map([session.id], role="user", max_len=80)
    now = datetime.now(timezone.utc)
    return build_session_response(
        store,
        session,
        last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
        runtime_overlay=resolve_runtime_overlay(
            session,
            last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
            presence_map=presence_map,
            runtime_state_map=runtime_state_map,
            now=now,
        ),
        first_user_message=first_user_map.get(session.id),
    )


@router.get("/sessions/{session_id}/thread", response_model=SessionThreadResponse)
async def get_session_thread(
    session_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionThreadResponse:
    """Get all concrete continuations in a logical thread."""
    store = AgentsStore(db)
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    thread_sessions = store.list_thread_sessions(session)
    head = store.get_thread_head(session)
    activity_map = store.get_last_activity_map([item.id for item in thread_sessions])
    presence_map = load_presence_map(db, [item.id for item in thread_sessions])
    runtime_state_map = load_runtime_state_map(db, [item.id for item in thread_sessions])
    first_user_map = store.get_first_message_map([item.id for item in thread_sessions], role="user", max_len=80)
    thread_cache: dict[str, tuple[str, int]] = {}
    now = datetime.now(timezone.utc)

    return SessionThreadResponse(
        root_session_id=str(session.thread_root_session_id or session.id),
        head_session_id=str(head.id if head else session.id),
        sessions=[
            build_session_response(
                store,
                item,
                thread_cache=thread_cache,
                last_activity_at=activity_map.get(item.id) or item.ended_at or item.started_at,
                runtime_overlay=resolve_runtime_overlay(
                    item,
                    last_activity_at=activity_map.get(item.id) or item.ended_at or item.started_at,
                    presence_map=presence_map,
                    runtime_state_map=runtime_state_map,
                    now=now,
                ),
                first_user_message=first_user_map.get(item.id),
            )
            for item in thread_sessions
        ],
    )


@router.get("/sessions/{session_id}/events", response_model=EventsListResponse)
async def get_session_events(
    session_id: UUID,
    roles: Optional[str] = Query(None, description="Comma-separated roles to filter"),
    tool_name: Optional[str] = Query(None, description="Exact tool name filter, e.g. Bash"),
    query: Optional[str] = Query(None, description="Content search within session events"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> EventsListResponse:
    """Get events for a session."""
    store = AgentsStore(db)

    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    role_list = [r.strip() for r in roles.split(",")] if roles else None
    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )
    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )

    events = store.get_session_events(
        session_id,
        roles=role_list,
        tool_name=tool_name,
        query=query,
        context_mode=context_mode,
        branch_mode=branch_mode,
        limit=limit,
        offset=offset,
    )
    boundary = store.get_active_context_boundary(session_id, branch_mode=branch_mode)
    head_branch_id = store.get_head_branch_id(session_id)

    total = store.count_session_events(
        session_id,
        roles=role_list,
        tool_name=tool_name,
        query=query,
        context_mode=context_mode,
        branch_mode=branch_mode,
    )
    abandoned_events = 0
    if branch_mode == "head":
        forensic_total = store.count_session_events(
            session_id,
            roles=role_list,
            tool_name=tool_name,
            query=query,
            context_mode=context_mode,
            branch_mode="all",
        )
        abandoned_events = max(0, forensic_total - total)

    return EventsListResponse(
        events=[
            build_event_response(
                store,
                e,
                boundary=boundary,
                head_branch_id=head_branch_id,
            )
            for e in events
        ],
        total=total,
        branch_mode=branch_mode,
        abandoned_events=abandoned_events,
    )


@router.get("/sessions/{session_id}/projection", response_model=SessionProjectionResponse)
async def get_session_projection(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode: head|all"),
    limit: int = Query(100, ge=1, le=1000, description="Max projected items"),
    offset: int = Query(0, ge=0, description="Offset within the stitched projection"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SessionProjectionResponse:
    """Get the stitched lineage-path projection for a focused session."""
    store = AgentsStore(db)

    session = store.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )

    projection = store.get_session_projection_page(
        session,
        branch_mode=branch_mode,
        limit=limit,
        offset=offset,
    )
    head = store.get_thread_head(session)
    active_context_boundary_cache: dict[UUID, int | None] = {}
    head_branch_id_cache: dict[UUID, int | None] = {}

    def get_boundary(current_session_id: UUID) -> int | None:
        if current_session_id not in active_context_boundary_cache:
            active_context_boundary_cache[current_session_id] = store.get_active_context_boundary(
                current_session_id,
                branch_mode=branch_mode,
            )
        return active_context_boundary_cache[current_session_id]

    def get_head_branch_id(current_session_id: UUID) -> int | None:
        if current_session_id not in head_branch_id_cache:
            head_branch_id_cache[current_session_id] = store.get_head_branch_id(current_session_id)
        return head_branch_id_cache[current_session_id]

    items: list[SessionProjectionItemResponse] = []
    for item in projection.items:
        if item.kind == "event" and item.event is not None:
            items.append(
                SessionProjectionItemResponse(
                    kind="event",
                    session_id=str(item.session.id),
                    timestamp=item.event.timestamp,
                    event=build_event_response(
                        store,
                        item.event,
                        boundary=get_boundary(item.session.id),
                        head_branch_id=get_head_branch_id(item.session.id),
                    ),
                )
            )
            continue

        items.append(
            SessionProjectionItemResponse(
                kind="seam",
                session_id=str(item.session.id),
                timestamp=item.session.started_at,
                continued_from_session_id=(str(item.session.continued_from_session_id) if item.session.continued_from_session_id else None),
                continuation_kind=item.session.continuation_kind,
                origin_label=item.session.origin_label,
                parent_origin_label=(item.parent_session.origin_label if item.parent_session else None),
                parent_continuation_kind=(item.parent_session.continuation_kind if item.parent_session else None),
                branched_from_event_id=item.session.branched_from_event_id,
            )
        )

    return SessionProjectionResponse(
        root_session_id=str(session.thread_root_session_id or session.id),
        focus_session_id=str(session.id),
        head_session_id=str(head.id if head else session.id),
        path_session_ids=[str(path_session.id) for path_session in projection.path_sessions],
        items=items,
        total=projection.total,
        branch_mode=projection.branch_mode,
        abandoned_events=projection.abandoned_events,
    )


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: UUID,
    branch_mode: str = Query("head", description="Branch projection mode for export: head|all"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> Response:
    """Export session as JSONL for Claude Code --resume."""
    store = AgentsStore(db)
    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )

    result = store.export_session_jsonl(session_id, branch_mode=branch_mode)

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    jsonl_bytes, session = result

    provider_session_id = session.provider_session_id or str(session.id)

    headers = {
        "Content-Disposition": f"attachment; filename={session_id}.jsonl",
        "X-Session-CWD": session.cwd or "",
        "X-Provider-Session-ID": provider_session_id,
        "X-Session-Provider": session.provider,
        "X-Session-Project": session.project or "",
        "X-Session-Branch-Mode": branch_mode,
    }

    return Response(
        content=jsonl_bytes,
        media_type="application/x-ndjson",
        headers=headers,
    )
