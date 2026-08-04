"""Tests for the dense/semantic lane wired into the live-catalog recall path.

Regression coverage for the gaps a Sol review found in commit 169bcb329. The
semantic lane must execute under tests through injected boundaries, and every
unavailable/timeout state must remain distinguishable from an honest miss.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import numpy as np
import pytest

from zerg.routers import agents_search
from zerg.services.session_views import RecallMatch

_REAL_COVERAGE_CHECK = agents_search._require_projection_coverage
_STORE_ID = "00000000-0000-4000-8000-000000000001"
_SCHEMA_GENERATION = "searchd-test-v1"


def _catalog_coverage(*, lag_count: int = 0, oldest_lag_seconds: float | None = None):
    return agents_search._ProjectorCoveragePayload(
        projector="test-projector",
        store_binding=agents_search._ProjectorStoreBindingPayload(
            store_id=_STORE_ID,
            schema_generation=_SCHEMA_GENERATION,
            commit_seq="1",
        ),
        lag_count=lag_count,
        indexed_through="9" if lag_count else "10",
        oldest_lag_at="2026-08-02T00:00:01+00:00" if lag_count else None,
        oldest_lag_seconds=oldest_lag_seconds if lag_count else None,
        commit_seq="10",
        observed_at="2026-08-02T00:00:02+00:00",
    )


@pytest.fixture(autouse=True)
def _complete_catalog_projection(monkeypatch):
    async def complete(*, timeout_seconds):
        assert timeout_seconds > 0
        return _catalog_coverage()

    monkeypatch.setattr(agents_search, "_require_projection_coverage", complete)


async def _noop_hydrate(match, **_kwargs):
    match.evidence_status = "not_requested"
    return match


async def _recall(*, mode: str):
    """Drive the route handler directly, past auth and the request-timeout state."""

    class _Request:
        query_params = {"query": "anything", "mode": mode}
        state = type("S", (), {})()

    class _Auth:
        owner_id = 42

    # Called directly, so every FastAPI Query default has to be supplied.
    return await agents_search.recall_sessions(
        _Request(),
        response=None,
        query="anything",
        project=None,
        provider=None,
        include_test=False,
        since_days=90,
        max_results=5,
        context_turns=0,
        context_mode="forensic",
        include_automation=False,
        mode=mode,
        _auth=_Auth(),
        _single=None,
    )


def _match(session_id: str, score: float) -> RecallMatch:
    return RecallMatch(session_id=session_id, chunk_index=0, score=score)


def _resident_coverage(*, stale: bool = False) -> agents_search._EmbeddingCoveragePayload:
    return agents_search._EmbeddingCoveragePayload(
        integrity_ready=True,
        complete=True,
        unpublished_sessions=0,
        expected_sessions=1,
        published_sessions=1,
        expected_episodes=1,
        current_episodes=1,
        invalid_vectors=0,
        unnormalized_vectors=0,
        unlocatable_episodes=0,
        episode_count_mismatches=0,
        missing_session_ids=[],
        stale=stale,
    )

@pytest.mark.asyncio
async def test_dense_rpc_response_rejects_malformed_rows_and_envelopes(monkeypatch):
    class FakeSearch:
        async def call(self, _method, _params, *, timeout_seconds):
            assert timeout_seconds == 1.0
            return {
                "results": [
                    {
                        "session_id": str(uuid4()),
                        "episode_ordinal": 0,
                        "score": float("nan"),
                        "event_index_start": 0,
                        "event_index_end": 1,
                        "generation_id": str(uuid4()),
                        "start_order_time_us": 1,
                    }
                ],
                "unexpected": True,
            }

    monkeypatch.setattr(agents_search, "get_searchd_client", lambda: FakeSearch())
    with pytest.raises(ValueError):
        await agents_search.search_storage_v2_episode_embeddings(
            model="test-model",
            owner_id=42,
            dims=2,
            query_embedding=np.array([1.0, 0.0], dtype=np.float32).tobytes(),
            limit=5,
            timeout_seconds=1.0,
        )

@pytest.mark.asyncio
async def test_current_embedding_projection_opens_the_outer_coverage_gate(monkeypatch):
    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, method, params, *, timeout_seconds):
            assert method == "projector.coverage.read.v2"
            assert timeout_seconds == 1.0
            return {
                "projector": params["projector"],
                "store_binding": {
                    "store_id": _STORE_ID,
                    "schema_generation": _SCHEMA_GENERATION,
                    "commit_seq": "1",
                },
                "lag_count": 0,
                "indexed_through": "10",
                "oldest_lag_at": None,
                "oldest_lag_seconds": None,
                "commit_seq": "10",
                "observed_at": "2026-08-02T00:00:00+00:00",
            }

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(
        agents_search,
        "_require_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )

    await agents_search._require_projection_coverage(timeout_seconds=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lag_count", "oldest_lag_seconds"),
    [
        (500, 1.0),
        (1, 86_400.0),
    ],
)
async def test_projection_lag_serves_instead_of_refusing(
    monkeypatch,
    lag_count,
    oldest_lag_seconds,
):
    """Lag is freshness, not corruption, so it must not close the gate.

    A single unfinished session used to make every semantic query in the tenant
    return a typed "incomplete corpus" 503, including queries over the tens of
    thousands of sessions that were long since immutable and embedded. Neither a
    large head nor an old one is a reason to refuse; both are reported.
    """

    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, _method, params, *, timeout_seconds):
            assert timeout_seconds == 1.0
            payload = _catalog_coverage(
                lag_count=lag_count,
                oldest_lag_seconds=oldest_lag_seconds,
            ).model_dump()
            payload["projector"] = params["projector"]
            return payload

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(
        agents_search,
        "_require_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )

    coverage = await agents_search._require_projection_coverage(timeout_seconds=1.0)

    assert coverage.lag_count == lag_count
    assert coverage.oldest_lag_seconds == oldest_lag_seconds
    # The watermark is what makes serving-under-lag honest rather than silent.
    assert coverage.indexed_through == "9"


@pytest.mark.asyncio
async def test_malformed_projector_coverage_is_typed_unavailable(monkeypatch):
    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, _method, _params, *, timeout_seconds):
            assert timeout_seconds == 1.0
            return {"lag_count": "not-an-integer"}

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(
        agents_search,
        "_require_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )

    with pytest.raises(agents_search.HTTPException) as unavailable:
        await agents_search._require_projection_coverage(timeout_seconds=1.0)

    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["code"] == "coverage_status_unavailable"
    assert unavailable.value.detail["reason"] == "invalid_catalog_response"


@pytest.mark.asyncio
async def test_semantic_recall_never_turns_missing_test_model_into_a_miss():
    """TESTING is not permission to make an unavailable lane look empty."""

    with pytest.raises(agents_search.HTTPException) as unavailable:
        await agents_search._semantic_recall_matches(
            query="anything",
            project=None,
            provider=None,
            since_days=90,
            include_test=False,
            include_automation=False,
            max_results=5,
            timeout_seconds=5.0,
            owner_id=42,
        )
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["code"] == "embedder_unavailable"


@pytest.mark.asyncio
async def test_semantic_recall_matches_times_out_gracefully(monkeypatch):
    """A slow embedding call must degrade within the caller's remaining budget, not hang."""
    monkeypatch.setenv("TESTING", "0")

    fake_config = type("Cfg", (), {"model": "test-model", "dims": 4})()
    import zerg.models_config as models_config_module

    monkeypatch.setattr(models_config_module, "get_embedding_space_config", lambda: fake_config)

    async def slow_generate_embedding(_text):
        await asyncio.sleep(5)
        return np.zeros(4, dtype=np.float32)

    import zerg.services.local_embedder as local_embedder_module

    monkeypatch.setattr(local_embedder_module, "embed_query", slow_generate_embedding)

    with pytest.raises(agents_search.HTTPException) as timed_out:
        await agents_search._semantic_recall_matches(
            query="slow query",
            project=None,
            provider=None,
            since_days=90,
            include_test=False,
            include_automation=False,
            max_results=5,
            timeout_seconds=0.05,
            owner_id=42,
        )
    assert timed_out.value.status_code == 503
    assert timed_out.value.detail["code"] == "dense_timed_out"


