"""Agents API — semantic search and recall endpoints."""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

import zerg.database as database_module
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.database import catalog_db_dependency
from zerg.database import get_db
from zerg.database import get_session_factory
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.agents import AgentsStore
from zerg.services.internal_sessions import internal_canary_session_clause
from zerg.services.internal_sessions import is_internal_canary_provider_filter
from zerg.services.internal_sessions import provider_proof_session_clause
from zerg.services.live_catalog_timeline import read_live_catalog_session
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.retrieval_index import child_chunk_count
from zerg.services.retrieval_index import connect_retrieval_db
from zerg.services.retrieval_index import connect_retrieval_db_readonly
from zerg.services.retrieval_index import get_chunks_by_ids
from zerg.services.retrieval_index import initialize_retrieval_db
from zerg.services.retrieval_index import resolve_retrieval_db_path
from zerg.services.retrieval_index import retrieval_schema_ready
from zerg.services.retrieval_index import search_lexical_chunks
from zerg.services.retrieval_index_jobs import enqueue_recall_index_job
from zerg.services.retrieval_index_jobs import get_latest_recall_index_job
from zerg.services.retrieval_index_jobs import get_recall_index_job
from zerg.services.retrieval_index_jobs import recall_index_jobs_table_ready
from zerg.services.retrieval_index_jobs import request_recall_index_cancel
from zerg.services.retrieval_index_jobs import wake_recall_index_worker
from zerg.services.searchd_supervisor import get_searchd_client
from zerg.services.session_pause_requests import load_active_pause_request_map
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_processing.embeddings import CleanTranscriptEvent
from zerg.services.session_processing.embeddings import iter_clean_transcript_events
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SemanticSearchResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import build_session_response
from zerg.utils.server_timing import ServerTimingRecorder

router = APIRouter(prefix="/agents", tags=["agents"])
logger = logging.getLogger(__name__)
RECALL_ROUTE_TIMEOUT_SECONDS = 5.0

_catalog_db_dependency = catalog_db_dependency()


def _legacy_search_db():
    if database_module.live_catalog_enabled():
        yield None
        return
    with database_module.get_session_factory()() as db:
        yield db


search_db_dependency = get_db if _catalog_db_dependency is get_db else _legacy_search_db


def _catalog_owner_id(auth: object) -> int:
    owner_id = getattr(auth, "owner_id", None)
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "owner_required",
                "message": "Storage-v2 search requires an owner-bound device token.",
            },
        )
    return int(owner_id)


async def search_storage_v2_rows(
    *,
    owner_id: int,
    query: str,
    project: str | None,
    provider: str | None,
    environment: str | None,
    days_back: int,
    limit: int,
    timeout_seconds: float | None = None,
) -> list[dict[str, object]]:
    """Search the disposable v2 index without opening the retired archive DB."""

    search = get_searchd_client()
    if search is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "search_unavailable", "message": "The derived search index is unavailable."},
        )
    now = datetime.now(timezone.utc)
    try:
        params = {
            "owner_id": str(owner_id),
            "query": query,
            "project": project,
            "provider": provider,
            "environment": environment,
            "window_start_us": int((now - timedelta(days=days_back)).timestamp() * 1_000_000),
            "window_end_us": None,
            "limit": min(200, max(1, limit)),
        }
        if timeout_seconds is None:
            result = await search.call("search.query.v2", params)
        else:
            result = await search.call("search.query.v2", params, timeout_seconds=timeout_seconds)
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        reason = exc.code if isinstance(exc, CatalogRemoteError) else str(exc)
        logger.warning(
            "Storage-v2 search query unavailable owner_id=%s query_length=%d reason=%s",
            owner_id,
            len(query),
            reason,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "search_unavailable",
                "message": "The derived search index is unavailable.",
                "reason": reason,
            },
        ) from exc
    return [row for row in (result.get("results") or []) if isinstance(row, dict)]


