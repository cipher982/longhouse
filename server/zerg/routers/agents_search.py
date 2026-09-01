"""Agents API — semantic search and recall endpoints."""

import asyncio
import base64
import binascii
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.database import catalog_db_dependency
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_caller
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.live_catalog_timeline import read_live_catalog_session
from zerg.services.live_catalog_timeline import read_live_catalog_sessions
from zerg.services.searchd_supervisor import get_searchd_client
from zerg.services.session_views import RECALL_COVERAGE_MAX_NAMED_SESSIONS
from zerg.services.session_views import MachineSearchLaneFailure
from zerg.services.session_views import MachineSessionResponse
from zerg.services.session_views import MachineSessionsListResponse
from zerg.services.session_views import RecallContextResponse
from zerg.services.session_views import RecallContextTurn
from zerg.services.session_views import RecallCoverage
from zerg.services.session_views import RecallCoverageSummary
from zerg.services.session_views import RecallExpandedTurn
from zerg.services.session_views import RecallLaneFailure
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import RecallSearchResult
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import project_machine_session
from zerg.utils.server_timing import ServerTimingRecorder

router = APIRouter(prefix="/agents", tags=["agents"])
logger = logging.getLogger(__name__)
# This is the broken-request bound, not the latency target. The hosted FTS
# corpus is larger than RAM and a valid cold-page query can take longer than
# two seconds even though warm queries remain sub-second. Five seconds keeps
# the explicit hydration reserve and stays within searchd's hard RPC ceiling;
# timing telemetry and release gates enforce ordinary latency separately.
RECALL_ROUTE_TIMEOUT_SECONDS = 5.0
RECALL_SEARCH_SNIPPET_BYTES = 320
RECALL_CONTEXT_TOTAL_BYTES = 8 * 1024
RECALL_CONTEXT_MAX_TURN_BYTES = 4_000
RECALL_SEARCH_RESULT_LIMIT = 10
RECALL_SERIALIZED_RESPONSE_BYTES = 12 * 1024
_RECALL_REF_PREFIX = "rr1_"
_RECALL_SNIPPET_TRUNCATION_MARKER = " …[truncated]"


def _apply_recall_diagnostic_headers(response: Response | None, *, include_dense: bool) -> None:
    """Keep release diagnostics off the model-visible JSON payload."""

    if response is None:
        return
    from zerg.build_info import BuildIdentityMissing
    from zerg.build_info import load

    try:
        response.headers["X-Longhouse-Commit"] = load().commit
    except BuildIdentityMissing:
        pass
    if include_dense:
        from zerg.embedding_space import ACTIVE_EMBEDDING_DIMS
        from zerg.embedding_space import ACTIVE_EMBEDDING_MODEL
        from zerg.embedding_space import EMBEDDING_ARTIFACT_REVISION
        from zerg.embedding_space import EMBEDDING_PROJECTOR_ID

        response.headers["X-Recall-Embedding-Model"] = ACTIVE_EMBEDDING_MODEL
        response.headers["X-Recall-Embedding-Dims"] = str(ACTIVE_EMBEDDING_DIMS)
        response.headers["X-Recall-Embedding-Revision"] = EMBEDDING_ARTIFACT_REVISION
        response.headers["X-Recall-Projector"] = EMBEDDING_PROJECTOR_ID


_catalog_db_dependency = catalog_db_dependency()


class _DenseEpisodeHit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    session_id: str
    episode_ordinal: int = Field(ge=0)
    score: float
    event_index_start: int | None = Field(ge=0)
    event_index_end: int | None = Field(ge=0)
    generation_id: str
    start_order_time_us: int = Field(ge=0)
    project: str | None
    provider: str | None
    started_at: str | None

    @model_validator(mode="after")
    def validate_identity(self) -> "_DenseEpisodeHit":
        for field, value in (("session_id", self.session_id), ("generation_id", self.generation_id)):
            try:
                parsed = UUID(value)
            except ValueError as exc:
                raise ValueError(f"{field} must be a canonical UUID") from exc
            if str(parsed) != value:
                raise ValueError(f"{field} must be a canonical UUID")
        if self.event_index_start is not None and self.event_index_end is not None and self.event_index_end < self.event_index_start:
            raise ValueError("dense hit event range is invalid")
        return self


class _DenseQueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    results: list[_DenseEpisodeHit]
    coverage: "_EmbeddingCoveragePayload"
    store_id: str
    schema_generation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_store_identity(self) -> "_DenseQueryPayload":
        try:
            parsed = UUID(self.store_id)
        except ValueError as exc:
            raise ValueError("store_id must be a canonical UUID") from exc
        if str(parsed) != self.store_id:
            raise ValueError("store_id must be a canonical UUID")
        return self