@pytest.mark.asyncio
async def test_semantic_recall_carries_live_rpc_coverage_watermark(monkeypatch):
    """Scoping is a SQL predicate (owner/project/provider/environment/recency)
    against searchd's session_index, not an enumerated session id list.

    A dense-only match -- one lexical search for this exact query text would
    never surface, which is the entire point of having a semantic lane --
    must still reach search.embedding.query.v2 with real scoping filters, not
    an empty or capped session_filter. An earlier version of this code
    enumerated the owner's visible sessions client-side and passed ids
    directly; that capped out well before covering a real tenant's full
    history, which is exactly the regression this test guards against.
    """
    monkeypatch.setenv("TESTING", "0")
    fake_config = type("Cfg", (), {"model": "test-model", "dims": 2})()
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: fake_config)

    async def fake_generate_embedding(_text):
        return np.array([1, 0], dtype=np.float32)

    candidate_session_id = str(uuid4())
    seen = {}

    async def fake_query(**kwargs):
        seen.update(kwargs)
        return agents_search._DenseQueryPayload(
            results=[
                {
                    "session_id": candidate_session_id,
                    "episode_ordinal": 3,
                    "score": 0.9,
                    "event_index_start": 4,
                    "event_index_end": 5,
                    "generation_id": str(uuid4()),
                    "start_order_time_us": 123,
                }
            ],
            coverage=_resident_coverage(),
            store_id=_STORE_ID,
            schema_generation=_SCHEMA_GENERATION,
        )

    import zerg.services.local_embedder as local_embedder_module

    monkeypatch.setattr(local_embedder_module, "embed_query", fake_generate_embedding)
    monkeypatch.setattr(agents_search, "search_storage_v2_episode_embeddings", fake_query)
    result = await agents_search._semantic_recall(
        query="important answer",
        project=None,
        provider=None,
        since_days=90,
        include_test=False,
        include_automation=False,
        max_results=5,
        timeout_seconds=5.0,
        owner_id=42,
    )
    assert [match.chunk_index for match in result.matches] == [3]
    assert result.coverage.complete is True
    assert result.coverage.complete_through_commit_seq == "10"
    assert result.coverage.unpublished_sessions == 0
    assert result.coverage.catalog_commit_seq == "10"
    assert result.coverage.search_store_id == _STORE_ID
    assert result.coverage.search_schema_generation == _SCHEMA_GENERATION
    assert result.coverage.resident_stale is False
    assert result.coverage.expected_episodes == 1
    assert seen["model"] == "test-model"
    assert seen["owner_id"] == 42
    assert seen["environment"] is None
    assert seen["exclude_environments"] == ["test", "e2e", "automation"]
    assert seen["since_iso"] is not None


