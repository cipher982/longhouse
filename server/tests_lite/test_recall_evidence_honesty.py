"""Recall must never claim evidence it does not have.

Why this exists: a live recall returned its three highest-scoring matches with
``evidence: null``, ``context: []`` and ``total_events: 0``, each labelled
``evidence_status: "complete"``. Nothing had gone wrong at request time — the
semantic lane appended its matches after the hydrator had already run, so those
matches simply kept the model default, which was "complete". A caller reading
the top results had no way to tell they were empty.

Two invariants: a status is asserted by whoever hydrated, never inherited; and a
semantic episode carrying a locator hydrates like a lexical hit does.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers import agents_search
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse


def _timing() -> dict[str, float | int]:
    return {"admit_ms": 0.0, "sql_ms": 0.1, "active_readers": 1, "queued_readers": 0}


def _coverage() -> dict[str, object]:
    return {
        "complete": True,
        "complete_through_commit_seq": "10",
        "unpublished_sessions": 0,
        "projector": "embeddings-test-256d-p3",
        "cutover_certified_commit_seq": "9",
        "cutover_certified_at": "2026-08-02T00:00:00+00:00",
        "search_store_id": "00000000-0000-4000-8000-000000000001",
        "search_schema_generation": "searchd-test-v1",
        "catalog_lag_count": 0,
        "catalog_indexed_through": "10",
        "catalog_oldest_lag_at": None,
        "catalog_oldest_lag_seconds": None,
        "catalog_commit_seq": "10",
        "catalog_observed_at": "2026-08-02T00:00:00+00:00",
        "resident_stale": False,
        "expected_sessions": 1,
        "published_sessions": 1,
        "expected_episodes": 1,
        "current_episodes": 1,
        "invalid_vectors": 0,
        "unnormalized_vectors": 0,
        "unlocatable_episodes": 0,
        "episode_count_mismatches": 0,
        "missing_session_ids": [],
    }


def _context_row(content: str, *, order_time_us: int = 100, role: str = "assistant") -> dict[str, object]:
    return {
        "search_event_id": order_time_us,
        "event_id": f"event-{order_time_us}",
        "source_object_id": "a" * 64,
        "record_ordinal": order_time_us,
        "order_time_us": order_time_us,
        "role": role,
        "content_text": content,
        "tool_name": None,
    }


def test_a_bare_match_is_unavailable_not_complete():
    """The default is the whole bug. An unhydrated match must not claim completeness."""
    match = RecallMatch(session_id=str(uuid4()), chunk_index=0, score=0.62)

    assert match.evidence_status == "unavailable"


def test_internal_locator_never_reaches_the_wire():
    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.5,
        start_order_time_us=1785605093280000,
    )

    assert match.start_order_time_us == 1785605093280000
    assert "start_order_time_us" not in match.model_dump()


def test_finalizer_normalizes_evidence_states_into_one_truthful_algebra():
    complete_without_context = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.5,
        evidence="matching snippet",
        evidence_status="complete",
    )
    unavailable_with_material = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.4,
        evidence="matching snippet",
        evidence_status="unavailable",
        evidence_reason="context_unavailable",
    )
    partial_without_material = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.3,
        evidence_status="partial",
        evidence_reason="context_timed_out",
    )
    complete_without_material = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.2,
        evidence_status="complete",
    )
    complete_with_stale_reason = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.1,
        evidence="matching snippet",
        context=[{"role": "assistant", "content_text": "matching snippet"}],
        evidence_status="complete",
        evidence_reason="stale_store_reason",
    )

    agents_search._finalize_recall_evidence(
        [
            complete_without_context,
            unavailable_with_material,
            partial_without_material,
            complete_without_material,
            complete_with_stale_reason,
        ]
    )

    assert (complete_without_context.evidence_status, complete_without_context.evidence_reason) == (
        "partial",
        "complete_without_materialized_evidence",
    )
    assert unavailable_with_material.evidence_status == "partial"
    assert partial_without_material.evidence_status == "unavailable"
    assert complete_without_material.evidence_status == "unavailable"
    assert complete_with_stale_reason.evidence_reason is None


def test_recall_response_enforces_lane_space_and_evidence_consistency():
    valid_match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.7,
        evidence="the answer",
        context=[{"role": "assistant", "content_text": "the answer"}],
        evidence_status="complete",
        retrieval_lanes=["dense"],
        lane_ranks={"dense": 1},
    )
    response = RecallResponse(
        matches=[valid_match],
        total=1,
        lanes=["dense"],
        embedding_model="google/embeddinggemma-300m",
        embedding_dims=256,
        embedding_revision="a" * 40,
        coverage=_coverage(),
    )
    assert response.total == 1

    with pytest.raises(ValueError, match="embedding-space identity"):
        RecallResponse(matches=[], total=0, lanes=["dense"])
    with pytest.raises(ValueError, match="lane attribution"):
        RecallResponse(
            matches=[valid_match.model_copy(update={"lane_ranks": {"lexical": 1}})],
            total=1,
            lanes=["dense"],
            embedding_model="google/embeddinggemma-300m",
            embedding_dims=256,
            embedding_revision="a" * 40,
            coverage=_coverage(),
        )
    with pytest.raises(ValueError, match="complete recall evidence"):
        RecallResponse(
            matches=[valid_match.model_copy(update={"context": []})],
            total=1,
            lanes=["dense"],
            embedding_model="google/embeddinggemma-300m",
            embedding_dims=256,
            embedding_revision="a" * 40,
            coverage=_coverage(),
        )
    stale_coverage = {**_coverage(), "catalog_indexed_through": "9"}
    with pytest.raises(ValueError, match="current catalog watermark"):
        RecallResponse(
            matches=[valid_match],
            total=1,
            lanes=["dense"],
            embedding_model="google/embeddinggemma-300m",
            embedding_dims=256,
            embedding_revision="a" * 40,
            coverage=stale_coverage,
        )
    with pytest.raises(ValueError, match="lexical-only recall must not claim dense corpus coverage"):
        RecallResponse(matches=[], total=0, lanes=["lexical"], coverage=_coverage())


@pytest.mark.asyncio
async def test_semantic_match_with_a_locator_hydrates(monkeypatch):
    """The episode lane knows where it starts; that has to be enough to fetch evidence."""
    session_id = str(uuid4())
    generation_id = str(uuid4())
    seen: dict = {}

    async def fake_context(**kwargs):
        seen.update(kwargs)
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [_context_row("the migration applied cleanly")],
            "total_events": 590,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(
        session_id=session_id,
        chunk_index=3,
        score=0.62,
        generation_id=generation_id,
        start_order_time_us=1785605093280000,
    )
    await agents_search._hydrate_recall_match(
        match,
        owner_id=42,
        context_turns=2,
        timeout_seconds=5.0,
    )

    assert seen["start_order_time_us"] == 1785605093280000
    assert seen["search_event_id"] is None
    assert match.evidence_status == "complete"
    assert match.total_events == 590
    assert match.context[0]["content_text"] == "the migration applied cleanly"


@pytest.mark.asyncio
async def test_lexical_match_still_locates_by_event_id(monkeypatch):
    """A match holding both locators must use the event id, not the position."""
    seen: dict = {}

    async def fake_context(**kwargs):
        seen.update(kwargs)
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [],
            "total_events": 12,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.03,
        generation_id=str(uuid4()),
        match_event_id=4459411,
        start_order_time_us=1785605093280000,
    )
    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert seen["search_event_id"] == 4459411
    assert seen["start_order_time_us"] is None


@pytest.mark.asyncio
async def test_match_without_any_locator_reports_why(monkeypatch):
    """An episode embedded before locators existed cannot borrow another event's position."""

    async def fake_context(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("hydration must not be attempted without a locator")

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(session_id=str(uuid4()), chunk_index=0, score=0.58, generation_id=str(uuid4()))
    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert match.evidence_status == "unavailable"
    assert match.evidence_reason == "search_hit_missing_locator"


@pytest.mark.asyncio
async def test_absent_store_status_fails_the_strict_contract(monkeypatch):
    """A malformed response is an error, not evidence that hydration partially worked."""

    async def fake_context(**kwargs):
        return {"context": [], "total_events": 0}

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.4,
        generation_id=str(uuid4()),
        match_event_id=99,
    )
    with pytest.raises(ValueError):
        await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_malformed_context_turn_fails_the_strict_contract(monkeypatch):
    async def fake_context(**_kwargs):
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [{**_context_row("the answer"), "unexpected": True}],
            "total_events": 1,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.4,
        generation_id=str(uuid4()),
        match_event_id=99,
    )
    with pytest.raises(ValueError):
        await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_semantic_match_carries_evidence_beside_its_context(monkeypatch):
    """A null `evidence` next to a populated `context` is its own small lie.

    The lexical lane fills evidence from an FTS snippet. The semantic lane has
    none, so it returned null there even once hydration was working — and a
    caller that checks `evidence` to decide whether a hit is worth reading would
    skip a match whose evidence was sitting in the very next field.
    """

    async def fake_context(**_kwargs):
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [
                _context_row("earlier turn, before the anchor", order_time_us=100, role="user"),
                _context_row("the anchored episode text", order_time_us=200),
                _context_row("later turn", order_time_us=300, role="user"),
            ],
            "total_events": 42,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=1,
        score=0.6,
        generation_id=str(uuid4()),
        start_order_time_us=200,
    )
    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert match.evidence == "the anchored episode text"
    assert match.context_text == "the anchored episode text"


@pytest.mark.asyncio
async def test_lexical_snippet_is_not_overwritten_by_the_anchor(monkeypatch):
    async def fake_context(**_kwargs):
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [_context_row("neighbour text", order_time_us=200)],
            "total_events": 42,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)

    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.05,
        generation_id=str(uuid4()),
        match_event_id=17,
        evidence="the matched fts snippet",
    )
    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert match.evidence == "the matched fts snippet"


@pytest.mark.asyncio
async def test_lexical_match_without_a_snippet_uses_its_exact_context_event(monkeypatch):
    async def fake_context(**_kwargs):
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "context": [
                _context_row("earlier neighbour", order_time_us=16),
                _context_row("the exact lexical match", order_time_us=17),
                _context_row("later neighbour", order_time_us=18),
            ],
            "total_events": 42,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    match = RecallMatch(
        session_id=str(uuid4()),
        chunk_index=0,
        score=0.05,
        generation_id=str(uuid4()),
        match_event_id=17,
    )

    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert match.evidence == "the exact lexical match"
    assert match.context_text == "the exact lexical match"