class _EmbeddingCoveragePayload(BaseModel):
    """searchd's own account of the snapshot it just searched.

    The four corruption counters stay ``Literal[0]``: searchd refuses to serve a
    corrupt matrix, so seeing a nonzero count here means the daemon contradicted
    itself and the response must not be trusted. Publication shortfall is
    ordinary lag and is carried through to the caller instead of rejected.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    integrity_ready: Literal[True]
    complete: bool
    unpublished_sessions: int = Field(ge=0)
    expected_sessions: int = Field(ge=0)
    published_sessions: int = Field(ge=0)
    expected_episodes: int = Field(ge=0)
    current_episodes: int = Field(ge=0)
    invalid_vectors: Literal[0]
    unnormalized_vectors: Literal[0]
    unlocatable_episodes: Literal[0]
    episode_count_mismatches: Literal[0]
    missing_session_ids: list[str] = Field(default_factory=list)
    stale: bool

    @model_validator(mode="after")
    def validate_coverage_shape(self) -> "_EmbeddingCoveragePayload":
        if self.published_sessions > self.expected_sessions:
            raise ValueError("resident coverage published more sessions than it expects")
        if self.current_episodes > self.expected_episodes:
            raise ValueError("resident coverage holds more episodes than it expects")
        if self.unpublished_sessions != self.expected_sessions - self.published_sessions:
            raise ValueError("resident coverage shortfall is inconsistent")
        if self.complete != (self.unpublished_sessions == 0 and self.current_episodes == self.expected_episodes):
            raise ValueError("resident completeness contradicts its own counts")
        return self


class _SearchReadTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    admit_ms: float
    sql_ms: float
    active_readers: int
    queued_readers: int


class _RecallContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_status: Literal["complete", "partial", "unavailable"]
    evidence_reason: str | None
    anchor_event_id: int | None = Field(default=None, ge=1)
    context: list[RecallContextTurn]
    total_events: int = Field(ge=0)
    timing: _SearchReadTiming


class _ProjectorStoreBindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    store_id: str
    schema_generation: str = Field(min_length=1)
    commit_seq: str = Field(pattern=r"^[0-9]+$")

    @model_validator(mode="after")
    def validate_store_id(self) -> "_ProjectorStoreBindingPayload":
        try:
            parsed = UUID(self.store_id)
        except ValueError as exc:
            raise ValueError("store_id must be a canonical UUID") from exc
        if str(parsed) != self.store_id:
            raise ValueError("store_id must be a canonical UUID")
        return self


class _ProjectorCoveragePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    projector: str = Field(min_length=1)
    store_binding: _ProjectorStoreBindingPayload | None
    lag_count: int = Field(ge=0)
    indexed_through: str = Field(pattern=r"^[0-9]+$")
    oldest_lag_at: str | None
    oldest_lag_seconds: float | None = Field(ge=0, allow_inf_nan=False)
    commit_seq: str = Field(pattern=r"^[0-9]+$")
    observed_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_head_shape(self) -> "_ProjectorCoveragePayload":
        if self.lag_count == 0 and (self.oldest_lag_at is not None or self.oldest_lag_seconds is not None):
            raise ValueError("zero projector lag cannot have an oldest lag")
        if self.lag_count == 0 and self.indexed_through != self.commit_seq:
            raise ValueError("zero projector lag requires the current catalog watermark")
        if self.lag_count > 0 and (self.oldest_lag_at is None or self.oldest_lag_seconds is None):
            raise ValueError("nonzero projector lag requires its oldest age")
        return self


async def _require_projection_coverage(*, timeout_seconds: float) -> _ProjectorCoveragePayload:
    """Require a bound store, and report exactly how current it is.

    This used to also require a durable "cutover certificate": proof that the
    projector identity had once reached zero backlog. That answered a historical
    question -- was this generation ever completely caught up? -- which says
    nothing about the correctness of the snapshot being searched now, and it was
    unobtainable on a live instance, where lag is never zero. A new projector
    identity could therefore never certify, which made re-projecting the corpus
    impossible: bumping the identity to re-embed put every dense query into
    `cutover_not_certified` within seconds.

    What actually establishes the answer is already here. The store binding
    identifies the derived store; searchd proves generation, revision, dimension,
    normalization and locator invariants for the resident rows; projector
    completion is recorded only after the searchd mutation commits; and
    `indexed_through` is the catalog prefix that work represents. A store that
    has projected almost nothing serves almost nothing and reports a low
    watermark beside a large lag count -- degraded, but not silent, which is the
    property the certificate was reaching for.
    """

    from zerg.embedding_space import EMBEDDING_PROJECTOR_ID
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalog = get_catalogd_client()
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "coverage_status_unavailable", "message": "Embedding coverage status is unavailable."},
        )

    async def coverage(projector: str) -> _ProjectorCoveragePayload:
        try:
            result = await catalog.call(
                "projector.coverage.read.v2",
                {"projector": projector},
                timeout_seconds=timeout_seconds,
            )
        except (CatalogRemoteError, CatalogUnavailable) as exc:
            reason = exc.code if isinstance(exc, CatalogRemoteError) else "catalogd_unavailable"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "coverage_status_unavailable",
                    "message": "Embedding coverage status is unavailable.",
                    "reason": reason,
                },
            ) from exc
        try:
            return _ProjectorCoveragePayload.model_validate(result)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "coverage_status_unavailable",
                    "message": "Embedding coverage status is malformed.",
                    "reason": "invalid_catalog_response",
                },
            ) from exc

    embedding_coverage = await coverage(EMBEDDING_PROJECTOR_ID)
    # Without a store binding there is no derived store to attribute results to,
    # so serving would be a guess. Lag is not a refusal: it is reported.
    if embedding_coverage.store_binding is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "embedding_coverage_unproven",
                "message": "No derived store is bound for the embedding corpus.",
                "reason": "store_binding_missing",
                "catalog_coverage": {
                    EMBEDDING_PROJECTOR_ID: embedding_coverage.model_dump(),
                },
            },
        )
    return embedding_coverage


@dataclass(frozen=True)
class _DenseRecallResult:
    matches: list[RecallMatch]
    coverage: RecallCoverage


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
    include_snippets: bool = True,
    include_origin_hidden: bool = False,
    include_test: bool = False,
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
            "include_snippets": include_snippets,
            "include_origin_hidden": include_origin_hidden,
            "include_test": include_test,
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
    before_turns: int,
    after_turns: int,
    max_content_bytes: int,
    timeout_seconds: float,
) -> _RecallContextPayload:
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
                "before_turns": before_turns,
                "after_turns": after_turns,
                "max_content_bytes": max_content_bytes,
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
    return _RecallContextPayload.model_validate(result)


async def read_search_coverage(*, owner_id: int) -> dict[str, object] | None:
    """Best-effort scope of the searched index, for empty results only.

    Never raises: this exists to make a zero-hit answer honest, so failing to
    describe the corpus must not turn a successful empty search into an error.
    A missing coverage block is itself truthful -- it says nothing rather than
    something wrong.
    """

    search = get_searchd_client()
    if search is None:
        return None
    try:
        payload = await search.call("search.coverage.v2", {"owner_id": str(owner_id)}, timeout_seconds=1.5)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    # searchd decorates every result with its own `timing` block, and the
    # response model forbids extra fields, so hand back only the declared keys.
    # Passing the raw payload through made every coverage block fail validation
    # and silently come back null -- the failure mode this whole change exists
    # to avoid, reproduced one layer down.
    return {
        "indexed_sessions": payload.get("indexed_sessions", 0),
        "providers": payload.get("providers", []),
        "oldest_session_at": payload.get("oldest_session_at"),
        "newest_session_at": payload.get("newest_session_at"),
    }


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
    environment: str | None = None,
    include_origin_hidden: bool = False,
    include_test: bool = False,
) -> _DenseQueryPayload:
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
        raise CatalogUnavailable("searchd unavailable for search.embedding.query.v2")
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
            "environment": environment,
            "exclude_environments": exclude_environments,
            "since_iso": since_iso,
            "include_origin_hidden": include_origin_hidden,
            "include_test": include_test,
        },
        timeout_seconds=timeout_seconds,
    )
    return _DenseQueryPayload.model_validate(result)


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
    degraded: list[MachineSearchLaneFailure] | None = None,
) -> list[SessionResponse | MachineSessionResponse]:
    rows = await search_storage_v2_rows(
        owner_id=owner_id,
        query=query,
        project=project,
        provider=provider,
        environment=environment,
        days_back=days_back,
        limit=200,
        include_origin_hidden=include_automation,
        include_test=include_test,
    )
    best_rows: dict[str, dict[str, object]] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "")
        if session_id and session_id not in best_rows:
            best_rows[session_id] = row
    sessions: list[SessionResponse | MachineSessionResponse] = []
    candidates = [
        item
        for item in best_rows.items()
        if device_id is not None
        or _search_row_matches_filters(
            item[1],
            hide_autonomous=hide_autonomous,
            include_automation=include_automation,
            device_id=None,
        )
    ]
    projected: list[object] = []
    for offset in range(0, len(candidates), 20):
        page = candidates[offset : offset + 20]
        try:
            projected.extend(
                await asyncio.to_thread(
                    read_live_catalog_sessions,
                    [UUID(session_id) for session_id, _row in page],
                    owner_id=owner_id,
                )
            )
        except (CatalogReadError, CatalogRemoteError, CatalogUnavailable) as exc:
            projected.extend([exc] * len(page))
        if len(projected) >= limit:
            break
    for projection, (_session_id, row) in zip(projected, candidates):
        if isinstance(projection, BaseException):
            if not isinstance(projection, (CatalogReadError, CatalogRemoteError, CatalogUnavailable)):
                raise projection
            if degraded is not None and not any(item.lane == "catalog" for item in degraded):
                degraded.append(
                    MachineSearchLaneFailure(
                        lane="catalog",
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        code=getattr(projection, "code", None) or "catalog_unavailable",
                        message=getattr(projection, "message", None) or "The live catalog is temporarily unavailable.",
                        reason=(str(projection) if isinstance(projection, CatalogUnavailable) else None),
                    )
                )
            fallback = _machine_session_from_search_row(row)
            if fallback is not None and _search_row_matches_filters(
                row,
                hide_autonomous=hide_autonomous,
                include_automation=include_automation,
                device_id=device_id,
            ):
                sessions.append(fallback)
            if len(sessions) >= limit:
                break
            continue
        session, _provider_alias, _commit_seq = projection
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


def _search_row_matches_filters(
    row: dict[str, object],
    *,
    hide_autonomous: bool,
    include_automation: bool,
    device_id: str | None,
) -> bool:
    # searchd already applies owner, project, provider, environment, hidden,
    # archived, test, and tombstone policy. These are the remaining filters
    # that canonical hydration normally proves. Device identity is not in the
    # derived row, so fail closed if that filter was requested.
    if device_id is not None:
        return False
    if hide_autonomous and int(row.get("user_messages") or 0) <= 0:
        return False
    if not include_automation and str(row.get("environment") or "") == "automation":
        return False
    return True


def _machine_session_from_search_row(row: dict[str, object]) -> MachineSessionResponse | None:
    """Keep a search hit useful when canonical catalog hydration times out."""

    raw_started_at = row.get("started_at")
    if not isinstance(raw_started_at, str):
        return None
    try:
        started_at = datetime.fromisoformat(raw_started_at)
    except ValueError:
        return None
    rank = abs(float(row.get("rank") or 0.0))
    return MachineSessionResponse(
        id=str(row.get("session_id") or ""),
        provider=str(row.get("provider") or "unknown"),
        origin_kind=str(row["origin_kind"]) if row.get("origin_kind") is not None else None,
        project=str(row["project"]) if row.get("project") is not None else None,
        environment=str(row["environment"]) if row.get("environment") is not None else None,
        cwd=str(row["cwd"]) if row.get("cwd") is not None else None,
        git_repo=str(row["git_repo"]) if row.get("git_repo") is not None else None,
        started_at=started_at,
        last_activity_at=started_at,
        user_messages=int(row.get("user_messages") or 0),
        assistant_messages=int(row.get("assistant_messages") or 0),
        tool_calls=int(row.get("tool_calls") or 0),
        is_sidechain=bool(row.get("is_sidechain")),
        searchable=True,
        match_event_id=int(row["search_event_id"]) if row.get("search_event_id") is not None else None,
        match_snippet=str(row.get("content_snippet") or row.get("tool_output_snippet") or "") or None,
        match_role=str(row["role"]) if row.get("role") is not None else None,
        match_score=1.0 / (1.0 + rank),
    )


async def _semantic_recall(
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
    environment: Optional[str] = None,
) -> _DenseRecallResult:
    """Dense recall over episode-level embeddings via searchd's episode_embeddings.

    The query is embedded in-process. It used to be a live third-party call
    whose 27s tail could not fit the 5s route budget, so the lane silently
    returned nothing on every slow call while spending the whole budget; see
    zerg/services/local_embedder.py.

    Returns the full ranked list (not deduped against lexical results) so
    the caller can run real reciprocal rank fusion: a session found by both
    lanes should get credit from both, not just whichever ran first.
    """
    from zerg.embedding_space import EMBEDDING_PROJECTOR_ID
    from zerg.models_config import get_embedding_space_config
    from zerg.services.local_embedder import LocalEmbedderUnavailable
    from zerg.services.local_embedder import embed_query

    if timeout_seconds <= 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "dense_timed_out", "message": "Dense recall had no execution budget."},
        )
    if owner_id is None or not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_dense_request", "message": "Dense recall requires an owner and query."},
        )

    config = get_embedding_space_config()

    async def _run() -> _DenseRecallResult:
        query_vec, catalog_coverage = await asyncio.gather(
            embed_query(query),
            _require_projection_coverage(timeout_seconds=timeout_seconds),
        )

        # Scoping is a SQL predicate against searchd's own session_index
        # (owner/project/provider/environment/recency), not an enumerated
        # session id list -- an earlier version of this paginated the
        # owner's full visible listing client-side and passed ids as a
        # filter, which caps out well before covering a real tenant's full
        # history (tens of thousands of sessions), silently excluding
        # exactly the older sessions a paraphrase query most needs to
        # reach. See search.embedding.query.v2 and ResidentEpisodeIndex.
        exclude_environments: list[str] = []
        if not include_test:
            exclude_environments.extend(["test", "e2e"])
        if not include_automation:
            exclude_environments.append("automation")
        since_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        dense_payload = await search_storage_v2_episode_embeddings(
            model=config.model,
            owner_id=owner_id,
            dims=config.dims,
            query_embedding=query_vec.astype("float32").tobytes(),
            limit=max_results * 3,
            timeout_seconds=timeout_seconds,
            project=project,
            provider=provider,
            environment=environment,
            exclude_environments=exclude_environments or None,
            since_iso=since_iso,
            include_origin_hidden=include_automation,
            include_test=include_test,
        )
        if dense_payload.coverage.stale:
            # Catalog advancement precedes the searchd mutation, so a second
            # read after observing a stale resident snapshot captures the head
            # responsible for that staleness instead of relying on the earlier
            # concurrent observation.
            catalog_coverage = await _require_projection_coverage(
                timeout_seconds=timeout_seconds,
            )
        binding = catalog_coverage.store_binding
        if binding is None or (dense_payload.store_id, dense_payload.schema_generation) != (
            binding.store_id,
            binding.schema_generation,
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "embedding_coverage_incomplete",
                    "message": "The active embedding corpus is incomplete.",
                    "reason": "store_binding_mismatch",
                },
            )
        matches: list[RecallMatch] = []
        seen: set[str] = set()
        for raw_row in dense_payload.results:
            row = _DenseEpisodeHit.model_validate(raw_row)
            session_id = row.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            matches.append(
                RecallMatch(
                    session_id=session_id,
                    project=row.project,
                    provider=row.provider,
                    started_at=row.started_at,
                    chunk_index=row.episode_ordinal,
                    score=row.score,
                    event_index_start=row.event_index_start,
                    event_index_end=row.event_index_end,
                    # Carrying the generation the episode was embedded from is
                    # what lets hydration refuse a superseded transcript instead
                    # of showing neighbours that have since moved.
                    generation_id=row.generation_id,
                    start_order_time_us=row.start_order_time_us,
                )
            )
            if len(matches) >= max_results:
                break
        resident = dense_payload.coverage
        # `indexed_through` is one below the oldest lagging revision, so it is
        # exactly the point the corpus is provably current through. It was
        # already computed and returned by catalogd and simply never used; the
        # gate looked at lag age instead, which is why a single unfinished
        # session could refuse the whole corpus.
        coverage = RecallCoverage(
            complete_through_commit_seq=catalog_coverage.indexed_through,
            complete=catalog_coverage.lag_count == 0,
            unpublished_sessions=resident.unpublished_sessions,
            projector=EMBEDDING_PROJECTOR_ID,
            search_store_id=dense_payload.store_id,
            search_schema_generation=dense_payload.schema_generation,
            catalog_lag_count=catalog_coverage.lag_count,
            catalog_indexed_through=catalog_coverage.indexed_through,
            catalog_oldest_lag_at=catalog_coverage.oldest_lag_at,
            catalog_oldest_lag_seconds=catalog_coverage.oldest_lag_seconds,
            catalog_commit_seq=catalog_coverage.commit_seq,
            catalog_observed_at=catalog_coverage.observed_at,
            resident_stale=resident.stale,
            expected_sessions=resident.expected_sessions,
            published_sessions=resident.published_sessions,
            expected_episodes=resident.expected_episodes,
            current_episodes=resident.current_episodes,
            invalid_vectors=resident.invalid_vectors,
            unnormalized_vectors=resident.unnormalized_vectors,
            unlocatable_episodes=resident.unlocatable_episodes,
            episode_count_mismatches=resident.episode_count_mismatches,
            missing_session_ids=resident.missing_session_ids[:RECALL_COVERAGE_MAX_NAMED_SESSIONS],
        )
        return _DenseRecallResult(matches=matches, coverage=coverage)

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
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        reason = exc.code if isinstance(exc, CatalogRemoteError) else "searchd_unavailable"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": reason, "message": "The resident dense index is unavailable."},
        ) from exc
    except TimeoutError:
        logger.warning("Dense recall exceeded its %.2fs budget", timeout_seconds)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "dense_timed_out", "message": "Dense recall exceeded its execution budget."},
        ) from None


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
    environment: Optional[str] = None,
) -> list[RecallMatch]:
    result = await _semantic_recall(
        query=query,
        project=project,
        provider=provider,
        since_days=since_days,
        include_test=include_test,
        include_automation=include_automation,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
        owner_id=owner_id,
        environment=environment,
    )
    return result.matches


async def search_storage_v2_semantic_sessions(
    *,
    owner_id: int,
    query: str,
    project: str | None,
    provider: str | None,
    environment: str | None,
    days_back: int,
    limit: int,
    include_test: bool,
) -> list[SessionResponse]:
    """Return full session views ranked by the actual resident dense lane."""

    candidate_depth = min(200, max(limit, limit * CANDIDATE_DEPTH_FACTOR))
    matches = await _semantic_recall_matches(
        query=query,
        project=project,
        provider=provider,
        environment=environment,
        since_days=days_back,
        include_test=include_test,
        include_automation=False,
        max_results=candidate_depth,
        timeout_seconds=RECALL_ROUTE_TIMEOUT_SECONDS,
        owner_id=owner_id,
    )
    # One unreadable session must not fail the whole listing. These projections
    # each hit catalogd, and catalogd is a single writer that a corpus
    # re-projection can saturate; without this a transient RPC timeout on any one
    # candidate turned the entire route into a bare 500 with no indication of
    # which part failed. Candidates are over-fetched anyway, so a dropped one
    # costs at most a result.
    projected = await asyncio.gather(
        *(asyncio.to_thread(read_live_catalog_session, UUID(match.session_id), owner_id=owner_id) for match in matches),
        return_exceptions=True,
    )
    sessions: list[SessionResponse] = []
    unreadable = 0
    for projection, match in zip(projected, matches, strict=True):
        if isinstance(projection, BaseException):
            # `read_live_catalog_session` re-raises catalogd faults as
            # CatalogReadError, so catching only the client-level types missed
            # the one that actually reaches here.
            if not isinstance(projection, (CatalogReadError, CatalogRemoteError, CatalogUnavailable)):
                raise projection
            unreadable += 1
            continue
        session, _provider_alias, _commit_seq = projection
        if session is None or session.user_hidden_from_timeline or session.user_messages <= 0 or session.is_sidechain:
            continue
        if environment is not None and session.environment != environment:
            continue
        if not include_test and session.environment in {"test", "e2e"}:
            continue
        if session.environment == "automation":
            continue
        sessions.append(session.model_copy(update={"match_score": match.score}))
        if len(sessions) >= limit:
            break
    if unreadable:
        logger.warning(
            "Semantic session listing dropped %d of %d candidates whose catalog projection was unavailable",
            unreadable,
            len(matches),
        )
    if not sessions and unreadable:
        # Everything the dense lane found was unreadable, so an empty list would
        # claim the corpus holds nothing relevant. Fail loudly instead.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "session_projection_unavailable",
                "message": "The catalog could not project any matching session.",
                "unreadable_candidates": unreadable,
            },
        )
    return sessions


# Hydration gets its own slice of the request rather than the remainder. A lane
# that runs long must cost results, not evidence.
HYDRATION_RESERVED_SECONDS = 1.0

# Fuse deeper than we return. Truncating each lane to max_results before RRF
# means agreement below that rank can never surface, which is most of what rank
# fusion is for.
CANDIDATE_DEPTH_FACTOR = 5


def _discovery_budget(*, remaining_seconds: float) -> float:
    """Reserve time for the anchor hydration every recall result now requires."""

    return max(0.05, remaining_seconds - HYDRATION_RESERVED_SECONDS)


def _lane_result(outcome: object, *, lane: Literal["lexical", "dense"], degraded: list["RecallLaneFailure"]):
    """Unwrap one lane's gathered outcome, recording a typed failure if it faulted.

    Only `HTTPException` is treated as a lane fault. Anything else is a bug in
    this process rather than a lane being unavailable, and swallowing it would
    reproduce the original defect in a new place: a silently missing lane that
    looks exactly like an honest miss.
    """

    if not isinstance(outcome, BaseException):
        return outcome
    if not isinstance(outcome, HTTPException):
        raise outcome
    detail = outcome.detail if isinstance(outcome.detail, dict) else {}
    degraded.append(
        RecallLaneFailure(
            lane=lane,
            status_code=outcome.status_code,
            code=str(detail.get("code") or "lane_unavailable"),
            message=str(detail.get("message") or "The lane could not run."),
            reason=(str(detail["reason"]) if detail.get("reason") is not None else None),
        )
    )
    logger.warning("Recall lane degraded lane=%s code=%s", lane, detail.get("code"))
    return None


def _rank_single_lane(
    matches: list[RecallMatch],
    *,
    limit: int,
    lane: Literal["lexical", "dense"],
) -> list[RecallMatch]:
    """Order one lane's own scores, deterministically.

    Ties break on session id so a repeated query cannot reorder its own results,
    which would read as retrieval instability rather than a coin flip.
    """

    ordered = sorted(matches, key=lambda m: (-m.score, m.session_id))
    for rank, match in enumerate(ordered, start=1):
        match.retrieval_lanes = [lane]
        match.lane_ranks = {lane: rank}
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
        include_snippets=False,
        include_origin_hidden=include_automation,
        include_test=include_test,
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
                project=str(row.get("project") or "") or None,
                provider=str(row.get("provider") or "") or None,
                started_at=str(row.get("started_at") or "") or None,
                chunk_index=int(row.get("record_ordinal") or 0),
                score=1.0 / (1.0 + abs(float(row.get("rank") or 0.0))),
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
    timeout_seconds: float,
    max_content_bytes: int = RECALL_SEARCH_SNIPPET_BYTES,
) -> None:
    """Attach one bounded source turn to a search card candidate.

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
            before_turns=0,
            after_turns=0,
            max_content_bytes=max_content_bytes,
            timeout_seconds=timeout_seconds,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        match.evidence_status = "partial"
        match.evidence_reason = str(detail.get("code") or "search_evidence_unavailable")
        return
    evidence = _RecallContextPayload.model_validate(evidence)
    match.context = evidence.context
    if evidence.anchor_event_id is not None:
        match.match_event_id = evidence.anchor_event_id
    match.total_events = evidence.total_events
    anchor = next(
        (turn for turn in evidence.context if turn.search_event_id == evidence.anchor_event_id),
        None,
    )
    if anchor is not None:
        match.matched_role = anchor.role
        match.matched_tool_name = anchor.tool_name
    # Only the store may declare completeness. An absent status means the
    # response did not carry one, which is not evidence that everything arrived
    # — reading it as "complete" is how a silent contract change would look
    # like a healthy result.
    reported = evidence.evidence_status
    match.evidence_status = reported
    reason = evidence.evidence_reason
    if reason is not None:
        match.evidence_reason = str(reason)
    else:
        match.evidence_reason = None
    if match.evidence is None:
        match.evidence = _anchor_excerpt(match)
    # Search cards carry the snippet only. Neighbour turns are a distinct,
    # one-result expansion so N search hits can never multiply transcript text.
    match.context = []
    if match.evidence:
        match.evidence_status = "not_requested"
        match.evidence_reason = None


