"""Agents API — semantic search and recall endpoints."""

import asyncio
import base64
import logging
import os
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

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.database import catalog_db_dependency
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.live_catalog_timeline import read_live_catalog_session
from zerg.services.searchd_supervisor import get_searchd_client
from zerg.services.session_views import MachineSessionsListResponse
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import project_machine_session
from zerg.utils.server_timing import ServerTimingRecorder

router = APIRouter(prefix="/agents", tags=["agents"])
logger = logging.getLogger(__name__)
RECALL_ROUTE_TIMEOUT_SECONDS = 5.0

_catalog_db_dependency = catalog_db_dependency()


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
    search_event_id: int | None = None,
    start_order_time_us: int | None = None,
    context_turns: int,
    timeout_seconds: float,
) -> dict[str, object]:
    """Read bounded neighbor evidence from the same generation as a search hit.

    Lexical hits locate by searchd event id; semantic episode hits locate by
    their start position in the published ordering. Exactly one applies.
    """

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
                "start_order_time_us": start_order_time_us,
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


async def search_storage_v2_episode_embeddings(
    *,
    model: str,
    owner_id: int,
    dims: int,
    query_embedding: bytes,
    limit: int,
    timeout_seconds: float,
    project: str | None = None,
    provider: str | None = None,
    exclude_environments: list[str] | None = None,
    since_iso: str | None = None,
) -> list[dict[str, object]]:
    """Query the derived dense index through searchd, never its SQLite file.

    Scoping is a SQL predicate against searchd's own session_index (owner,
    project, provider, environment, recency), not an enumerated session id
    list -- a fixed-size id list caps out at real corpus scale (tens of
    thousands of sessions) well before it covers a tenant's full visible
    history, silently excluding exactly the older sessions a paraphrase
    query most needs to reach.
    """
    search = get_searchd_client()
    if search is None:
        return []
    result = await search.call(
        "search.embedding.query.v2",
        {
            "model": model,
            "owner_id": str(owner_id),
            "dims": dims,
            "query_embedding": base64.b64encode(query_embedding).decode("ascii"),
            "limit": min(200, max(1, limit)),
            "project": project,
            "provider": provider,
            "exclude_environments": exclude_environments,
            "since_iso": since_iso,
        },
        timeout_seconds=timeout_seconds,
    )
    return [row for row in (result.get("results") or []) if isinstance(row, dict)]


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
        if session.user_hidden_from_timeline:
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


