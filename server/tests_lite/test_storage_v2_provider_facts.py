"""Provider facts ride the storage-v2 commit and decorate the session surfaces.

A provider writes turn accounting on transcript lines the render surface
never shows. The engine ships those as typed facts beside the raw bytes; the
catalog keeps one row per source line; the workspace stamps `turn_end` on
the event the turn finished on and serves `last_turn` on the session.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zerg.config import get_settings
from zerg.services.session_provider_facts import last_turn
from zerg.services.session_provider_facts import turn_ends_by_event
from zerg.services.storage_v2_workspace import _workspace_envelope

from tests_lite.test_agents_storage_v2 import _InlineRenderPool
from tests_lite.test_agents_storage_v2 import _payload
from tests_lite.test_agents_storage_v2 import _storage_v2_stack


def _fact(at: datetime, *, position: int = 3, duration_ms: int = 129_299) -> dict:
    return {
        "kind": "turn.duration",
        "at": at.isoformat(),
        "source_position": position,
        "payload": {"duration_ms": duration_ms, "message_count": 898},
    }


@pytest.mark.asyncio
async def test_turn_duration_fact_commits_beside_the_raw_bytes_and_replays_idempotently(monkeypatch):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-facts-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(session_id)
        ended_at = datetime(2026, 9, 3, 14, 20, 39, 100_000, tzinfo=UTC)
        payload["facts"] = [_fact(ended_at)]

        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert [(fact["kind"], fact["source_position"]) for fact in listed["facts"]] == [("turn.duration", 3)]
        assert listed["facts"][0]["payload"] == '{"duration_ms":129299,"message_count":898}'

        # An exact replay of the same envelope neither duplicates nor rejects the fact.
        replay = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert replay.status_code == 200, replay.text
        listed_again = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert len(listed_again["facts"]) == 1

        # A replay from an engine that now ships facts for bytes it committed
        # before facts existed adds the missing row without moving the receipt.
        payload["facts"] = [_fact(ended_at), _fact(ended_at + timedelta(minutes=5), position=4, duration_ms=58_459)]
        backfill = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert backfill.status_code == 200, backfill.text
        listed_backfilled = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert sorted(fact["source_position"] for fact in listed_backfilled["facts"]) == [3, 4]


@pytest.mark.asyncio
async def test_provider_facts_are_validated_strictly(monkeypatch):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-facts-bad-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        ended_at = datetime(2026, 9, 3, 14, 20, 39, tzinfo=UTC)
        for broken in (
            {**_fact(ended_at), "source_position": 99},  # outside the envelope range
            {**_fact(ended_at), "payload": {"duration_ms": "fast"}},  # wrong payload type
            {**_fact(ended_at), "kind": "turn.mystery"},  # not catalogued
            {"kind": "turn.duration", "at": ended_at.isoformat()},  # missing fields
        ):
            payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
            payload["session_id"] = str(uuid4())
            payload["facts"] = [broken]
            response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
            assert response.status_code == 422, (broken, response.text)

        # An envelope without the key at all is the pre-facts engine and stays valid.
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(uuid4())
        assert "facts" not in payload
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text


def _event(event_id: str, role: str, at: datetime) -> dict[str, object]:
    return {
        "event_id": event_id,
        "cursor": event_id,
        "role": role,
        "content_text": event_id,
        "timestamp": at.isoformat(),
        "tool_name": None,
        "tool_call_id": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "branch_kind": None,
    }


def test_turn_end_anchors_to_the_last_non_user_event_before_the_fact():
    t0 = datetime(2026, 9, 3, 14, 18, 30, tzinfo=UTC)
    events = [
        _event("u-1", "user", t0),
        _event("a-1", "assistant", t0 + timedelta(minutes=2)),
        _event("t-1", "tool", t0 + timedelta(minutes=2, seconds=5)),
        _event("u-2", "user", t0 + timedelta(minutes=3)),
        _event("a-2", "assistant", t0 + timedelta(minutes=4)),
    ]
    facts = [
        {"kind": "turn.duration", "at": t0 + timedelta(minutes=2, seconds=9), "payload": {"duration_ms": 129_299, "message_count": 898}},
        {"kind": "turn.duration", "at": t0 + timedelta(minutes=4, seconds=1), "payload": {"duration_ms": 58_459}},
        # A fact before any non-user event has no anchor and decorates nothing.
        {"kind": "turn.duration", "at": t0 - timedelta(minutes=1), "payload": {"duration_ms": 49}},
    ]
    ends = turn_ends_by_event(facts, events)
    assert ends == {
        "t-1": {"duration_ms": 129_299, "ended_at": facts[0]["at"].isoformat(), "message_count": 898},
        "a-2": {"duration_ms": 58_459, "ended_at": facts[1]["at"].isoformat(), "message_count": None},
    }
    assert last_turn(facts, ends) == {"duration_ms": 58_459, "ended_at": facts[1]["at"].isoformat(), "event_id": "a-2"}
    # Off-page anchors still surface as the session's last turn, without an event.
    assert last_turn(facts, {}) == {"duration_ms": 58_459, "ended_at": facts[1]["at"].isoformat(), "event_id": None}


def test_workspace_envelope_stamps_turn_end_and_last_turn():
    session_id = uuid4()
    session = SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
        model_dump=lambda mode: {"id": str(session_id)},
    )
    t0 = datetime(2026, 9, 3, 14, 18, 30, tzinfo=UTC)
    events = [_event("u-1", "user", t0), _event("a-1", "assistant", t0 + timedelta(minutes=2))]
    facts = [{"kind": "turn.duration", "at": t0 + timedelta(minutes=2, seconds=9), "payload": {"duration_ms": 129_299}}]
    envelope = _workspace_envelope(
        session_id=session_id,
        session=session,
        session_commit_seq="7",
        branch_mode="head",
        anchor="tail",
        cursor=None,
        storage={"commit_seq": "3", "session": {"updated_at": t0.isoformat()}},
        page={"events": events, "total": 2, "generation_id": "g1", "next_cursor": None},
        receipts=[],
        facts=facts,
    )
    items = envelope["projection"]["items"]
    assert items[0]["event"]["turn_end"] is None
    assert items[1]["event"]["turn_end"] == {"duration_ms": 129_299, "ended_at": facts[0]["at"].isoformat(), "message_count": None}
    assert envelope["session"]["last_turn"] == {"duration_ms": 129_299, "ended_at": facts[0]["at"].isoformat(), "event_id": "a-1"}