def _finalize_recall_evidence(matches: list[RecallMatch]) -> None:
    """Make every wire status an explicit, internally consistent fact."""

    for match in matches:
        has_evidence = bool(match.evidence)
        has_context = bool(match.context)
        if match.evidence_status == "complete" and (not has_evidence or not has_context):
            match.evidence_status = "partial"
            match.evidence_reason = "complete_without_materialized_evidence"
        if match.evidence_status == "partial" and not (has_evidence or has_context):
            match.evidence_status = "unavailable"
            match.evidence_reason = match.evidence_reason or "partial_without_materialized_evidence"
        if match.evidence_status == "unavailable" and (has_evidence or has_context):
            match.evidence_status = "partial"
            match.evidence_reason = match.evidence_reason or "context_unavailable"
        if match.evidence_status in {"partial", "unavailable"} and not match.evidence_reason:
            match.evidence_reason = f"{match.evidence_status}_without_reason"
        if match.evidence_status == "complete":
            match.evidence_reason = None
        if match.evidence_status == "not_requested":
            match.evidence_reason = None


def _anchor_excerpt(match: RecallMatch) -> str | None:
    """Text at the point the match anchors on, for matches with no snippet.

    The lexical lane fills ``evidence`` from its FTS snippet; the semantic lane
    has no snippet, so it was returning null evidence beside a fully populated
    ``context``. A caller checking ``evidence`` to decide whether a hit is worth
    reading would skip a match whose evidence was sitting right next to it.
    """

    if not match.context:
        return None
    if match.match_event_id is not None:
        for item in match.context:
            if item.search_event_id != match.match_event_id:
                continue
            text = item.content_text.strip()
            if text:
                return text[:_ANCHOR_EXCERPT_MAX_CHARS]
        return None
    if match.start_order_time_us is None:
        return None
    for item in match.context:
        if item.order_time_us < match.start_order_time_us:
            continue
        text = item.content_text.strip()
        if text:
            return text[:_ANCHOR_EXCERPT_MAX_CHARS]
    return None