async def _semantic_recall_matches(
    *,
    query: str,
    project: Optional[str],
    provider: Optional[str],
    since_days: int,
    include_test: bool,
    include_automation: bool,
    max_results: int,
    timeout_seconds: float,
    owner_id: int | None = None,
) -> list[RecallMatch]:
    """Dense recall over episode-level embeddings via searchd's episode_embeddings.

    The query is embedded in-process. It used to be a live third-party call
    whose 27s tail could not fit the 5s route budget, so the lane silently
    returned nothing on every slow call while spending the whole budget; see
    zerg/services/local_embedder.py.

    Returns the full ranked list (not deduped against lexical results) so
    the caller can run real reciprocal rank fusion: a session found by both
    lanes should get credit from both, not just whichever ran first.
    """
    from zerg.models_config import get_embedding_config
    from zerg.services.local_embedder import LocalEmbedderUnavailable
    from zerg.services.local_embedder import embed_query

    if os.getenv("TESTING") == "1":
        # Tests do not carry model weights; skip deterministically rather than
        # depend on a downloaded artifact.
        return []

    if timeout_seconds <= 0 or owner_id is None:
        return []

    config = get_embedding_config()
    if not config or not query:
        return []

    async def _run() -> list[RecallMatch]:
        query_vec = await embed_query(query)

        # Scoping is a SQL predicate against searchd's own session_index
        # (owner/project/provider/environment/recency), not an enumerated
        # session id list -- an earlier version of this paginated the
        # owner's full visible listing client-side and passed ids as a
        # filter, which caps out well before covering a real tenant's full
        # history (tens of thousands of sessions), silently excluding
        # exactly the older sessions a paraphrase query most needs to
        # reach. See search.embedding.query.v2 / query_episode_embeddings.
        exclude_environments: list[str] = []
        if not include_test:
            exclude_environments.extend(["test", "e2e"])
        if not include_automation:
            exclude_environments.append("automation")
        since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        rows = await search_storage_v2_episode_embeddings(
            model=config.model,
            owner_id=owner_id,
            dims=config.dims,
            query_embedding=query_vec.astype("float32").tobytes(),
            limit=max_results * 3,
            timeout_seconds=timeout_seconds,
            project=project,
            provider=provider,
            exclude_environments=exclude_environments or None,
            since_iso=since_iso,
        )
        matches: list[RecallMatch] = []
        seen: set[str] = set()
        for row in rows:
            session_id = str(row.get("session_id") or "")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            start_order_time_us = row.get("start_order_time_us")
            matches.append(
                RecallMatch(
                    session_id=session_id,
                    chunk_index=int(row.get("episode_ordinal") or 0),
                    score=float(row.get("score") or 0.0),
                    event_index_start=row.get("event_index_start"),
                    event_index_end=row.get("event_index_end"),
                    # Carrying the generation the episode was embedded from is
                    # what lets hydration refuse a superseded transcript instead
                    # of showing neighbours that have since moved.
                    generation_id=str(row.get("generation_id") or "") or None,
                    start_order_time_us=int(start_order_time_us) if start_order_time_us is not None else None,
                )
            )
            if len(matches) >= max_results:
                break
        return matches

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except LocalEmbedderUnavailable:
        # A missing model is a deployment fault, not "no results". Swallowing it
        # is how the previous remote lane stayed dead for days: an empty list is
        # indistinguishable from an honest miss.
        logger.error("Dense recall unavailable: local embedder is not loaded")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "embedder_unavailable", "message": "The local embedding model is not loaded."},
        ) from None
    except TimeoutError:
        logger.warning("Dense recall exceeded its %.2fs budget", timeout_seconds)
        return []


# Hydration gets its own slice of the request rather than the remainder. A lane
# that runs long must cost results, not evidence.
HYDRATION_RESERVED_SECONDS = 1.0

# Fuse deeper than we return. Truncating each lane to max_results before RRF
# means agreement below that rank can never surface, which is most of what rank
# fusion is for.
CANDIDATE_DEPTH_FACTOR = 5


def _rank_single_lane(matches: list[RecallMatch], *, limit: int) -> list[RecallMatch]:
    """Order one lane's own scores, deterministically.

    Ties break on session id so a repeated query cannot reorder its own results,
    which would read as retrieval instability rather than a coin flip.
    """

    ordered = sorted(matches, key=lambda m: (-m.score, m.session_id))
    return ordered[:limit]


