"""Use case for listing agent sessions.

This keeps the canonical `/api/agents/sessions` route thin while preserving
the current listing, search, runtime-overlay, and response-header behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.auth.managed_local_hook_tokens import ManagedLocalHookToken
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.services.session_views import build_session_response
from zerg.services.session_views import normalize_utc_datetime
from zerg.services.unmanaged_bindings import load_binding_overlay


@dataclass(frozen=True)
class SessionListParams:
    project: str | None
    provider: str | None
    environment: str | None
    include_test: bool
    hide_autonomous: bool
    device_id: str | None
    days_back: int
    query: str | None
    limit: int
    offset: int
    sort: str | None
    mode: str | None
    context_mode: str


@dataclass(frozen=True)
class SessionListResult:
    response: SessionsListResponse
    headers: dict[str, str] = field(default_factory=dict)


class SessionListingError(Exception):
    """Expected session-listing failure that maps cleanly to an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def list_agent_sessions(
    *,
    db: Session,
    auth: object,
    params: SessionListParams,
) -> SessionListResult:
    """List sessions for the machine-facing agents API."""

    _validate_managed_hook_scope(auth, params)
    _validate_context_mode(params.context_mode)
    effective_sort = _resolve_effective_sort(params)

    if params.mode == "hybrid":
        return await _list_hybrid_sessions(db=db, params=params)

    return _list_lexical_sessions(db=db, params=params, effective_sort=effective_sort)


def _validate_managed_hook_scope(auth: object, params: SessionListParams) -> None:
    if not isinstance(auth, ManagedLocalHookToken):
        return

    if params.project != auth.project:
        raise SessionListingError(
            403,
            "Managed-local hook token requires a matching project filter",
        )
    if (
        params.provider is not None
        or params.environment is not None
        or params.include_test
        or params.device_id is not None
        or params.query is not None
        or params.offset != 0
        or params.limit > 5
        or params.days_back > 7
        or params.sort not in {None, "recency"}
        or params.mode != "lexical"
        or params.context_mode != "forensic"
        or not params.hide_autonomous
    ):
        raise SessionListingError(
            403,
            "Managed-local hook token only supports bounded recent project lookup",
        )


def _validate_context_mode(context_mode: str) -> None:
    if context_mode not in {"forensic", "active_context"}:
        raise SessionListingError(
            400,
            "context_mode must be one of: forensic, active_context",
        )


def _resolve_effective_sort(params: SessionListParams) -> str:
    if params.sort is None:
        return "relevance" if params.query else "recency"

    if params.sort == "balanced" and not params.query:
        raise SessionListingError(
            400,
            "sort=balanced requires a search query (q param)",
        )

    return params.sort


async def _list_hybrid_sessions(*, db: Session, params: SessionListParams) -> SessionListResult:
    if params.offset > 0:
        raise SessionListingError(
            400,
            "Pagination (offset) is not supported for mode=hybrid",
        )

    from zerg.models_config import get_embedding_config_with_db_fallback
    from zerg.services.search import SessionFilters
    from zerg.services.search import lexical_search
    from zerg.services.search import rrf_fuse

    filters = SessionFilters(
        project=params.project,
        provider=params.provider,
        environment=params.environment,
        include_test=params.include_test,
        device_id=params.device_id,
        days_back=params.days_back,
        exclude_user_states=["archived"],
        hide_autonomous=params.hide_autonomous,
        context_mode=params.context_mode,
    )

    lex_hits = lexical_search(params.query or "", db, filters, params.limit, over_fetch=True)

    config = get_embedding_config_with_db_fallback(db=db)
    sem_hits: list[tuple[AgentSession, float]] = []
    x_search_mode_header = None
    query_vec = None
    cache = None
    if params.context_mode == "active_context":
        x_search_mode_header = "active-context-lexical"
    elif config and params.query:
        from zerg.services.embedding_cache import EmbeddingCache
        from zerg.services.session_processing.embeddings import generate_embedding

        fetch_limit = min(params.limit * 3, 200)
        query_vec = await generate_embedding(params.query, config)
        cache = EmbeddingCache()
        if not cache._session_loaded:
            cache.load_session_embeddings(db, config.model, config.dims)

        valid_ids = _hybrid_semantic_candidate_ids(db, params)
        sem_results = cache.search_sessions(query_vec, limit=fetch_limit, session_filter=valid_ids)
        store = AgentsStore(db)
        session_map = {str(session.id): session for session in store.get_sessions_ordered([sid for sid, _score in sem_results])}
        for sid, score in sem_results:
            session = session_map.get(str(sid))
            if session:
                sem_hits.append((session, score))
    else:
        x_search_mode_header = "lexical-fallback"

    fused = rrf_fuse(lex_hits, sem_hits, params.limit)

    store = AgentsStore(db)
    match_map = _load_match_map(store, [s.id for s in lex_hits], params.query, context_mode=params.context_mode) if lex_hits else {}
    semantic_snippet_map = _load_semantic_snippet_map(
        db=db,
        config=config,
        cache=cache,
        query=params.query,
        query_vec=query_vec,
        fused=fused,
        match_map=match_map,
    )
    sem_score_map = {s.id: score for s, score in sem_hits}
    response_sessions = _build_session_list_response(
        db=db,
        store=store,
        sessions=fused,
        match_map=match_map,
        semantic_snippet_map=semantic_snippet_map,
        sem_score_map=sem_score_map,
    )

    response = SessionsListResponse(
        sessions=response_sessions,
        total=len(fused),
        has_real_sessions=_has_real_sessions(db, default_when_empty=False),
    )
    headers = {"X-Search-Mode": x_search_mode_header} if x_search_mode_header else {}
    return SessionListResult(response=response, headers=headers)


