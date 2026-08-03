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


@pytest.fixture(autouse=True)
def _complete_catalog_projection(monkeypatch):
    async def complete(*, timeout_seconds):
        assert timeout_seconds > 0

    monkeypatch.setattr(agents_search, "_require_complete_projection_coverage", complete)


def _match(session_id: str, score: float) -> RecallMatch:
    return RecallMatch(session_id=session_id, chunk_index=0, score=score)


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
            assert method == "projector.state.list_lag.v2"
            assert timeout_seconds == 1.0
            seen.append(params["projector"])
            return {
                "states": [],
                "lag_count": 1,
                "indexed_through": "9",
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
    assert seen == [EMBEDDING_PROJECTOR_ID]


@pytest.mark.asyncio
async def test_current_embedding_projection_opens_the_outer_coverage_gate(monkeypatch):
    from zerg.services import catalogd_supervisor

    class FakeCatalog:
        async def call(self, _method, _params, *, timeout_seconds):
            assert timeout_seconds == 1.0
            return {
                "states": [],
                "lag_count": 0,
                "indexed_through": "10",
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
async def test_semantic_recall_matches_uses_live_catalog_embedding_rpc(monkeypatch):
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
        return [
            {
                "session_id": candidate_session_id,
                "episode_ordinal": 3,
                "score": 0.9,
                "event_index_start": 4,
                "event_index_end": 5,
                "generation_id": str(uuid4()),
                "start_order_time_us": 123,
            }
        ]

    import zerg.services.local_embedder as local_embedder_module

    monkeypatch.setattr(local_embedder_module, "embed_query", fake_generate_embedding)
    monkeypatch.setattr(agents_search, "search_storage_v2_episode_embeddings", fake_query)
    result = await agents_search._semantic_recall_matches(
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
    assert [match.chunk_index for match in result] == [3]
    assert seen["model"] == "test-model"
    assert seen["owner_id"] == 42
    assert seen["environment"] is None
    assert seen["exclude_environments"] == ["test", "e2e", "automation"]
    assert seen["since_iso"] is not None