async def _lexical_recall_matches(
    *,
    owner_id: int,
    query: str,
    project: Optional[str],
    provider: Optional[str],
    since_days: int,
    include_test: bool,
    include_automation: bool,
    candidate_depth: int,
    timeout_seconds: float,
) -> list[RecallMatch]:
    """FTS discovery, one match per session, best row wins."""

    rows = await search_storage_v2_rows(
        owner_id=owner_id,
        query=query,
        project=project,
        provider=provider,
        environment=None,
        days_back=since_days,
        limit=min(200, candidate_depth),
        timeout_seconds=timeout_seconds,
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
        if len(matches) >= candidate_depth:
            break
    return matches


async def _hydrate_recall_match(
    match: RecallMatch,
    *,
    owner_id: int,
    context_turns: int,
    timeout_seconds: float,
) -> None:
    """Attach neighbour evidence to a match and record how well that went.

    Every exit sets ``evidence_status`` explicitly. Nothing here may leave it at
    the model default, because a default is a claim nobody checked.
    """

    if match.generation_id is None or (match.match_event_id is None and match.start_order_time_us is None):
        match.evidence_status = "unavailable"
        match.evidence_reason = "search_hit_missing_locator"
        return
    try:
        evidence = await search_storage_v2_context(
            owner_id=owner_id,
            session_id=match.session_id,
            generation_id=match.generation_id,
            search_event_id=match.match_event_id,
            # An event id is exact; a position is an anchor. Prefer the exact one
            # when a match somehow carries both.
            start_order_time_us=None if match.match_event_id is not None else match.start_order_time_us,
            context_turns=context_turns,
            timeout_seconds=timeout_seconds,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        match.evidence_status = "partial"
        match.evidence_reason = str(detail.get("code") or "search_evidence_unavailable")
        return
    match.context = [item for item in (evidence.get("context") or []) if isinstance(item, dict)]
    match.total_events = int(evidence.get("total_events") or match.total_events)
    # Only the store may declare completeness. An absent status means the
    # response did not carry one, which is not evidence that everything arrived
    # — reading it as "complete" is how a silent contract change would look
    # like a healthy result.
    reported = str(evidence.get("evidence_status") or "")
    match.evidence_status = reported if reported in {"complete", "partial", "unavailable"} else "partial"
    reason = evidence.get("evidence_reason")
    if reason is not None:
        match.evidence_reason = str(reason)
    elif not reported:
        match.evidence_reason = "search_evidence_status_absent"
    else:
        match.evidence_reason = None
    if match.evidence is None:
        match.evidence = _anchor_excerpt(match)
        match.context_text = match.evidence


def _anchor_excerpt(match: RecallMatch) -> str | None:
    """Text at the point the match anchors on, for matches with no snippet.

    The lexical lane fills ``evidence`` from its FTS snippet; the semantic lane
    has no snippet, so it was returning null evidence beside a fully populated
    ``context``. A caller checking ``evidence`` to decide whether a hit is worth
    reading would skip a match whose evidence was sitting right next to it.
    """

    if match.start_order_time_us is None or not match.context:
        return None
    for item in match.context:
        order_time = item.get("order_time_us")
        if not isinstance(order_time, int) or order_time < match.start_order_time_us:
            continue
        text = str(item.get("content_text") or "").strip()
        if text:
            return text[:_ANCHOR_EXCERPT_MAX_CHARS]
    return None


# Long enough to judge relevance, short enough that five of them do not crowd out
# the context they are summarizing.
_ANCHOR_EXCERPT_MAX_CHARS = 400


def _rrf_merge_recall_matches(
    lexical: list[RecallMatch],
    semantic: list[RecallMatch],
    *,
    limit: int,
) -> list[RecallMatch]:
    """Reciprocal rank fusion over two ranked RecallMatch lists, keyed by session_id.

    A session found by both lanes accumulates a score contribution from each
    (standard RRF agreement credit). When a session appears in both, the
    match object with the *better* individual rank wins for evidence display
    -- picking the lexical copy unconditionally would show weaker evidence
    for a session that the dense lane actually ranked far higher.
    """
    k = 60
    scores: dict[str, float] = {}
    best_rank: dict[str, tuple[int, RecallMatch]] = {}

    for rank, match in enumerate(lexical):
        sid = match.session_id
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
        if sid not in best_rank or rank < best_rank[sid][0]:
            best_rank[sid] = (rank, match)
    for rank, match in enumerate(semantic):
        sid = match.session_id
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
        if sid not in best_rank or rank < best_rank[sid][0]:
            best_rank[sid] = (rank, match)

    ordered_ids = sorted(scores, key=lambda sid: scores[sid], reverse=True)
    merged = []
    for sid in ordered_ids[:limit]:
        match = best_rank[sid][1]
        # Report the value the ordering was actually made from. The lanes score
        # on incomparable scales — lexical is 1/(1+|bm25|), semantic is cosine —
        # so passing the winner's raw lane score through made a fused list look
        # arbitrarily ordered to anyone reading the numbers.
        match.score = scores[sid]
        merged.append(match)
    return merged


@router.get("/sessions/semantic", response_model=MachineSessionsListResponse)
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
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> MachineSessionsListResponse:
    """Search sessions by semantic similarity, scoped to the owner's storage-v2 catalog."""
    timing = ServerTimingRecorder(surface="search")
    if context_mode not in {"forensic", "active_context"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="context_mode must be one of: forensic, active_context",
        )
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
    # Machine surface: identity and provenance, none of the browser's control
    # or presentation state. Ten full session payloads exceeded the MCP token
    # cap before any transcript content came back.
    result = MachineSessionsListResponse(
        sessions=[project_machine_session(session) for session in sessions],
        total=len(sessions),
        has_real_sessions=bool(sessions),
    )
    timing.apply(response)
    return result


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
    _auth: object = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> RecallResponse:
    """Recall specific knowledge from past sessions."""
    handler_started = time.perf_counter()
    request_started = getattr(request.state, "request_timeout_started_at", None)
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
    if context_mode != "forensic":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "search_mode_unsupported",
                "message": "Storage-v2 search does not yet project active-context boundaries.",
            },
        )

    owner_id = _catalog_owner_id(_auth)
    candidate_depth = min(200, max(max_results, max_results * CANDIDATE_DEPTH_FACTOR))

    # Reserved, not leftover. Hydration used to receive whatever discovery had
    # not already spent, which in practice was the 0.05s floor, so matches came
    # back "partial / search_evidence_unavailable" whenever a lane ran long.
    discovery_deadline = max(0.05, remaining_budget() - HYDRATION_RESERVED_SECONDS)

    async def lexical() -> list[RecallMatch]:
        with timing.span("lexical"):
            return await _lexical_recall_matches(
                owner_id=owner_id,
                query=query,
                project=project,
                provider=provider,
                since_days=since_days,
                include_test=include_test,
                include_automation=include_automation,
                candidate_depth=candidate_depth,
                timeout_seconds=discovery_deadline,
            )

    async def dense() -> list[RecallMatch]:
        with timing.span("dense"):
            return await _semantic_recall_matches(
                query=query,
                project=project,
                provider=provider,
                since_days=since_days,
                include_test=include_test,
                include_automation=include_automation,
                max_results=candidate_depth,
                timeout_seconds=discovery_deadline,
                owner_id=owner_id,
            )

    # Each mode runs exactly the lanes it names. `semantic` used to run lexical
    # first and fuse, which made the lane-specific evaluation meaningless: every
    # mode measured some amount of lexical.
    if mode == "lexical":
        matches = _rank_single_lane(await lexical(), limit=max_results)
        lanes = ("lexical",)
    elif mode == "semantic":
        matches = _rank_single_lane(await dense(), limit=max_results)
        lanes = ("dense",)
    else:
        lexical_matches, dense_matches = await asyncio.gather(lexical(), dense())
        matches = _rrf_merge_recall_matches(lexical_matches, dense_matches, limit=max_results)
        lanes = ("lexical", "dense")

    # Hydrate after fusion, not before. Hydrating the lexical list first meant
    # semantic matches never reached the hydrator at all — they were appended
    # afterwards and went out with empty evidence — while lexical matches that
    # fusion then dropped were hydrated for nothing.
    with timing.span("hydrate"):
        await asyncio.gather(
            *(
                _hydrate_recall_match(
                    match,
                    owner_id=owner_id,
                    context_turns=context_turns,
                    timeout_seconds=max(0.05, remaining_budget()),
                )
                for match in matches
            )
        )

    timing.apply(response)
    return RecallResponse(matches=matches, total=len(matches), lanes=list(lanes))