def _hybrid_semantic_candidate_ids(db: Session, params: SessionListParams) -> set[str]:
    since = datetime.now(timezone.utc) - timedelta(days=params.days_back)
    filter_q = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
    if params.project:
        filter_q = filter_q.filter(AgentSession.project == params.project)
    if params.provider:
        filter_q = filter_q.filter(AgentSession.provider == params.provider)
    if params.environment:
        filter_q = filter_q.filter(AgentSession.environment == params.environment)
    if params.hide_autonomous:
        filter_q = filter_q.filter(AgentSession.user_messages > 0).filter(AgentSession.is_sidechain == 0)
    return {str(row[0]) for row in filter_q.all()}


def _load_match_map(
    store: AgentsStore,
    session_ids: list[Any],
    query: str | None,
    *,
    context_mode: str,
) -> dict[Any, dict[str, Any]]:
    if not query:
        return {}
    try:
        return store.get_session_matches(session_ids, query, context_mode=context_mode)
    except Exception:
        return {}


def _load_semantic_snippet_map(
    *,
    db: Session,
    config: object | None,
    cache: object | None,
    query: str | None,
    query_vec: object | None,
    fused: list[AgentSession],
    match_map: dict[Any, dict[str, Any]],
) -> dict[str, str]:
    if not (config and cache is not None and query and query_vec is not None):
        return {}

    no_snippet_ids = {str(s.id) for s in fused if not (match_map.get(s.id) or {}).get("snippet")}
    if not no_snippet_ids:
        return {}

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
        return _load_semantic_event_snippets(db, best_turn)
    except Exception:
        return {}


def _load_semantic_event_snippets(
    db: Session,
    best_turn: dict[str, tuple[int | None, int | None]],
) -> dict[str, str]:
    semantic_snippet_map: dict[str, str] = {}
    for tsid, (estart, eend) in best_turn.items():
        if estart is None:
            continue
        count = max(1, (eend or estart) - estart + 1)
        events = db.query(AgentEvent).filter(AgentEvent.session_id == tsid).order_by(AgentEvent.id).offset(estart).limit(count).all()
        for ev in events:
            content_text = (ev.content_text or "").strip()
            if len(content_text) > 20:
                semantic_snippet_map[tsid] = content_text[:200]
                break
    return semantic_snippet_map


def _list_lexical_sessions(
    *,
    db: Session,
    params: SessionListParams,
    effective_sort: str,
) -> SessionListResult:
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=params.days_back)

    sessions, total = store.list_sessions(
        project=params.project,
        provider=params.provider,
        environment=params.environment,
        include_test=params.include_test,
        device_id=params.device_id,
        since=since,
        query=params.query,
        limit=params.limit,
        offset=params.offset,
        hide_autonomous=params.hide_autonomous,
        context_mode=params.context_mode,
        anchor_on_activity=effective_sort == "recency",
    )

    if params.query or effective_sort != "recency":
        from zerg.services.search import apply_sort

        bm25_order = [str(s.id) for s in sessions]
        sessions = apply_sort(sessions, effective_sort, bm25_order=bm25_order)

    session_ids = [s.id for s in sessions]
    match_map = store.get_session_matches(session_ids, params.query, context_mode=params.context_mode) if params.query else {}
    response_sessions = _build_session_list_response(
        db=db,
        store=store,
        sessions=sessions,
        match_map=match_map,
    )

    if effective_sort == "recency":
        response_sessions.sort(
            key=lambda r: r.timeline_anchor_at or r.last_activity_at or r.started_at,
            reverse=True,
        )

    return SessionListResult(
        response=SessionsListResponse(
            sessions=response_sessions,
            total=total,
            has_real_sessions=_has_real_sessions(db, default_when_empty=total == 0),
        )
    )


def _build_session_list_response(
    *,
    db: Session,
    store: AgentsStore,
    sessions: list[AgentSession],
    match_map: dict[Any, dict[str, Any]],
    semantic_snippet_map: dict[str, str] | None = None,
    sem_score_map: dict[Any, float] | None = None,
) -> list[SessionResponse]:
    session_ids = [s.id for s in sessions]
    activity_map = store.get_last_activity_map(session_ids)
    now = datetime.now(timezone.utc)
    runtime_state_map = load_runtime_state_map(db, session_ids)
    first_user_map = store.get_first_message_map(session_ids, role="user", max_len=80)
    thread_cache = store.batch_thread_meta(sessions)
    binding_overlay_map = load_binding_overlay(db, session_ids, now=now)
    semantic_snippet_map = semantic_snippet_map or {}
    sem_score_map = sem_score_map or {}

    return [
        build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            last_activity_at=normalize_utc_datetime(activity_map.get(session.id) or session.ended_at or session.started_at),
            runtime_overlay=resolve_runtime_overlay(
                session,
                last_activity_at=activity_map.get(session.id) or session.ended_at or session.started_at,
                runtime_state_map=runtime_state_map,
                now=now,
            ),
            first_user_message=first_user_map.get(session.id),
            match_event_id=(match_map.get(session.id) or {}).get("event_id"),
            match_snippet=(match_map.get(session.id) or {}).get("snippet") or semantic_snippet_map.get(str(session.id)),
            match_role=(match_map.get(session.id) or {}).get("role"),
            match_score=sem_score_map.get(session.id),
            binding_overlay=binding_overlay_map.get(session.id),
        )
        for session in sessions
    ]


def _has_real_sessions(db: Session, *, default_when_empty: bool) -> bool:
    if default_when_empty:
        return True

    return (
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
