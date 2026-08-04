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

_REAL_COVERAGE_CHECK = agents_search._require_complete_projection_coverage
_STORE_ID = "00000000-0000-4000-8000-000000000001"
_SCHEMA_GENERATION = "searchd-test-v1"


def _catalog_coverage(*, lag_count: int = 0, oldest_lag_seconds: float | None = None):
    return agents_search._ProjectorCoveragePayload(
        projector="test-projector",
        certificate=agents_search._CutoverCertificatePayload(
            certified_commit_seq="9",
            certified_at="2026-08-02T00:00:00+00:00",
        ),
        store_binding=agents_search._ProjectorStoreBindingPayload(
            store_id=_STORE_ID,
            schema_generation=_SCHEMA_GENERATION,
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

    monkeypatch.setattr(agents_search, "_require_complete_projection_coverage", complete)


def _match(session_id: str, score: float) -> RecallMatch:
    return RecallMatch(session_id=session_id, chunk_index=0, score=score)


def _resident_coverage(*, stale: bool = False) -> agents_search._EmbeddingCoveragePayload:
    return agents_search._EmbeddingCoveragePayload(
        ready=True,
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


def test_rrf_merge_credits_both_lanes_for_agreeing_session():
    """A session found by both lanes should outscore one found by only one lane."""
    shared = str(uuid4())
    lexical_only = str(uuid4())

    lexical = [_match(shared, 0.9), _match(lexical_only, 0.5)]
    semantic = [_match(shared, 0.8)]

    merged = agents_search._rrf_merge_recall_matches(lexical, semantic, limit=10)
    ordered_ids = [m.session_id for m in merged]

    assert ordered_ids[0] == shared
    assert lexical_only in ordered_ids


def test_rrf_merge_prefers_evidence_from_the_better_ranked_lane():
    """When both lanes return a session, use whichever lane ranked it higher for evidence."""
    shared = str(uuid4())

    lexical_match = RecallMatch(session_id=shared, chunk_index=0, score=0.1, evidence="weak lexical snippet")
    semantic_match = RecallMatch(session_id=shared, chunk_index=0, score=0.9, evidence="strong semantic snippet")

    # Semantic ranks it #0 (best), lexical ranks it #3 (weak) -- semantic evidence should win.
    lexical = [_match(str(uuid4()), 0.0), _match(str(uuid4()), 0.0), _match(str(uuid4()), 0.0), lexical_match]
    semantic = [semantic_match]

    merged = agents_search._rrf_merge_recall_matches(lexical, semantic, limit=10)
    winner = next(m for m in merged if m.session_id == shared)

    assert winner.evidence == "strong semantic snippet"


def test_rrf_merge_respects_limit():
    lexical = [_match(str(uuid4()), 1.0) for _ in range(5)]
    merged = agents_search._rrf_merge_recall_matches(lexical, [], limit=2)
    assert len(merged) == 2


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
async def test_current_embedding_projection_lag_closes_the_outer_coverage_gate(monkeypatch):
    from zerg.embedding_space import EMBEDDING_PROJECTOR_ID
    from zerg.services import catalogd_supervisor

    seen = []

    class FakeCatalog:
        async def call(self, method, params, *, timeout_seconds):
            assert method == "projector.coverage.read.v2"
            assert timeout_seconds == 1.0
            seen.append(params["projector"])
            return {
                "projector": params["projector"],
                "certificate": None,
                "store_binding": {
                    "store_id": _STORE_ID,
                    "schema_generation": _SCHEMA_GENERATION,
                },
                "lag_count": 1,
                "indexed_through": "9",
                "oldest_lag_at": "2026-08-02T00:00:00+00:00",
                "oldest_lag_seconds": 10.0,
                "commit_seq": "10",
                "observed_at": "2026-08-02T00:00:00+00:00",
            }

    monkeypatch.setattr(catalogd_supervisor, "get_catalogd_client", lambda: FakeCatalog())
    # Override the file's autouse success stub with the real boundary.
    monkeypatch.setattr(
        agents_search,
        "_require_complete_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )
    with pytest.raises(agents_search.HTTPException) as incomplete:
        await agents_search._require_complete_projection_coverage(timeout_seconds=1.0)

    assert incomplete.value.status_code == 503
    assert incomplete.value.detail["code"] == "embedding_coverage_incomplete"
    assert incomplete.value.detail["reason"] == "cutover_not_certified"
    assert seen == [EMBEDDING_PROJECTOR_ID]


@pytest.mark.asyncio
async def test_current_embedding_projection_opens_the_outer_coverage_gate(monkeypatch):
    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, method, params, *, timeout_seconds):
            assert method == "projector.coverage.read.v2"
            assert timeout_seconds == 1.0
            return {
                "projector": params["projector"],
                "certificate": {
                    "certified_commit_seq": "9",
                    "certified_at": "2026-08-02T00:00:00+00:00",
                },
                "store_binding": {
                    "store_id": _STORE_ID,
                    "schema_generation": _SCHEMA_GENERATION,
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
        "_require_complete_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )

    await agents_search._require_complete_projection_coverage(timeout_seconds=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lag_count", "oldest_lag_seconds"),
    [
        (agents_search.RECALL_LIVE_HEAD_MAX_SESSIONS + 1, 1.0),
        (1, agents_search.RECALL_LIVE_HEAD_MAX_AGE_SECONDS + 0.1),
    ],
)
async def test_live_head_must_stay_inside_explicit_bounds(
    monkeypatch,
    lag_count,
    oldest_lag_seconds,
):
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
        "_require_complete_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )
    with pytest.raises(agents_search.HTTPException) as incomplete:
        await agents_search._require_complete_projection_coverage(timeout_seconds=1.0)
    assert incomplete.value.detail["reason"] == "live_head_exceeds_bounds"


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
        "_require_complete_projection_coverage",
        _REAL_COVERAGE_CHECK,
    )

    with pytest.raises(agents_search.HTTPException) as unavailable:
        await agents_search._require_complete_projection_coverage(timeout_seconds=1.0)

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
async def test_semantic_recall_carries_live_rpc_coverage_certificate(monkeypatch):
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
    assert result.coverage.ready is True
    assert result.coverage.catalog_commit_seq == "10"
    assert result.coverage.cutover_certified_commit_seq == "9"
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
