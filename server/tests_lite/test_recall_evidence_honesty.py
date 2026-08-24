"""Recall search cards and one-result expansion must stay small and truthful."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.routers import agents_search
from zerg.services.session_views import RecallCoverageSummary
from zerg.services.session_views import RecallMatch
from zerg.services.session_views import RecallResponse
from zerg.services.session_views import RecallSearchResult


def _timing() -> dict[str, float | int]:
    return {"admit_ms": 0.0, "sql_ms": 0.1, "active_readers": 1, "queued_readers": 0}


def _context_row(content: str, *, event_id: int = 100, role: str = "assistant") -> dict[str, object]:
    return {
        "search_event_id": event_id,
        "event_id": f"event-{event_id}",
        "source_object_id": "a" * 64,
        "record_ordinal": event_id,
        "order_time_us": event_id,
        "role": role,
        "content_text": content,
        "tool_name": None,
    }


def _match(**updates) -> RecallMatch:
    values = {
        "session_id": str(uuid4()),
        "generation_id": str(uuid4()),
        "match_event_id": 100,
        "chunk_index": 0,
        "score": 0.5,
        "retrieval_lanes": ["lexical"],
    }
    values.update(updates)
    return RecallMatch(**values)


def _card(**updates) -> RecallSearchResult:
    match = _match(evidence="the answer")
    values = agents_search._recall_search_result(match).model_dump()
    values.update(updates)
    return RecallSearchResult(**values)


def test_a_bare_match_is_unavailable_not_complete():
    assert RecallMatch(session_id=str(uuid4()), chunk_index=0, score=0.62).evidence_status == "unavailable"


def test_result_ref_round_trips_without_exposing_storage_locators():
    match = _match()
    result_ref = agents_search._encode_recall_ref(match)
    decoded = agents_search._decode_recall_ref(result_ref)

    assert len(result_ref) == 59
    assert decoded.session_id == match.session_id
    assert decoded.generation_id == match.generation_id
    assert decoded.search_event_id == 100
    assert match.session_id not in result_ref


def test_search_card_requires_exactly_one_snippet_state():
    with pytest.raises(ValueError, match="either a snippet"):
        _card(snippet=None, snippet_unavailable_reason=None)
    with pytest.raises(ValueError, match="either a snippet"):
        _card(snippet="answer", snippet_unavailable_reason="also unavailable")


def test_search_card_caps_utf8_snippets_and_the_page_has_a_serialized_ceiling():
    match = _match(evidence="\x1b" + "é" * 320)
    card = agents_search._recall_search_result(match)

    assert len(card.snippet.encode("utf-8")) <= agents_search.RECALL_SEARCH_SNIPPET_BYTES
    assert card.snippet.endswith(" …[truncated]")
    assert "\x1b" not in card.snippet
    cards = [agents_search._recall_search_result(_match(evidence="é" * 320)) for _ in range(10)]
    response = RecallResponse(results=cards, total=10, lanes=["lexical"])
    assert len(response.model_dump_json(exclude_none=True).encode("utf-8")) <= 12 * 1024
    with pytest.raises(ValueError):
        RecallResponse(results=cards + [card], total=11, lanes=["lexical"])


def test_search_page_drops_only_trailing_cards_when_json_escaping_fills_the_ceiling():
    cards = [
        agents_search._recall_search_result(
            _match(
                project='"' * 200,
                provider='"' * 64,
                matched_tool_name='"' * 128,
                evidence='"' * 320,
            )
        )
        for _ in range(10)
    ]

    response = agents_search._fit_recall_search_response(
        results=cards,
        lanes=["lexical"],
        degraded=[],
    )

    assert 0 < response.total < 10
    assert response.results == cards[: response.total]
    assert len(response.model_dump_json(exclude_none=True).encode("utf-8")) <= agents_search.RECALL_SERIALIZED_RESPONSE_BYTES


def test_recall_response_exposes_only_compact_coverage_and_consistent_lanes():
    card = _card()
    coverage = RecallCoverageSummary(
        complete=True,
        lagging_sessions=0,
        unpublished_sessions=0,
        oldest_lag_seconds=None,
    )
    response = RecallResponse(results=[card], total=1, lanes=["lexical", "dense"], coverage=coverage)
    assert response.total == 1

    with pytest.raises(ValueError, match="dense recall requires"):
        RecallResponse(results=[card], total=1, lanes=["dense"])
    with pytest.raises(ValueError, match="lexical-only"):
        RecallResponse(results=[card], total=1, lanes=["lexical"], coverage=coverage)
    with pytest.raises(ValueError, match="lane attribution"):
        RecallResponse(results=[card.model_copy(update={"matched_by": ["dense"]})], total=1, lanes=["lexical"])


@pytest.mark.asyncio
async def test_search_hydration_fetches_only_the_anchor_and_discards_raw_context(monkeypatch):
    seen: dict = {}

    async def fake_context(**kwargs):
        seen.update(kwargs)
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "anchor_event_id": 100,
            "context": [_context_row("the bounded anchor")],
            "total_events": 42,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    match = _match(evidence=None)
    await agents_search._hydrate_recall_match(match, owner_id=42, timeout_seconds=5.0)

    assert seen["before_turns"] == 0
    assert seen["after_turns"] == 0
    assert seen["max_content_bytes"] == agents_search.RECALL_SEARCH_SNIPPET_BYTES
    assert match.evidence == "the bounded anchor"
    assert match.context == []
    assert match.total_events == 42
    assert match.matched_role == "assistant"
    assert match.evidence_status == "not_requested"


def test_unexpandable_hit_is_skipped_without_erasing_valid_cards():
    valid = _match(evidence="valid answer")
    invalid = _match(generation_id=None, match_event_id=None, evidence="bad locator")

    results = agents_search._recall_search_results([invalid, valid])

    assert [result.session_id for result in results] == [valid.session_id]


@pytest.mark.asyncio
async def test_semantic_position_locator_hydrates_without_borrowing_an_event_id(monkeypatch):
    seen: dict = {}

    async def fake_context(**kwargs):
        seen.update(kwargs)
        return {
            "evidence_status": "complete",
            "evidence_reason": None,
            "anchor_event_id": 333,
            "context": [_context_row("semantic anchor", event_id=333)],
            "total_events": 590,
            "timing": _timing(),
        }

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    match = _match(match_event_id=None, start_order_time_us=1785605093280000, evidence=None)
    await agents_search._hydrate_recall_match(match, owner_id=42, timeout_seconds=5.0)

    assert seen["search_event_id"] is None
    assert seen["start_order_time_us"] == 1785605093280000
    assert match.match_event_id == 333
    assert match.evidence == "semantic anchor"


@pytest.mark.asyncio
async def test_match_without_locator_reports_why_and_never_calls_searchd(monkeypatch):
    async def fake_context(**_kwargs):  # pragma: no cover
        raise AssertionError("must not fetch without a locator")

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    match = _match(match_event_id=None)
    await agents_search._hydrate_recall_match(match, owner_id=42, timeout_seconds=5.0)
    assert (match.evidence_status, match.evidence_reason) == ("unavailable", "search_hit_missing_locator")


@pytest.mark.asyncio
async def test_context_expansion_is_one_result_strips_locators_and_clamps_total_budget(monkeypatch):
    match = _match()
    result_ref = agents_search._encode_recall_ref(match)
    seen: dict = {}

    async def fake_context(**kwargs):
        seen.update(kwargs)
        return agents_search._RecallContextPayload.model_validate(
            {
                "evidence_status": "complete",
                "evidence_reason": None,
                "anchor_event_id": 100,
                "context": [
                    _context_row("before", event_id=99),
                    _context_row("match", event_id=100),
                    _context_row("after", event_id=101),
                ],
                "total_events": 1000,
                "timing": _timing(),
            }
        )

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agents/recall/context",
            "headers": [],
            "query_string": f"ref={result_ref}&before=5&after=5&max_content_bytes=4000".encode(),
        }
    )
    response = await agents_search.recall_context(
        request=request,
        ref=result_ref,
        before=5,
        after=5,
        max_content_bytes=4000,
        _auth=SimpleNamespace(owner_id=42),
        _single=None,
    )

    assert seen["owner_id"] == 42
    assert seen["max_content_bytes"] == 8192 // 11
    assert response.content_byte_budget <= 8192
    assert [turn.is_match for turn in response.turns] == [False, True, False]
    assert "search_event_id" not in response.turns[0].model_dump()


@pytest.mark.asyncio
async def test_context_expansion_trims_json_escaping_instead_of_raising(monkeypatch):
    match = _match()
    result_ref = agents_search._encode_recall_ref(match)

    async def fake_context(**_kwargs):
        return agents_search._RecallContextPayload.model_validate(
            {
                "evidence_status": "complete",
                "evidence_reason": None,
                "anchor_event_id": 100,
                "context": [
                    _context_row("\x1b" * 4_000, event_id=100),
                    _context_row("\x1b" * 4_000, event_id=101),
                ],
                "total_events": 2,
                "timing": _timing(),
            }
        )

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agents/recall/context",
            "headers": [],
            "query_string": f"ref={result_ref}&before=0&after=1&max_content_bytes=4000".encode(),
        }
    )
    response = await agents_search.recall_context(
        request=request,
        ref=result_ref,
        before=0,
        after=1,
        max_content_bytes=4_000,
        _auth=SimpleNamespace(owner_id=42),
        _single=None,
    )

    assert response.evidence_status == "partial"
    assert "response_byte_ceiling_applied" in (response.evidence_reason or "")
    assert sum(turn.is_match for turn in response.turns) == 1
    assert len(response.model_dump_json(exclude_none=True).encode("utf-8")) <= agents_search.RECALL_SERIALIZED_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_context_expansion_rejects_malformed_refs_before_search(monkeypatch):
    async def fake_context(**_kwargs):  # pragma: no cover
        raise AssertionError("invalid refs must not reach searchd")

    monkeypatch.setattr(agents_search, "search_storage_v2_context", fake_context)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/agents/recall/context",
            "headers": [],
            "query_string": b"ref=rr1_bad",
        }
    )
    with pytest.raises(agents_search.HTTPException) as failure:
        await agents_search.recall_context(
            request=request,
            ref="rr1_bad",
            before=2,
            after=2,
            max_content_bytes=1200,
            _auth=SimpleNamespace(owner_id=42),
            _single=None,
        )
    assert failure.value.status_code == 422
