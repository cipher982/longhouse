"""Agents API — semantic search and recall endpoints."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SemanticSearchResponse
from zerg.services.session_views import build_session_response

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/sessions/semantic", response_model=SemanticSearchResponse)
async def semantic_search_sessions(
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    days_back: int = Query(14, ge=1, le=365, description="Days to look back"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SemanticSearchResponse:
    """Search sessions by semantic similarity using embeddings."""
    from zerg.models_config import get_embedding_config
    from zerg.services.embedding_cache import EmbeddingCache
    from zerg.services.session_processing.embeddings import generate_embedding

    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )

    config = get_embedding_config()
    if not config:
        return SemanticSearchResponse(sessions=[], total=0)

    query_vec = await generate_embedding(query, config)

    cache = EmbeddingCache()

    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    filter_query = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
    if project:
        filter_query = filter_query.filter(AgentSession.project == project)
    if provider:
        filter_query = filter_query.filter(AgentSession.provider == provider)
    else:
        filter_query = filter_query.filter(or_(AgentSession.provider != "canary", AgentSession.provider.is_(None)))
    if environment:
        filter_query = filter_query.filter(AgentSession.environment == environment)
    # Session-identity-kernel cleanup: ``is_sidechain`` was dropped.
    filter_query = filter_query.filter(AgentSession.user_messages > 0)
    valid_ids = {str(row[0]) for row in filter_query.all()}

    matched_rows: list[tuple[AgentSession, str | None, float]] = []
    store = AgentsStore(db)

    if context_mode == "forensic":
        if not cache._session_loaded:
            cache.load_session_embeddings(db, config.model, config.dims)

        results = cache.search_sessions(query_vec, limit=limit, session_filter=valid_ids)
        session_map = {str(session.id): session for session in store.get_sessions_ordered([sid for sid, _score in results])}
        for sid, score in results:
            session = session_map.get(str(sid))
            if not session:
                continue
            matched_rows.append((session, session.summary or session.summary_title or None, score))
    else:
        if not cache._turn_loaded:
            cache.load_turn_embeddings(db, config.model, config.dims)

        turn_hits = cache.search_turns(
            query_vec,
            limit=min(limit * 8, 200),
            session_filter=valid_ids,
        )
        unique_session_ids: list[str] = []
        seen_sessions: set[str] = set()
        for sid, _chunk_index, score, event_start, _event_end in turn_hits:
            sid_str = str(sid)
            if sid_str in seen_sessions:
                continue
            unique_session_ids.append(sid_str)
            seen_sessions.add(sid_str)

        session_map = {str(session.id): session for session in store.get_sessions_ordered(unique_session_ids)}
        seen_sessions.clear()
        for sid, _chunk_index, score, event_start, _event_end in turn_hits:
            sid_str = str(sid)
            if sid_str in seen_sessions:
                continue
            session = session_map.get(sid_str)
            if not session:
                continue

            matched_event = None
            if event_start is not None and event_start >= 0:
                matched_event = (
                    db.query(AgentEvent)
                    .filter(AgentEvent.session_id == session.id)
                    .filter(durable_transcript_event_predicate())
                    .order_by(AgentEvent.timestamp, AgentEvent.id)
                    .offset(event_start)
                    .limit(1)
                    .first()
                )
            boundary = store.get_active_context_boundary(session.id)
            if boundary is not None and (matched_event is None or not store.is_event_in_active_context(matched_event, boundary)):
                continue

            snippet_source = ""
            if matched_event is not None:
                snippet_source = (matched_event.content_text or matched_event.tool_output_text or "").strip()
            snippet = (
                (snippet_source[:200] + "...")
                if snippet_source and len(snippet_source) > 200
                else (snippet_source or session.summary or session.summary_title or None)
            )
            matched_rows.append((session, snippet, score))
            seen_sessions.add(sid_str)
            if len(matched_rows) >= limit:
                break

    matched_sessions = [session for session, _snippet, _score in matched_rows]
    thread_cache = store.batch_thread_meta(matched_sessions)
    transcript_preview_map = load_active_provisional_preview_map(db, [session.id for session in matched_sessions])
    sessions = [
        build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            match_snippet=snippet,
            match_score=score,
            transcript_preview=transcript_preview_map.get(str(session.id)),
        )
        for session, snippet, score in matched_rows
    ]

    return SemanticSearchResponse(sessions=sessions, total=len(sessions))


@router.get("/recall", response_model=RecallResponse)
async def recall_sessions(
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=20, description="Max matches"),
    context_turns: int = Query(2, ge=0, le=10, description="Context turns before/after match"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RecallResponse:
    """Recall specific knowledge from past sessions."""
    from zerg.models_config import get_embedding_config
    from zerg.services.embedding_cache import EmbeddingCache
    from zerg.services.session_processing.embeddings import generate_embedding

    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )

    config = get_embedding_config()
    if not config:
        return RecallResponse(matches=[], total=0)

    from zerg.services.session_processing.content import redact_secrets

    query_vec = await generate_embedding(query, config)

    cache = EmbeddingCache()
    if not cache._session_loaded:
        cache.load_session_embeddings(db, config.model, config.dims)
    if not cache._turn_loaded:
        cache.load_turn_embeddings(db, config.model, config.dims)

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    filter_query = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
    if project:
        filter_query = filter_query.filter(AgentSession.project == project)
    filter_query = filter_query.filter(or_(AgentSession.provider != "canary", AgentSession.provider.is_(None)))
    valid_ids = {str(row[0]) for row in filter_query.all()}

    results = cache.search_turns(query_vec, limit=max_results, session_filter=valid_ids)

    store = AgentsStore(db)
    ordered_session_ids = []
    seen_session_ids: set[str] = set()
    for session_id, _chunk_index, _score, _event_start, _event_end in results:
        session_key = str(session_id)
        if session_key in seen_session_ids:
            continue
        ordered_session_ids.append(session_id)
        seen_session_ids.add(session_key)

    events_by_session: dict[str, list[AgentEvent]] = {str(session_id): [] for session_id in ordered_session_ids}
    if ordered_session_ids:
        all_events = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id.in_(ordered_session_ids))
            .filter(durable_transcript_event_predicate())
            .order_by(AgentEvent.session_id, AgentEvent.timestamp, AgentEvent.id)
            .all()
        )
        for event in all_events:
            events_by_session.setdefault(str(event.session_id), []).append(event)

    active_start_index_cache: dict[str, int] = {}
    if context_mode == "active_context":
        for session_id in ordered_session_ids:
            session_key = str(session_id)
            session_events = events_by_session.get(session_key, [])
            total_events = len(session_events)
            boundary = store.get_active_context_boundary(session_id)
            if boundary is None:
                active_start_index_cache[session_key] = 0
                continue
            active_start_index = total_events
            for idx, event in enumerate(session_events):
                if store.is_event_in_active_context(event, boundary):
                    active_start_index = idx
                    break
            active_start_index_cache[session_key] = active_start_index

    matches = []
    for session_id, chunk_index, score, event_start, event_end in results:
        all_events = events_by_session.get(str(session_id), [])
        total_events = len(all_events)
        if total_events == 0:
            continue

        active_start_index = active_start_index_cache.get(str(session_id), 0)
        if context_mode == "active_context":
            if active_start_index >= total_events:
                continue
            if event_end is not None and event_end < active_start_index:
                continue

        context = []
        if event_start is not None and event_end is not None:
            window_start = max(active_start_index, event_start - context_turns)
            window_end = min(total_events, event_end + context_turns + 1)
            for i in range(window_start, window_end):
                if i < len(all_events):
                    e = all_events[i]
                    content = redact_secrets(e.content_text or "")
                    if len(content) > 500:
                        content = content[:500] + "..."
                    context.append(
                        {
                            "index": i,
                            "role": e.role,
                            "content": content,
                            "tool_name": e.tool_name,
                            "is_match": event_start <= i <= event_end,
                        }
                    )

        if context_mode == "active_context" and event_start is not None and event_start < active_start_index:
            event_start = active_start_index

        match_event_id = all_events[event_start].id if event_start is not None and event_start < total_events else None

        matches.append(
            RecallMatch(
                session_id=session_id,
                chunk_index=chunk_index,
                score=score,
                event_index_start=event_start,
                event_index_end=event_end,
                total_events=total_events,
                context=context,
                match_event_id=match_event_id,
            )
        )

    return RecallResponse(matches=matches, total=len(matches))