async def search_storage_v2_context(
    *,
    owner_id: int,
    session_id: str,
    generation_id: str,
    search_event_id: int,
    context_turns: int,
    timeout_seconds: float,
) -> dict[str, object]:
    """Read bounded neighbor evidence from the same generation as a search hit."""

    search = get_searchd_client()
    if search is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "search_unavailable", "message": "The derived search index is unavailable."},
        )
    try:
        result = await search.call(
            "search.context.v2",
            {
                "owner_id": str(owner_id),
                "session_id": session_id,
                "generation_id": generation_id,
                "search_event_id": search_event_id,
                "context_turns": context_turns,
            },
            timeout_seconds=timeout_seconds,
        )
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        reason = exc.code if isinstance(exc, CatalogRemoteError) else str(exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "search_evidence_unavailable",
                "message": "Recall evidence is unavailable.",
                "reason": reason,
            },
        ) from exc
    return result


async def search_storage_v2_sessions(
    *,
    owner_id: int,
    query: str,
    project: str | None,
    provider: str | None,
    environment: str | None,
    days_back: int,
    limit: int,
    include_test: bool,
    hide_autonomous: bool = False,
    include_automation: bool = False,
    device_id: str | None = None,
) -> list[SessionResponse]:
    rows = await search_storage_v2_rows(
        owner_id=owner_id,
        query=query,
        project=project,
        provider=provider,
        environment=environment,
        days_back=days_back,
        limit=200,
    )
    best_rows: dict[str, dict[str, object]] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "")
        if session_id and session_id not in best_rows:
            best_rows[session_id] = row
    projected = await asyncio.gather(
        *(
            asyncio.to_thread(
                read_live_catalog_session,
                UUID(session_id),
                owner_id=owner_id,
            )
            for session_id in best_rows
        )
    )
    sessions = []
    for (session, _provider_alias, _commit_seq), row in zip(projected, best_rows.values(), strict=True):
        if session is None:
            continue
        if not include_test and session.environment in {"test", "e2e"}:
            continue
        if not include_automation and session.environment == "automation":
            continue
        if hide_autonomous and session.user_messages <= 0:
            continue
        if device_id is not None and session.device_id != device_id:
            continue
        snippet = str(row.get("content_snippet") or row.get("tool_output_snippet") or "") or None
        rank = abs(float(row.get("rank") or 0.0))
        sessions.append(session.model_copy(update={"match_snippet": snippet, "match_score": 1.0 / (1.0 + rank)}))
        if len(sessions) >= limit:
            break
    return sessions


async def _run_retrieval_index_recall(
    database_url: str,
    *,
    query: str,
    project: str | None,
    provider: str | None,
    since_days: int,
    max_results: int,
    context_turns: int,
    context_mode: str,
    explicit: bool,
    include_automation: bool = False,
) -> RecallResponse | None:
    """Run retrieval.db recall without sharing the live server SQLite process."""

    if os.getenv("TESTING") == "1":
        return await asyncio.to_thread(
            _try_retrieval_index_recall,
            database_url,
            query=query,
            project=project,
            provider=provider,
            since_days=since_days,
            max_results=max_results,
            context_turns=context_turns,
            context_mode=context_mode,
            explicit=explicit,
            include_automation=include_automation,
        )

    if context_mode != "forensic":
        return None

    payload = {
        "database_url": database_url,
        "query": query,
        "project": project,
        "provider": provider,
        "since_days": since_days,
        "max_results": max_results,
        "context_turns": context_turns,
        "hide_internal_canary": not is_internal_canary_provider_filter(provider),
        "include_automation": include_automation,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zerg.services.retrieval_recall_subprocess",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(json.dumps(payload).encode("utf-8")),
        timeout=6.0,
    )
    if proc.returncode != 0:
        logger.warning(
            "Retrieval recall subprocess failed returncode=%s stderr=%s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace")[-1000:],
        )
        if explicit:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Retrieval recall worker failed.",
            )
        return None
    data = json.loads(stdout.decode("utf-8") or "null")
    return RecallResponse(**data) if data is not None else None


