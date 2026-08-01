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
            "context": [{"role": "assistant", "content_text": "the migration applied cleanly"}],
            "total_events": 590,
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
        return {"evidence_status": "complete", "evidence_reason": None, "context": [], "total_events": 12}

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
async def test_absent_store_status_is_not_read_as_complete(monkeypatch):
    """A response that carries no status is unknown, which is not the same as fine."""

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
    await agents_search._hydrate_recall_match(match, owner_id=42, context_turns=2, timeout_seconds=5.0)

    assert match.evidence_status == "partial"
    assert match.evidence_reason == "search_evidence_status_absent"


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
                {"order_time_us": 100, "role": "user", "content_text": "earlier turn, before the anchor"},
                {"order_time_us": 200, "role": "assistant", "content_text": "the anchored episode text"},
                {"order_time_us": 300, "role": "user", "content_text": "later turn"},
            ],
            "total_events": 42,
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
            "context": [{"order_time_us": 200, "role": "assistant", "content_text": "neighbour text"}],
            "total_events": 42,
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
