"""Hybrid lexical/semantic session-listing branch."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_listing_types import SessionListingError
from zerg.services.session_listing_types import SessionListParams
from zerg.services.session_listing_types import SessionListResult
from zerg.services.session_response_projection import build_session_response_list
from zerg.services.session_response_projection import has_real_sessions
from zerg.services.session_views import SessionsListResponse


async def list_hybrid_sessions(
    *,
    db: Session,
    params: SessionListParams,
    owner_id: int | None = None,
) -> SessionListResult:
    if params.offset > 0:
        raise SessionListingError(
            400,
            "Pagination (offset) is not supported for mode=hybrid",
        )

    from zerg.models_config import get_embedding_config
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

    config = get_embedding_config()
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
    response_sessions = build_session_response_list(
        db=db,
        store=store,
        sessions=fused,
        match_map=match_map,
        semantic_snippet_map=semantic_snippet_map,
        sem_score_map=sem_score_map,
        owner_id=owner_id,
    )

    response = SessionsListResponse(
        sessions=response_sessions,
        total=len(fused),
        has_real_sessions=has_real_sessions(db, default_when_empty=False),
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
        # Session-identity-kernel cleanup: ``is_sidechain`` was dropped.
        filter_q = filter_q.filter(AgentSession.user_messages > 0)
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
        events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == tsid)
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.id)
            .offset(estart)
            .limit(count)
            .all()
        )
        for ev in events:
            content_text = (ev.content_text or "").strip()
            if len(content_text) > 20:
                semantic_snippet_map[tsid] = content_text[:200]
                break
    return semantic_snippet_map