async def get_recall_database_url() -> str | None:
    if database_module.live_catalog_enabled():
        return None
    return get_settings().database_url


async def get_recall_session_factory() -> sessionmaker | None:
    if database_module.live_catalog_enabled():
        return None
    return get_session_factory()


def _embedding_unavailable_response(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Embeddings unavailable: {detail}",
    )


def _embedding_corpus_unavailable_response(kind: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            f"Embeddings unavailable: no {kind} embeddings are loaded for a nonempty "
            "session corpus. Run POST /api/agents/backfill-embeddings or fix the "
            "embedding worker before using semantic search."
        ),
    )


def _try_retrieval_index_recall(
    database_url: str,
    *,
    query: str,
    project: str | None,
    provider: str | None,
    since_days: int,
    max_results: int,
    context_turns: int,
    context_mode: str,
    explicit: bool,
    include_automation: bool = False,
) -> RecallResponse | None:
    """Serve recall from retrieval.db when a ready lexical index exists."""

    phase_started = time.perf_counter()
    phases: dict[str, float] = {}

    def mark_phase(name: str) -> None:
        nonlocal phase_started
        now = time.perf_counter()
        phases[name] = (now - phase_started) * 1000
        phase_started = now

    if context_mode != "forensic":
        if explicit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mode=lexical supports context_mode=forensic only.",
            )
        return None

    retrieval_path = resolve_retrieval_db_path(database_url)
    mark_phase("resolve")
    if retrieval_path is None or not retrieval_path.exists():
        return None

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    retrieval_db = connect_retrieval_db_readonly(retrieval_path)
    mark_phase("connect")
    try:
        if not retrieval_schema_ready(retrieval_db):
            mark_phase("schema")
            return None
        mark_phase("schema")
        if child_chunk_count(retrieval_db) <= 0:
            mark_phase("count")
            return None
        mark_phase("count")

        hits = search_lexical_chunks(
            retrieval_db,
            query,
            project=project,
            provider=provider,
            since=since.isoformat(),
            hide_internal_canary=not is_internal_canary_provider_filter(provider),
            limit=max_results,
        )
        mark_phase("search")
        if hits and not include_automation:
            visible_ids = _visible_recall_session_ids(
                database_url,
                [hit.session_id for hit in hits],
                include_automation=include_automation,
            )
            hits = [hit for hit in hits if hit.session_id in visible_ids]
            mark_phase("visibility")
        parent_ids = [hit.parent_chunk_id for hit in hits if hit.parent_chunk_id is not None]
        parents = get_chunks_by_ids(retrieval_db, parent_ids)
        mark_phase("parents")
    finally:
        retrieval_db.close()
    mark_phase("close")

    matches = []
    for hit in hits:
        parent = parents.get(hit.parent_chunk_id or -1)
        context_text = parent.content if parent is not None else hit.content
        context_start = parent.event_index_start if parent is not None else hit.event_index_start
        context_end = parent.event_index_end if parent is not None else hit.event_index_end
        context = _indexed_context_items(parent, hit, context_turns=context_turns)
        matches.append(
            RecallMatch(
                session_id=hit.session_id,
                chunk_index=hit.chunk_index,
                score=hit.score,
                chunk_id=hit.chunk_id,
                chunk_uid=hit.chunk_uid,
                parent_chunk_id=hit.parent_chunk_id,
                context_chunk_id=parent.chunk_id if parent is not None else hit.chunk_id,
                chunk_kind=hit.chunk_kind,
                context_text=context_text,
                intent=hit.intent_text,
                evidence=hit.evidence_text,
                structured_hits=_structured_hits(hit.structured_text),
                diagnostics={"mode": "lexical", "source": "retrieval_db"},
                event_index_start=hit.event_index_start,
                event_index_end=hit.event_index_end,
                total_events=max(0, context_end - context_start + 1),
                context=context,
                match_event_id=hit.first_event_id,
            )
        )

    response = RecallResponse(matches=matches, total=len(matches))
    mark_phase("format")
    total_ms = sum(phases.values())
    if total_ms > 1000:
        logger.warning(
            "Slow retrieval recall phases total_ms=%.1f hits=%d phases=%s",
            total_ms,
            len(hits),
            " ".join(f"{name}={value:.1f}" for name, value in phases.items()),
        )
    return response