@pytest.mark.asyncio
async def test_semantic_recall_rejects_resident_from_an_uncertified_store(monkeypatch):
    """An old catalog certificate cannot bless a newly replaced empty store."""

    fake_config = type("Cfg", (), {"model": "test-model", "dims": 2})()
    monkeypatch.setattr("zerg.models_config.get_embedding_space_config", lambda: fake_config)

    async def fake_generate_embedding(_text):
        return np.array([1, 0], dtype=np.float32)

    async def fake_query(**_kwargs):
        return agents_search._DenseQueryPayload(
            results=[],
            coverage=_resident_coverage(),
            store_id=str(uuid4()),
            schema_generation=_SCHEMA_GENERATION,
        )

    import zerg.services.local_embedder as local_embedder_module

    monkeypatch.setattr(local_embedder_module, "embed_query", fake_generate_embedding)
    monkeypatch.setattr(agents_search, "search_storage_v2_episode_embeddings", fake_query)

    with pytest.raises(agents_search.HTTPException) as incomplete:
        await agents_search._semantic_recall(
            query="important answer",
            project=None,
            provider=None,
            since_days=90,
            include_test=False,
            include_automation=False,
            max_results=5,
            timeout_seconds=5.0,
            owner_id=42,
        )
    assert incomplete.value.status_code == 503
    assert incomplete.value.detail["code"] == "embedding_coverage_incomplete"
    assert incomplete.value.detail["reason"] == "store_binding_mismatch"


@pytest.mark.asyncio
async def test_auto_mode_serves_lexical_when_the_dense_lane_is_down(monkeypatch):
    """One lane failing costs its own results, never the whole request.

    `auto` used to gather both lanes without `return_exceptions`, so a dense
    fault propagated out of the route and threw away lexical results that had
    already been computed. Agents got a 503 and a hint to fall back to plain
    string search, which is exactly what they did.
    """

    lexical_hit = _match(str(uuid4()), 0.8)

    async def lexical(**_kwargs):
        return [lexical_hit]

    async def dense(**_kwargs):
        raise agents_search.HTTPException(
            status_code=503,
            detail={"code": "embedder_unavailable", "message": "The local embedding model is not loaded."},
        )

    monkeypatch.setattr(agents_search, "_lexical_recall_matches", lexical)
    monkeypatch.setattr(agents_search, "_semantic_recall", dense)
    monkeypatch.setattr(agents_search, "_hydrate_recall_match", _noop_hydrate)

    response = await _recall(mode="auto")

    assert [match.session_id for match in response.matches] == [lexical_hit.session_id]
    assert response.lanes == ["lexical"]
    # The caller must be able to tell a narrower answer from a complete one.
    assert [failure.lane for failure in response.degraded] == ["dense"]
    assert response.degraded[0].code == "embedder_unavailable"
    assert response.coverage is None


@pytest.mark.asyncio
async def test_semantic_mode_still_fails_when_its_only_lane_is_down(monkeypatch):
    """A caller who named one lane gets that lane's fault, not an empty success."""

    async def dense(**_kwargs):
        raise agents_search.HTTPException(
            status_code=503,
            detail={"code": "embedder_unavailable", "message": "The local embedding model is not loaded."},
        )

    monkeypatch.setattr(agents_search, "_semantic_recall", dense)
    monkeypatch.setattr(agents_search, "_hydrate_recall_match", _noop_hydrate)

    with pytest.raises(agents_search.HTTPException) as failure:
        await _recall(mode="semantic")
    assert failure.value.status_code == 503


