"""Search service — lexical and semantic session search.

Extracted from routers/agents.py to support ranking modes and hybrid RRF fusion.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from zerg.models.agents import AgentSession

logger = logging.getLogger(__name__)


@dataclass
class SessionFilters:
    """Common filter parameters for session search."""

    project: Optional[str] = None
    provider: Optional[str] = None
    environment: Optional[str] = None
    include_test: bool = False
    device_id: Optional[str] = None
    days_back: int = 14
    exclude_user_states: list[str] = field(default_factory=list)


def lexical_search(
    q: str,
    db: DBSession,
    filters: SessionFilters,
    limit: int,
    over_fetch: bool = False,
) -> list[AgentSession]:
    """FTS5 full-text search. Returns sessions ordered by BM25 rank (best first).

    When over_fetch=True, fetches min(limit * 3, 200) results for RRF fusion.
    """
    from zerg.services.agents_store import AgentsStore

    fetch_limit = min(limit * 3, 200) if over_fetch else limit
    since = datetime.now(timezone.utc) - timedelta(days=filters.days_back)

    store = AgentsStore(db)
    sessions, _ = store.list_sessions(
        project=filters.project,
        provider=filters.provider,
        environment=filters.environment,
        include_test=filters.include_test,
        device_id=filters.device_id,
        since=since,
        query=q,
        limit=fetch_limit,
        offset=0,
        exclude_user_states=filters.exclude_user_states,
    )
    return sessions


def semantic_search(
    q: str,
    db: DBSession,
    filters: SessionFilters,
    limit: int,
    over_fetch: bool = False,
) -> list[tuple[AgentSession, float]]:
    """Semantic similarity search over session embeddings.

    Returns list of (session, similarity_score) ordered by score descending.
    Returns empty list if embeddings are not configured.

    When over_fetch=True, fetches min(limit * 3, 200) results for RRF fusion.
    """
    import asyncio

    from zerg.models_config import get_embedding_config_with_db_fallback
    from zerg.services.embedding_cache import EmbeddingCache
    from zerg.services.session_processing.embeddings import generate_embedding

    config = get_embedding_config_with_db_fallback(db=db)
    if not config:
        return []

    fetch_limit = min(limit * 3, 200) if over_fetch else limit

    # Generate query embedding (sync wrapper for use in sync context)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're in an async context; use thread pool to avoid nested event loops
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, generate_embedding(q, config))
                query_vec = future.result(timeout=30)
        else:
            query_vec = loop.run_until_complete(generate_embedding(q, config))
    except Exception:
        logger.exception("Failed to generate query embedding for semantic search")
        return []

    cache = EmbeddingCache()
    if not cache._session_loaded:
        cache.load_session_embeddings(db, config.model, config.dims)

    since = datetime.now(timezone.utc) - timedelta(days=filters.days_back)
    filter_query = db.query(AgentSession.id).filter(AgentSession.started_at >= since)
    if filters.project:
        filter_query = filter_query.filter(AgentSession.project == filters.project)
    if filters.provider:
        filter_query = filter_query.filter(AgentSession.provider == filters.provider)
    if filters.environment:
        filter_query = filter_query.filter(AgentSession.environment == filters.environment)
    valid_ids = {str(row[0]) for row in filter_query.all()}

    results = cache.search_sessions(query_vec, limit=fetch_limit, session_filter=valid_ids)

    sessions_with_scores: list[tuple[AgentSession, float]] = []
    for sid, score in results:
        session = db.query(AgentSession).filter(AgentSession.id == sid).first()
        if session:
            sessions_with_scores.append((session, score))

    return sessions_with_scores


_RRF_K = 60


def rrf_fuse(
    lexical_hits: list[AgentSession],
    semantic_hits: list[tuple[AgentSession, float]],
    limit: int,
) -> list[AgentSession]:
    """Standard Reciprocal Rank Fusion.

    rrf_score(d) = sum(1 / (K + rank)) for lists where d appears.
    Only sums over lists where the document appears (no missing_penalty).
    Ranks are 1-based.
    Tie-break: score DESC, ended_at DESC, id ASC (stable).
    """
    scores: dict[str, float] = {}

    for rank_0, session in enumerate(lexical_hits):
        sid = str(session.id)
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (_RRF_K + rank_0 + 1)

    for rank_0, (session, _sim) in enumerate(semantic_hits):
        sid = str(session.id)
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (_RRF_K + rank_0 + 1)

    # Merge session objects (prefer lexical copy; dedup by id)
    all_sessions: dict[str, AgentSession] = {str(s.id): s for s in lexical_hits}
    for s, _ in semantic_hits:
        all_sessions.setdefault(str(s.id), s)

    def sort_key(s: AgentSession):
        rrf = scores.get(str(s.id), 0.0)
        ts = (s.ended_at or s.started_at).timestamp() if (s.ended_at or s.started_at) else 0.0
        return (-rrf, -ts, str(s.id))

    ordered = sorted(all_sessions.values(), key=sort_key)
    return ordered[:limit]


def apply_sort(
    sessions: list[AgentSession],
    sort: str,
    bm25_order: Optional[list[str]] = None,
) -> list[AgentSession]:
    """Apply ranking mode to a result list.

    sort: "relevance" | "recency" | "balanced"
    bm25_order: list of session id strings in BM25 rank order (for relevance/balanced)
    """
    if sort == "recency":
        return sorted(
            sessions,
            key=lambda s: (
                -(s.ended_at or s.started_at).timestamp() if (s.ended_at or s.started_at) else 0,
                str(s.id),
            ),
        )

    if sort == "relevance":
        if bm25_order:
            rank_map = {sid: i for i, sid in enumerate(bm25_order)}
            return sorted(sessions, key=lambda s: (rank_map.get(str(s.id), len(sessions)), str(s.id)))
        # No explicit BM25 order: fall back to recency
        return apply_sort(sessions, "recency")

    if sort == "balanced":
        n = len(sessions)
        if n == 0:
            return sessions

        bm25_order = bm25_order or []
        rank_map = {sid: i for i, sid in enumerate(bm25_order)}
        now_ts = datetime.now(timezone.utc).timestamp()

        def blend(s: AgentSession) -> float:
            rank_idx = rank_map.get(str(s.id), n)
            norm_rank = 1.0 - rank_idx / max(n, 1)

            session_ts = (s.ended_at or s.started_at).timestamp() if (s.ended_at or s.started_at) else 0.0
            days_ago = max(0, (now_ts - session_ts) / 86400)
            norm_recency = math.exp(-days_ago / 30)

            return 0.5 * norm_rank + 0.5 * norm_recency

        return sorted(sessions, key=lambda s: (-blend(s), str(s.id)))

    # Unknown sort value: fall back to recency
    return apply_sort(sessions, "recency")