def _visible_recall_session_ids(
    database_url: str,
    session_ids: list[str],
    *,
    include_automation: bool,
) -> set[str]:
    if include_automation or not session_ids:
        return set(session_ids)
    from zerg.database import make_engine
    from zerg.database import make_sessionmaker

    engine = make_engine(database_url)
    SessionLocal = make_sessionmaker(engine)
    try:
        with SessionLocal() as db:
            rows = (
                db.query(AgentSession.id)
                .filter(AgentSession.id.in_(session_ids))
                .filter(
                    or_(
                        AgentSession.hidden_from_default_timeline.is_(None),
                        AgentSession.hidden_from_default_timeline == 0,
                    )
                )
                .all()
            )
            return {str(row[0]) for row in rows}
    except OperationalError:
        return set(session_ids)
    finally:
        engine.dispose()


def _bounded_context_text(value: str | None) -> str:
    if not value:
        return ""
    return value[:500] + ("..." if len(value) > 500 else "")


def _indexed_context_items(parent, hit, *, context_turns: int) -> list[dict[str, object]]:
    if parent is None or context_turns <= 0:
        return [_context_item_from_hit(hit)]

    lower = hit.event_index_start - context_turns
    upper = hit.event_index_end + context_turns
    items: list[dict[str, object]] = []
    for offset, line in enumerate(parent.content.splitlines()):
        event_index = parent.event_index_start + offset
        if event_index < lower or event_index > upper:
            continue
        parsed = _parse_context_line(line)
        if parsed is None:
            continue
        role, tool_name, content = parsed
        items.append(
            {
                "index": event_index,
                "role": role,
                "content": _bounded_context_text(content),
                "tool_name": tool_name,
                "is_match": hit.event_index_start <= event_index <= hit.event_index_end,
            }
        )
    return items or [_context_item_from_hit(hit)]


def _context_item_from_hit(hit) -> dict[str, object]:
    return {
        "index": hit.event_index_start,
        "role": _role_for_chunk_kind(hit.chunk_kind),
        "content": _bounded_context_text(hit.content),
        "tool_name": None,
        "is_match": True,
    }


def _parse_context_line(line: str) -> tuple[str, str | None, str] | None:
    label, sep, content = line.partition(": ")
    if not sep:
        return None
    if ":" in label:
        role, tool_name = label.split(":", 1)
    else:
        role, tool_name = label, None
    if role not in {"user", "assistant", "tool", "system"}:
        return None
    return role, tool_name, content.replace("\\n", "\n")


def _role_for_chunk_kind(chunk_kind: str) -> str:
    if chunk_kind == "intent":
        return "user"
    if chunk_kind == "tool_result":
        return "tool"
    return "assistant"


def _structured_hits(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split() if ":" in part][:20]