# Long enough to judge relevance, short enough that five of them do not crowd out
# the context they are summarizing.
_ANCHOR_EXCERPT_MAX_CHARS = RECALL_SEARCH_SNIPPET_BYTES


@dataclass(frozen=True)
class _RecallRef:
    session_id: str
    generation_id: str
    search_event_id: int | None
    start_order_time_us: int | None


def _encode_recall_ref(match: RecallMatch) -> str:
    """Encode the exact published hit into a compact, stateless click target."""

    if match.generation_id is None:
        raise ValueError("recall result is missing its generation")
    if match.match_event_id is not None:
        kind = 0
        locator = match.match_event_id
    elif match.start_order_time_us is not None:
        kind = 1
        locator = match.start_order_time_us
    else:
        raise ValueError("recall result is missing its locator")
    payload = bytes([kind]) + UUID(match.session_id).bytes + UUID(match.generation_id).bytes + locator.to_bytes(8, "big")
    return _RECALL_REF_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_recall_ref(value: str) -> _RecallRef:
    """Decode a result reference without trusting it as authorization."""

    if not value.startswith(_RECALL_REF_PREFIX):
        raise ValueError("unsupported recall reference")
    token = value[len(_RECALL_REF_PREFIX) :]
    try:
        payload = base64.b64decode(token + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid recall reference encoding") from exc
    if len(payload) != 41 or payload[0] not in (0, 1):
        raise ValueError("invalid recall reference payload")
    locator = int.from_bytes(payload[33:], "big")
    if locator <= 0:
        raise ValueError("invalid recall reference locator")
    return _RecallRef(
        session_id=str(UUID(bytes=payload[1:17])),
        generation_id=str(UUID(bytes=payload[17:33])),
        search_event_id=locator if payload[0] == 0 else None,
        start_order_time_us=locator if payload[0] == 1 else None,
    )


def _recall_search_result(match: RecallMatch) -> RecallSearchResult:
    snippet = _bounded_recall_snippet(match.evidence) if match.evidence else None
    return RecallSearchResult(
        ref=_encode_recall_ref(match),
        session_id=match.session_id,
        project=match.project[:200] if match.project else None,
        provider=match.provider[:64] if match.provider else None,
        started_at=match.started_at[:64] if match.started_at else None,
        total_events=match.total_events,
        matched_role=match.matched_role,
        matched_tool_name=match.matched_tool_name[:128] if match.matched_tool_name else None,
        snippet=snippet,
        snippet_unavailable_reason=None if snippet else (match.evidence_reason or "snippet_unavailable"),
        matched_by=match.retrieval_lanes,
    )


def _bounded_recall_snippet(value: str) -> str | None:
    """Return one printable, byte-bounded card excerpt with an honest cut marker."""

    printable = "".join(" " if ord(character) < 32 or ord(character) == 127 else character for character in value)
    normalized = " ".join(printable.split()).strip()
    if not normalized:
        return None
    encoded = normalized.encode("utf-8")
    if len(encoded) <= RECALL_SEARCH_SNIPPET_BYTES:
        return normalized
    marker = _RECALL_SNIPPET_TRUNCATION_MARKER.encode("utf-8")
    prefix = encoded[: RECALL_SEARCH_SNIPPET_BYTES - len(marker)].decode("utf-8", "ignore").rstrip()
    return prefix + _RECALL_SNIPPET_TRUNCATION_MARKER


def _recall_search_results(matches: list[RecallMatch]) -> list[RecallSearchResult]:
    """Project every valid hit without letting one corrupt row erase the page."""

    results: list[RecallSearchResult] = []
    for match in matches:
        try:
            results.append(_recall_search_result(match))
        except (ValueError, ValidationError) as exc:
            logger.warning("Skipping unexpandable recall hit session_id=%s reason=%s", match.session_id, exc)
    return results


def _fit_recall_search_response(
    *,
    results: list[RecallSearchResult],
    lanes: list[Literal["lexical", "dense"]],
    degraded: list[RecallLaneFailure],
    coverage: RecallCoverageSummary | None = None,
) -> RecallResponse:
    """Keep the highest-ranked cards that fit the hard serialized page ceiling."""

    candidate = RecallResponse.model_construct(
        results=list(results),
        total=len(results),
        lanes=lanes,
        degraded=degraded,
        coverage=coverage,
    )
    dropped = 0
    while len(candidate.model_dump_json(exclude_none=True).encode("utf-8")) > RECALL_SERIALIZED_RESPONSE_BYTES:
        candidate.results.pop()
        candidate.total -= 1
        dropped += 1
    if dropped:
        logger.warning("Dropped %d trailing recall cards to enforce serialized response ceiling", dropped)
    return RecallResponse.model_validate(candidate.model_dump())


def _merge_evidence_reason(current: str | None, added: str) -> str:
    values = [value for value in (current, added) if value]
    return ",".join(dict.fromkeys(values))[:300]


def _context_response_size(response: RecallContextResponse) -> int:
    return len(response.model_dump_json(exclude_none=True).encode("utf-8"))


def _trim_context_turn(turn: RecallExpandedTurn, max_bytes: int) -> RecallExpandedTurn:
    encoded = turn.content_text.encode("utf-8")
    returned = encoded[:max_bytes].decode("utf-8", "ignore")
    full_bytes = turn.content_text_full_bytes or len(encoded)
    return turn.model_copy(
        update={
            "content_text": returned,
            "content_text_truncated": True,
            "content_text_full_bytes": full_bytes,
        }
    )


def _fit_recall_context_response(response: RecallContextResponse) -> RecallContextResponse:
    """Enforce the serialized ceiling by trimming evidence, never by returning 500."""

    if _context_response_size(response) <= RECALL_SERIALIZED_RESPONSE_BYTES:
        return RecallContextResponse.model_validate(response.model_dump())

    working = response.model_copy(deep=True)
    working.evidence_status = "partial"
    working.evidence_reason = _merge_evidence_reason(
        working.evidence_reason,
        "response_byte_ceiling_applied",
    )
    match_index = next(index for index, turn in enumerate(working.turns) if turn.is_match)
    trim_order = sorted(
        enumerate(working.turns),
        key=lambda item: (item[1].is_match, -abs(item[0] - match_index)),
    )
    for _, target in trim_order:
        if _context_response_size(working) <= RECALL_SERIALIZED_RESPONSE_BYTES:
            break
        index = next((index for index, turn in enumerate(working.turns) if turn is target), None)
        if index is None:
            continue
        turn = working.turns[index]
        encoded_bytes = len(turn.content_text.encode("utf-8"))
        low, high = 0, encoded_bytes
        best: RecallExpandedTurn | None = None
        while low <= high:
            midpoint = (low + high) // 2
            candidate = _trim_context_turn(turn, midpoint)
            working.turns[index] = candidate
            working.content_bytes_returned = sum(len(item.content_text.encode("utf-8")) for item in working.turns)
            if _context_response_size(working) <= RECALL_SERIALIZED_RESPONSE_BYTES:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best is not None and best.content_text:
            working.turns[index] = best
        elif not turn.is_match:
            working.turns.pop(index)
        else:
            working.turns[index] = _trim_context_turn(turn, max(0, high))
        working.content_bytes_returned = sum(len(item.content_text.encode("utf-8")) for item in working.turns)

    return RecallContextResponse.model_validate(working.model_dump())


def _recall_coverage_summary(coverage: RecallCoverage) -> RecallCoverageSummary:
    return RecallCoverageSummary(
        complete=coverage.complete,
        lagging_sessions=coverage.catalog_lag_count,
        unpublished_sessions=coverage.unpublished_sessions,
        oldest_lag_seconds=coverage.catalog_oldest_lag_seconds,
    )


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
    lexical_ranks = {match.session_id: rank for rank, match in enumerate(lexical, start=1)}
    semantic_ranks = {match.session_id: rank for rank, match in enumerate(semantic, start=1)}
    for sid in ordered_ids[:limit]:
        match = best_rank[sid][1]
        # Report the value the ordering was actually made from. The lanes score
        # on incomparable scales — lexical is 1/(1+|bm25|), semantic is cosine —
        # so passing the winner's raw lane score through made a fused list look
        # arbitrarily ordered to anyone reading the numbers.
        match.score = scores[sid]
        match.retrieval_lanes = [lane for lane, ranks in (("lexical", lexical_ranks), ("dense", semantic_ranks)) if sid in ranks]
        match.lane_ranks = {lane: ranks[sid] for lane, ranks in (("lexical", lexical_ranks), ("dense", semantic_ranks)) if sid in ranks}
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
    _auth: object = Depends(verify_agents_caller),
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
        sessions = await search_storage_v2_semantic_sessions(
            owner_id=_catalog_owner_id(_auth),
            query=query,
            project=project,
            provider=provider,
            environment=environment,
            days_back=days_back,
            limit=limit,
            include_test=include_test,
        )
    # Machine surface: identity and provenance, none of the browser's control
    # or presentation state. Ten full session payloads exceeded the MCP token
    # cap before any transcript content came back.
    result = MachineSessionsListResponse(
        sessions=[project_machine_session(session) for session in sessions],
        total=len(sessions),
        has_real_sessions=bool(sessions),
        lanes=["dense"],
    )
    timing.apply(response)
    return result


@router.get("/recall/context", response_model=RecallContextResponse, response_model_exclude_none=True)
async def recall_context(
    request: Request,
    ref: str = Query(..., description="Opaque result reference returned by recall"),
    before: int = Query(2, ge=0, le=5, description="Conversation turns before the match"),
    after: int = Query(2, ge=0, le=5, description="Conversation turns after the match"),
    max_content_bytes: int = Query(1_200, ge=200, le=RECALL_CONTEXT_MAX_TURN_BYTES),
    _auth: object = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> RecallContextResponse:
    """Open one recall card under a fixed total evidence budget."""

    unknown = sorted(set(request.query_params) - {"ref", "before", "after", "max_content_bytes"})
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_query_parameters", "parameters": unknown},
        )
    try:
        locator = _decode_recall_ref(ref)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_recall_ref", "message": str(exc)},
        ) from exc

    turn_count = before + after + 1
    applied_max = min(max_content_bytes, RECALL_CONTEXT_TOTAL_BYTES // turn_count)
    content_budget = applied_max * turn_count
    payload = await search_storage_v2_context(
        owner_id=_catalog_owner_id(_auth),
        session_id=locator.session_id,
        generation_id=locator.generation_id,
        search_event_id=locator.search_event_id,
        start_order_time_us=locator.start_order_time_us,
        before_turns=before,
        after_turns=after,
        max_content_bytes=applied_max,
        timeout_seconds=RECALL_ROUTE_TIMEOUT_SECONDS,
    )
    anchor_event_id = payload.anchor_event_id
    turns = [
        RecallExpandedTurn(
            role=turn.role,
            content_text=turn.content_text,
            is_match=turn.search_event_id == anchor_event_id,
            tool_name=turn.tool_name,
            content_text_truncated=turn.content_text_truncated,
            content_text_full_bytes=turn.content_text_full_bytes,
        )
        for turn in payload.context
    ]
    returned_bytes = sum(len(turn.content_text.encode("utf-8")) for turn in turns)
    candidate = RecallContextResponse.model_construct(
        ref=ref,
        session_id=locator.session_id,
        turns=turns,
        total_events=payload.total_events,
        content_byte_budget=content_budget,
        content_bytes_returned=returned_bytes,
        max_content_bytes_applied=applied_max,
        evidence_status=payload.evidence_status,
        evidence_reason=payload.evidence_reason,
    )
    return _fit_recall_context_response(candidate)


@router.get("/recall", response_model=RecallResponse, response_model_exclude_none=True)
async def recall_sessions(
    request: Request,
    response: Response = None,
    query: str = Query(..., description="What to search for"),
    project: Optional[str] = Query(None, description="Filter by project"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    include_test: bool = Query(False, description="Include test/e2e sessions"),
    since_days: int = Query(90, ge=1, le=365, description="Days to look back"),
    max_results: int = Query(5, ge=1, le=RECALL_SEARCH_RESULT_LIMIT, description="Max search-result cards"),
    include_automation: bool = Query(False, description="Include Hatch automation sessions in recall results"),
    mode: Literal["auto", "lexical", "semantic"] = "auto",
    _auth: object = Depends(verify_agents_caller),
    _single: None = Depends(require_single_tenant),
) -> RecallResponse:
    """Recall specific knowledge from past sessions."""
    handler_started = time.perf_counter()
    request_started = getattr(request.state, "request_timeout_started_at", None)
    timing = ServerTimingRecorder(surface="recall")

    allowed_query_params = {
        "query",
        "project",
        "provider",
        "include_test",
        "since_days",
        "max_results",
        "include_automation",
        "mode",
    }
    unknown_query_params = sorted(set(request.query_params) - allowed_query_params)
    if unknown_query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unknown_query_parameters",
                "message": "Recall request contains unsupported query parameters.",
                "parameters": unknown_query_params,
            },
        )

    def remaining_budget() -> float:
        started = request_started if isinstance(request_started, float) else handler_started
        return max(0.05, RECALL_ROUTE_TIMEOUT_SECONDS - (time.perf_counter() - started) - 0.1)

    owner_id = _catalog_owner_id(_auth)
    candidate_depth = min(200, max(max_results, max_results * CANDIDATE_DEPTH_FACTOR))

    # Reserved, not leftover. Hydration used to receive whatever discovery had
    # not already spent, which in practice was the 0.05s floor, so matches came
    # back "partial / search_evidence_unavailable" whenever a lane ran long.
    discovery_deadline = _discovery_budget(remaining_seconds=remaining_budget())

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

    async def dense() -> _DenseRecallResult:
        with timing.span("dense"):
            return await _semantic_recall(
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
    degraded: list[RecallLaneFailure] = []
    if mode == "lexical":
        matches = _rank_single_lane(await lexical(), limit=max_results, lane="lexical")
        lanes = ("lexical",)
        dense_result = None
    elif mode == "semantic":
        # The caller named exactly one lane. Failing it is the whole answer, so
        # this still raises rather than returning an empty success.
        dense_result = await dense()
        matches = _rank_single_lane(dense_result.matches, limit=max_results, lane="dense")
        lanes = ("dense",)
    else:
        # `auto` used to gather without `return_exceptions`, so a dense fault
        # propagated and threw away lexical results that had already been
        # computed. An agent asking a reasonable question got a 503 and a hint
        # to go use plain string search instead. A lane that cannot run costs
        # its own results and says so; it does not cost the request.
        lexical_outcome, dense_outcome = await asyncio.gather(lexical(), dense(), return_exceptions=True)
        lexical_matches = _lane_result(lexical_outcome, lane="lexical", degraded=degraded)
        dense_result = _lane_result(dense_outcome, lane="dense", degraded=degraded)
        if lexical_matches is None and dense_result is None:
            # Both lanes are down: there is no partial answer to report, so
            # surface the lexical fault rather than inventing a summary.
            raise lexical_outcome if isinstance(lexical_outcome, BaseException) else RuntimeError("recall produced no lanes")
        served: list[Literal["lexical", "dense"]] = []
        if lexical_matches is not None:
            served.append("lexical")
        if dense_result is not None:
            served.append("dense")
        matches = _rrf_merge_recall_matches(
            lexical_matches or [],
            dense_result.matches if dense_result is not None else [],
            limit=max_results,
        )
        lanes = tuple(served)

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
                    timeout_seconds=max(0.05, remaining_budget()),
                    max_content_bytes=RECALL_SEARCH_SNIPPET_BYTES,
                )
                for match in matches
            )
        )

    _finalize_recall_evidence(matches)
    results = _recall_search_results(matches)
    timing.apply(response)
    _apply_recall_diagnostic_headers(response, include_dense="dense" in lanes)
    if "dense" in lanes:
        assert dense_result is not None
        return _fit_recall_search_response(
            results=results,
            lanes=list(lanes),
            degraded=degraded,
            coverage=_recall_coverage_summary(dense_result.coverage),
        )
    return _fit_recall_search_response(
        results=results,
        lanes=list(lanes),
        degraded=degraded,
    )
