"""Tests for the dense/semantic lane wired into the live-catalog recall path.

Regression coverage for the gaps a Sol review found in commit 169bcb329: the
new code path was previously untested end to end because TESTING=1 makes
``_semantic_recall_matches`` short-circuit before ever reaching the embedding
call, the DB factory access, or the RRF merge -- so a crash in any of those
would have shipped invisibly. These tests exercise the pure fusion/snippet
logic directly and force the semantic path past its TESTING guard to prove
it degrades to an empty list instead of raising when the DB factory is
unavailable, which is exactly what live-catalog mode does not guarantee.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.routers import agents_search
from zerg.services.session_views import RecallMatch


def _make_db(tmp_path):
    db_path = tmp_path / "test_semantic_recall.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


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
async def test_semantic_recall_matches_short_circuits_under_testing_env():
    """The documented TESTING=1 fast path never touches the DB factory or network."""
    assert os.getenv("TESTING") == "1"

    result = await agents_search._semantic_recall_matches(
        query="anything",
        project=None,
        provider=None,
        since_days=90,
        include_test=False,
        include_automation=False,
        max_results=5,
        timeout_seconds=5.0,
    )
    assert result == []


@pytest.mark.asyncio
async def test_semantic_recall_matches_degrades_when_session_factory_unavailable(monkeypatch):
    """Live-catalog mode does not guarantee get_session_factory() works; this must not raise.

    This is the exact crash Sol's review flagged: get_session_factory() was
    previously called outside the function's try/except, so an unavailable
    factory in live-catalog mode would propagate as a 500 instead of
    degrading to lexical-only.
    """
    monkeypatch.setenv("TESTING", "0")

    fake_config = type("Cfg", (), {"model": "test-model", "dims": 4})()

    import zerg.models_config as models_config_module

    monkeypatch.setattr(models_config_module, "get_embedding_config", lambda: fake_config)

    async def fake_generate_embedding(_text, _config):
        return np.zeros(4, dtype=np.float32)

    import zerg.services.session_processing.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "generate_embedding", fake_generate_embedding)

    def _raise_unavailable():
        raise RuntimeError("get_session_factory unavailable in live-catalog mode")

    monkeypatch.setattr(agents_search.database_module, "get_session_factory", _raise_unavailable)

    result = await agents_search._semantic_recall_matches(
        query="does this crash",
        project=None,
        provider=None,
        since_days=90,
        include_test=False,
        include_automation=False,
        max_results=5,
        timeout_seconds=5.0,
    )
    assert result == []


@pytest.mark.asyncio
async def test_semantic_recall_matches_times_out_gracefully(monkeypatch):
    """A slow embedding call must degrade within the caller's remaining budget, not hang."""
    monkeypatch.setenv("TESTING", "0")

    fake_config = type("Cfg", (), {"model": "test-model", "dims": 4})()
    import zerg.models_config as models_config_module

    monkeypatch.setattr(models_config_module, "get_embedding_config", lambda: fake_config)

    async def slow_generate_embedding(_text, _config):
        await asyncio.sleep(5)
        return np.zeros(4, dtype=np.float32)

    import zerg.services.session_processing.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "generate_embedding", slow_generate_embedding)

    result = await agents_search._semantic_recall_matches(
        query="slow query",
        project=None,
        provider=None,
        since_days=90,
        include_test=False,
        include_automation=False,
        max_results=5,
        timeout_seconds=0.05,
    )
    assert result == []


@pytest.mark.asyncio
async def test_semantic_recall_matches_uses_live_catalog_embedding_rpc(monkeypatch):
    """Candidate sessions come from the owner's full visible listing, not from
    lexically matching the same query text.

    A dense-only match -- one lexical search for this exact query text would
    never surface, which is the entire point of having a semantic lane --
    must still reach search.embedding.query.v2. If candidate scope were
    derived from a lexical search on ``query``, this test's candidate session
    would never appear in that lexical result set and the RPC would receive
    an empty session_filter.
    """
    monkeypatch.setenv("TESTING", "0")
    fake_config = type("Cfg", (), {"model": "test-model", "dims": 2})()
    monkeypatch.setattr("zerg.models_config.get_embedding_config", lambda: fake_config)
    monkeypatch.setattr(agents_search.database_module, "live_catalog_enabled", lambda: True)

    async def fake_generate_embedding(_text, _config):
        return np.array([1, 0], dtype=np.float32)

    candidate_session_id = str(uuid4())

    def fake_list_live_catalog_sessions(*, params, owner_id):
        # Real signature is synchronous -- it's dispatched via asyncio.to_thread,
        # not awaited directly. An async mock here silently no-ops (the coroutine
        # is returned unawaited), which is why this test needs the real (sync)
        # calling convention exercised, not just assumed.
        assert params.query is None, "candidate listing must not apply a text-relevance filter"
        assert owner_id == 42
        fake_session = type("FakeSession", (), {"id": candidate_session_id})()
        return type("Listing", (), {"sessions": [fake_session]})()

    seen = {}

    async def fake_query(**kwargs):
        seen.update(kwargs)
        return [
            {
                "session_id": kwargs["session_filter"][0],
                "episode_ordinal": 3,
                "score": 0.9,
                "event_index_start": 4,
                "event_index_end": 5,
            }
        ]

    import zerg.services.session_processing.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "generate_embedding", fake_generate_embedding)
    monkeypatch.setattr("zerg.services.live_catalog_timeline.list_live_catalog_sessions", fake_list_live_catalog_sessions)
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
    assert seen["session_filter"] == [candidate_session_id]


def test_fetch_episode_snippet_uses_clean_projection_index_space(tmp_path):
    """event_start/end index the clean (content-bearing) projection, not raw durable rows.

    A tool-output-only row sits between two content-bearing events at raw
    positions 0 and 2, but clean-projection positions 0 and 1. Requesting
    the episode at clean indices [0, 1] must return the second *content*
    event, not whatever raw row happens to sit at a naive offset.
    """
    SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="please run the tests",
                timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="tool",
                tool_name="bash",
                tool_output_text="....... 40 passed",
                timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="All forty tests pass, the fix is confirmed working end to end.",
                timestamp=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            )
        )
        db.commit()

        snippet = agents_search._fetch_episode_snippet(db, session_id, event_start=0, event_end=1)

    assert "forty tests pass" in snippet