@router.get("/sessions/semantic", response_model=SemanticSearchResponse)
async def semantic_search_sessions(
    response: Response = None,
    query: str = Query(..., description="Search query"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    environment: Optional[str] = Query(None, description="Filter by environment (production, development, test, e2e)"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
    days_back: int = Query(14, ge=1, le=365, description="Days to look back"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    db: Session | None = Depends(search_db_dependency),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> SemanticSearchResponse:
    """Search sessions by semantic similarity using embeddings."""
    from zerg.models_config import embedding_unavailable_detail
    from zerg.models_config import get_embedding_config
    from zerg.services.embedding_cache import EmbeddingCache
    from zerg.services.session_processing.embeddings import generate_embedding

    timing = ServerTimingRecorder(surface="search")
    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )

    if database_module.live_catalog_enabled():
        if context_mode != "forensic":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "search_mode_unsupported",
                    "message": "Storage-v2 search does not yet project active-context boundaries.",
                },
            )
        with timing.span("search_query"):
            sessions = await search_storage_v2_sessions(
                owner_id=_catalog_owner_id(_auth),
                query=query,
                project=project,
                provider=provider,
                environment=environment,
                days_back=days_back,
                limit=limit,
                include_test=include_test,
                hide_autonomous=True,
            )
        result = SemanticSearchResponse(sessions=sessions, total=len(sessions), has_real_sessions=bool(sessions))
        timing.apply(response)
        return result

    assert db is not None

    config = get_embedding_config()
    if not config:
        raise _embedding_unavailable_response(embedding_unavailable_detail())

    with timing.span("query_embedding"):
        query_vec = await generate_embedding(query, config)

    cache = EmbeddingCache()

    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    filter_query = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
    if project:
        filter_query = filter_query.filter(AgentSession.project == project)
    if provider:
        filter_query = filter_query.filter(AgentSession.provider == provider)
    if not is_internal_canary_provider_filter(provider):
        filter_query = filter_query.filter(~internal_canary_session_clause(AgentSession))
    if environment:
        filter_query = filter_query.filter(AgentSession.environment == environment)
    elif not include_test:
        filter_query = filter_query.filter(AgentSession.environment.notin_(["test", "e2e"]))
    if not include_test:
        filter_query = filter_query.filter(~provider_proof_session_clause(AgentSession))
    # Session-identity-kernel cleanup: ``is_sidechain`` was dropped.
    filter_query = filter_query.filter(AgentSession.user_messages > 0)
    valid_ids = {str(row[0]) for row in filter_query.all()}

    matched_rows: list[tuple[AgentSession, str | None, float]] = []
    store = AgentsStore(db)

    if context_mode == "forensic":
        if not cache._session_loaded:
            cache.load_session_embeddings(db, config.model, config.dims)
        if valid_ids and cache.session_embedding_count == 0:
            raise _embedding_corpus_unavailable_response("session")

        with timing.span("search_query"):
            results = cache.search_sessions(query_vec, limit=limit, session_filter=valid_ids)
        ordered_session_ids = [sid for sid, _score in results]
        session_map = {str(session.id): session for session in store.get_sessions_ordered(ordered_session_ids)}
        for sid, score in results:
            session = session_map.get(str(sid))
            if not session:
                continue
            matched_rows.append((session, session.summary or session.summary_title or None, score))
    else:
        if not cache._turn_loaded:
            cache.load_turn_embeddings(db, config.model, config.dims)
        if valid_ids and cache.turn_embedding_count == 0:
            raise _embedding_corpus_unavailable_response("turn")

        with timing.span("search_query"):
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
            if boundary is not None:
                matched_event_active = matched_event is not None and store.is_event_in_active_context(
                    matched_event,
                    boundary,
                )
            else:
                matched_event_active = True
            if not matched_event_active:
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
    matched_session_ids = [session.id for session in matched_sessions]
    transcript_preview_map = load_active_provisional_preview_map(db, matched_session_ids)
    pause_request_map = load_active_pause_request_map(db, matched_session_ids)
    sessions = [
        build_session_response(
            store,
            session,
            thread_cache=thread_cache,
            match_snippet=snippet,
            match_score=score,
            transcript_preview=transcript_preview_map.get(str(session.id)),
            pause_request=serialize_pause_request_projection(pause_request_map.get(session.id)),
        )
        for session, snippet, score in matched_rows
    ]

    result = SemanticSearchResponse(sessions=sessions, total=len(sessions))
    timing.apply(response)
    return result


@router.get("/recall/status")
async def recall_index_status(
    database_url: str | None = Depends(get_recall_database_url),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    """Return retrieval.db recall index status."""

    if database_module.live_catalog_enabled():
        return {"status": "retired", "reason": "storage_v2_search_owned"}
    assert database_url is not None

    retrieval_path = resolve_retrieval_db_path(database_url)
    if retrieval_path is None:
        return {"status": "unavailable", "reason": "main database is not file-backed SQLite"}
    if not retrieval_path.exists():
        return {"status": "missing", "path": str(retrieval_path), "chunk_count": 0, "child_chunk_count": 0}
    with connect_retrieval_db_readonly(retrieval_path) as retrieval_db:
        if not retrieval_schema_ready(retrieval_db):
            return {"status": "uninitialized", "path": str(retrieval_path), "chunk_count": 0, "child_chunk_count": 0}
        chunk_count = int(retrieval_db.execute("SELECT count(*) FROM recall_chunks").fetchone()[0])
        searchable_count = child_chunk_count(retrieval_db)
        latest_job = get_latest_recall_index_job(retrieval_db) if recall_index_jobs_table_ready(retrieval_db) else None
        return {
            "status": "ready" if searchable_count > 0 else "empty",
            "path": str(retrieval_path),
            "chunk_count": chunk_count,
            "child_chunk_count": searchable_count,
            "latest_job": latest_job.as_dict() if latest_job is not None else None,
        }


@router.post("/recall/index")
async def index_recall_sessions(
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    since_days: int = Query(90, ge=1, le=365, description="Days to index"),
    limit: int = Query(100, ge=1, le=1000, description="Max sessions to index"),
    database_url: str | None = Depends(get_recall_database_url),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    """Queue recent sessions for projection into retrieval.db."""

    if database_module.live_catalog_enabled():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "recall_index_retired", "message": "Search indexing is owned by storage v2."},
        )
    assert database_url is not None

    retrieval_path = resolve_retrieval_db_path(database_url)
    if retrieval_path is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval index unavailable: main database is not file-backed SQLite.",
        )

    with connect_retrieval_db(retrieval_path) as retrieval_db:
        initialize_retrieval_db(retrieval_db)
        job, created = enqueue_recall_index_job(
            retrieval_db,
            project=project,
            provider=provider,
            since_days=since_days,
            limit=limit,
        )
    wake_recall_index_worker()

    return {
        "status": "queued" if created else job.status,
        "path": str(retrieval_path),
        "created": created,
        "job": job.as_dict(),
    }