@pytest.mark.asyncio
async def test_a_barely_projected_generation_serves_and_says_so(monkeypatch):
    """A fresh projector identity must serve, not refuse, so a re-embed is possible.

    This used to require a durable cutover certificate proving the identity had
    once reached zero backlog. On a live instance lag is never zero, so a new
    identity could never certify and re-projecting the corpus was impossible --
    bumping the identity to re-embed put every dense query into
    `cutover_not_certified` within seconds. A store that has projected almost
    nothing now serves almost nothing and reports exactly that.
    """

    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, _method, params, *, timeout_seconds):
            payload = _catalog_coverage(lag_count=24_000, oldest_lag_seconds=99_999.0).model_dump()
            payload["projector"] = params["projector"]
            return payload

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(agents_search, "_require_projection_coverage", _REAL_COVERAGE_CHECK)

    coverage = await agents_search._require_projection_coverage(timeout_seconds=1.0)

    assert coverage.lag_count == 24_000
    assert coverage.store_binding is not None
    # Degraded, but never silent: the caller gets the watermark and the backlog.
    assert coverage.indexed_through == "9"


@pytest.mark.asyncio
async def test_missing_store_binding_still_closes_the_gate(monkeypatch):
    """Without a bound store there is nothing to attribute results to."""

    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, _method, params, *, timeout_seconds):
            payload = _catalog_coverage().model_dump()
            payload["projector"] = params["projector"]
            payload["store_binding"] = None
            return payload

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    monkeypatch.setattr(agents_search, "_require_projection_coverage", _REAL_COVERAGE_CHECK)

    with pytest.raises(agents_search.HTTPException) as unproven:
        await agents_search._require_projection_coverage(timeout_seconds=1.0)
    assert unproven.value.status_code == 503
    assert unproven.value.detail["reason"] == "store_binding_missing"


@pytest.mark.asyncio
async def test_semantic_session_listing_survives_one_unreadable_candidate(monkeypatch):
    """A catalog projection failure on one candidate must not fail the listing.

    Each candidate projection hits catalogd, which is a single writer that a
    corpus re-projection can saturate. Without this, a transient RPC timeout on
    any one session turned the whole route into a bare 500 that told the caller
    nothing about which part failed.
    """

    from dataclasses import dataclass
    from dataclasses import replace as dataclass_replace

    from zerg.catalogd.client import CatalogUnavailable

    @dataclass(frozen=True)
    class _Session:
        """Only the fields the projection filter reads."""

        id: str
        environment: str = "local"
        user_messages: int = 3
        user_hidden_from_timeline: bool = False
        is_sidechain: bool = False

        def model_copy(self, *, update):
            return dataclass_replace(self, **{k: v for k, v in update.items() if k != "match_score"})

    good_id, bad_id = str(uuid4()), str(uuid4())

    async def matches(**_kwargs):
        return [_match(bad_id, 0.9), _match(good_id, 0.8)]

    def read(session_id, *, owner_id):
        if str(session_id) == bad_id:
            raise CatalogUnavailable("catalogd unavailable for session.shadow_state.read.v2")
        return (_Session(id=good_id), None, "1")

    monkeypatch.setattr(agents_search, "_semantic_recall_matches", matches)
    monkeypatch.setattr(agents_search, "read_live_catalog_session", read)

    sessions = await agents_search.search_storage_v2_semantic_sessions(
        owner_id=42,
        query="anything",
        project=None,
        provider=None,
        environment=None,
        days_back=90,
        limit=5,
        include_test=False,
    )

    assert [session.id for session in sessions] == [good_id]


@pytest.mark.asyncio
async def test_semantic_session_listing_fails_when_every_candidate_is_unreadable(monkeypatch):
    """An empty list would claim the corpus holds nothing relevant. It doesn't."""

    from zerg.catalogd.client import CatalogUnavailable

    async def matches(**_kwargs):
        return [_match(str(uuid4()), 0.9)]

    def read(_session_id, *, owner_id):
        raise CatalogUnavailable("catalogd unavailable for session.shadow_state.read.v2")

    monkeypatch.setattr(agents_search, "_semantic_recall_matches", matches)
    monkeypatch.setattr(agents_search, "read_live_catalog_session", read)

    with pytest.raises(agents_search.HTTPException) as unavailable:
        await agents_search.search_storage_v2_semantic_sessions(
            owner_id=42,
            query="anything",
            project=None,
            provider=None,
            environment=None,
            days_back=90,
            limit=5,
            include_test=False,
        )
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["code"] == "session_projection_unavailable"