@router.get("/recall/index/{job_id}")
async def recall_index_job_status(
    job_id: str,
    database_url: str | None = Depends(get_recall_database_url),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    if database_module.live_catalog_enabled():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "recall_index_retired", "message": "Search indexing is owned by storage v2."},
        )
    assert database_url is not None
    retrieval_path = resolve_retrieval_db_path(database_url)
    if retrieval_path is None or not retrieval_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Retrieval index unavailable.")
    with connect_retrieval_db(retrieval_path) as retrieval_db:
        initialize_retrieval_db(retrieval_db)
        job = get_recall_index_job(retrieval_db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recall index job not found.")
    return {"job": job.as_dict()}


@router.post("/recall/index/{job_id}/cancel")
async def cancel_recall_index_job(
    job_id: str,
    database_url: str | None = Depends(get_recall_database_url),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> dict[str, object]:
    if database_module.live_catalog_enabled():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "recall_index_retired", "message": "Search indexing is owned by storage v2."},
        )
    assert database_url is not None
    retrieval_path = resolve_retrieval_db_path(database_url)
    if retrieval_path is None or not retrieval_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Retrieval index unavailable.")
    with connect_retrieval_db(retrieval_path) as retrieval_db:
        initialize_retrieval_db(retrieval_db)
        job = request_recall_index_cancel(retrieval_db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recall index job not found.")
    wake_recall_index_worker()
    return {"job": job.as_dict()}


@router.get("/recall", response_model=RecallResponse)
async def recall_sessions(
    request: Request,
    response: Response = None,
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=20, description="Max matches"),
    context_turns: int = Query(2, ge=0, le=10, description="Context turns before/after match"),
    context_mode: str = Query("forensic", description="Context projection mode: forensic|active_context"),
    include_automation: bool = Query(False, description="Include Hatch automation sessions in recall results"),
    mode: str = "auto",
    database_url: str | None = Depends(get_recall_database_url),
    session_factory: sessionmaker | None = Depends(get_recall_session_factory),
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RecallResponse:
    """Recall specific knowledge from past sessions."""
    from zerg.models_config import embedding_unavailable_detail
    from zerg.models_config import get_embedding_config
    from zerg.services.embedding_cache import EmbeddingCache
    from zerg.services.session_processing.embeddings import generate_embedding

    handler_started = time.perf_counter()
    request_started = getattr(request.state, "request_timeout_started_at", None)
    pre_handler_ms = (handler_started - request_started) * 1000 if isinstance(request_started, float) else None
    timing = ServerTimingRecorder(surface="recall")

    def remaining_budget() -> float:
        started = request_started if isinstance(request_started, float) else handler_started
        return max(0.05, RECALL_ROUTE_TIMEOUT_SECONDS - (time.perf_counter() - started) - 0.1)

    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )
    if mode not in {"auto", "lexical", "semantic"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be one of: auto, lexical, semantic",
        )

    if database_module.live_catalog_enabled():
        if context_mode != "forensic":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "search_mode_unsupported",
                    "message": "Storage-v2 search does not yet project active-context boundaries.",
                },
            )
        owner_id = _catalog_owner_id(_auth)
        with timing.span("discovery"):
            rows = await search_storage_v2_rows(
                owner_id=owner_id,
                query=query,
                project=project,
                provider=provider,
                environment=None,
                days_back=since_days,
                limit=min(200, max_results * 8),
                timeout_seconds=remaining_budget(),
            )
        matches: list[RecallMatch] = []
        seen: set[str] = set()
        for row in rows:
            session_id = str(row.get("session_id") or "")
            environment = str(row.get("environment") or "")
            if not session_id or session_id in seen:
                continue
            if not include_test and environment in {"test", "e2e"}:
                continue
            if not include_automation and environment == "automation":
                continue
            seen.add(session_id)
            snippet = str(row.get("content_snippet") or row.get("tool_output_snippet") or "")
            matches.append(
                RecallMatch(
                    session_id=session_id,
                    chunk_index=int(row.get("record_ordinal") or 0),
                    score=1.0 / (1.0 + abs(float(row.get("rank") or 0.0))),
                    context_text=snippet or None,
                    evidence=snippet or None,
                    total_events=int(row.get("event_count") or 0),
                    context=[],
                    match_event_id=int(row["search_event_id"]) if row.get("search_event_id") is not None else None,
                    generation_id=str(row.get("generation_id") or "") or None,
                    source_object_id=str(row.get("source_object_id") or "") or None,
                    record_ordinal=int(row.get("record_ordinal") or 0),
                )
            )
            if len(matches) >= max_results:
                break

        async def hydrate(match: RecallMatch) -> None:
            if match.match_event_id is None or match.generation_id is None:
                match.evidence_status = "unavailable"
                match.evidence_reason = "search_hit_missing_locator"
                return
            try:
                evidence = await search_storage_v2_context(
                    owner_id=owner_id,
                    session_id=match.session_id,
                    generation_id=match.generation_id,
                    search_event_id=match.match_event_id,
                    context_turns=context_turns,
                    timeout_seconds=remaining_budget(),
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                match.evidence_status = "partial"
                match.evidence_reason = str(detail.get("code") or "search_evidence_unavailable")
                return
            match.context = [item for item in (evidence.get("context") or []) if isinstance(item, dict)]
            match.total_events = int(evidence.get("total_events") or match.total_events)
            match.evidence_status = str(evidence.get("evidence_status") or "complete")
            reason = evidence.get("evidence_reason")
            match.evidence_reason = str(reason) if reason is not None else None

        with timing.span("hydrate"):
            await asyncio.gather(*(hydrate(match) for match in matches))
        timing.apply(response)
        return RecallResponse(matches=matches, total=len(matches))

    assert database_url is not None
    assert session_factory is not None

    if mode in {"auto", "lexical"}:
        lexical_started = time.perf_counter()
        lexical_response = await _run_retrieval_index_recall(
            database_url,
            query=query,
            project=project,
            provider=provider,
            since_days=since_days,
            max_results=max_results,
            context_turns=context_turns,
            context_mode=context_mode,
            explicit=mode == "lexical",
            include_automation=include_automation,
        )
        lexical_ms = (time.perf_counter() - lexical_started) * 1000
        if lexical_response is not None:
            handler_ms = (time.perf_counter() - handler_started) * 1000
            if handler_ms > 1000 or (pre_handler_ms is not None and pre_handler_ms > 1000):
                logger.warning(
                    "Slow recall handler phase mode=lexical pre_handler_ms=%s handler_ms=%.1f lexical_ms=%.1f matches=%d",
                    f"{pre_handler_ms:.1f}" if pre_handler_ms is not None else "-",
                    handler_ms,
                    lexical_ms,
                    lexical_response.total,
                )
            return lexical_response

    if mode == "lexical":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Retrieval index unavailable: no searchable recall chunks are ready.",
        )

    config = get_embedding_config()
    if not config:
        raise _embedding_unavailable_response(embedding_unavailable_detail())

    query_vec = await generate_embedding(query, config)

    db = session_factory()
    try:
        cache = EmbeddingCache()
        if not cache._session_loaded:
            cache.load_session_embeddings(db, config.model, config.dims)
        if not cache._turn_loaded:
            cache.load_turn_embeddings(db, config.model, config.dims)

        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        filter_query = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
        if project:
            filter_query = filter_query.filter(AgentSession.project == project)
        if provider:
            filter_query = filter_query.filter(AgentSession.provider == provider)
        if not is_internal_canary_provider_filter(provider):
            filter_query = filter_query.filter(~internal_canary_session_clause(AgentSession))
        if not include_test:
            filter_query = filter_query.filter(AgentSession.environment.notin_(["test", "e2e"]))
            filter_query = filter_query.filter(~provider_proof_session_clause(AgentSession))
        if not include_automation:
            filter_query = filter_query.filter(
                or_(AgentSession.hidden_from_default_timeline.is_(None), AgentSession.hidden_from_default_timeline == 0)
            )
        valid_ids = {str(row[0]) for row in filter_query.all()}
        if valid_ids and cache.turn_embedding_count == 0:
            raise _embedding_corpus_unavailable_response("turn")

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

        clean_events_by_session: dict[str, list[CleanTranscriptEvent]] = {
            session_key: list(iter_clean_transcript_events([_event_to_clean_projection_dict(event) for event in events]))
            for session_key, events in events_by_session.items()
        }
        event_by_session_and_id: dict[str, dict[int, AgentEvent]] = {
            session_key: {event.id: event for event in events if event.id is not None} for session_key, events in events_by_session.items()
        }

        active_start_index_cache: dict[str, int] = {}
        if context_mode == "active_context":
            for session_id in ordered_session_ids:
                session_key = str(session_id)
                clean_events = clean_events_by_session.get(session_key, [])
                total_events = len(clean_events)
                boundary = store.get_active_context_boundary(session_id)
                if boundary is None:
                    active_start_index_cache[session_key] = 0
                    continue
                active_start_index = total_events
                event_by_id = event_by_session_and_id.get(session_key, {})
                for idx, clean_event in enumerate(clean_events):
                    event = event_by_id.get(clean_event.event_id or -1)
                    if event is not None and store.is_event_in_active_context(event, boundary):
                        active_start_index = idx
                        break
                active_start_index_cache[session_key] = active_start_index

        matches = []
        for session_id, chunk_index, score, event_start, event_end in results:
            clean_events = clean_events_by_session.get(str(session_id), [])
            total_events = len(clean_events)
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
                    if i < len(clean_events):
                        e = clean_events[i]
                        content = e.content
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

            match_event_id = clean_events[event_start].event_id if event_start is not None and event_start < total_events else None

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
    finally:
        db.close()


def _event_to_clean_projection_dict(event: AgentEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "role": event.role,
        "content_text": event.content_text,
        "tool_output_text": event.tool_output_text,
        "tool_name": event.tool_name,
        "timestamp": event.timestamp,
    }
