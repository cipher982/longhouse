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

from tests_lite.test_agents_storage_v2 import _InlineRenderPool
from tests_lite.test_agents_storage_v2 import _payload
from tests_lite.test_agents_storage_v2 import _storage_v2_stack
from zerg.config import get_settings
from zerg.services.session_provider_facts import last_turn
from zerg.services.session_provider_facts import provider_facts_from_rows
from zerg.services.session_provider_facts import recap
from zerg.services.session_provider_facts import turn_ends_by_event
from zerg.services.session_provider_facts import usage_latest
from zerg.services.session_title import resolve_title_provenance
from zerg.services.storage_v2_workspace import _workspace_envelope


def _payload_for_source(*, tenant_id: str, epoch, opaque_source_id: str, data: bytes = b"hello\n") -> dict:
    """An envelope for its own transcript file, so several sessions can share one authenticated machine."""
    from zerg.storage_v2.contracts import EnvelopeIdentity
    from zerg.storage_v2.contracts import envelope_id
    from zerg.storage_v2.contracts import hash_records

    payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=epoch, data=data)
    payload["opaque_source_id"] = opaque_source_id
    payload["render"]["generation_id"] = str(uuid4())
    payload["expected_envelope_id"] = envelope_id(
        EnvelopeIdentity(
            tenant_id=tenant_id,
            machine_id="cinder",
            provider="codex",
            opaque_source_id=opaque_source_id,
            source_epoch=epoch,
            range_kind="byte_offset",
            range_start=0,
            range_end=len(data),
            record_hashes=hash_records((data,)),
        )
    )
    return payload


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
async def test_malformed_facts_are_dropped_and_never_block_the_raw_envelope(monkeypatch):
    """Facts derive from the bytes beside them; a bad fact must not park the source cursor."""
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-facts-bad-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        ended_at = datetime(2026, 9, 3, 14, 20, 39, tzinfo=UTC)
        broken_facts = (
            {**_fact(ended_at), "source_position": 99},  # outside the envelope range
            {**_fact(ended_at), "payload": {"duration_ms": "fast"}},  # wrong payload type
            {**_fact(ended_at), "kind": "turn.mystery"},  # not catalogued
            {"kind": "turn.duration", "at": ended_at.isoformat()},  # missing fields
            {"kind": "context.compaction", "at": ended_at.isoformat(), "source_position": 2, "payload": {}},  # empty
        )
        # One transcript file per case: the epoch-open guard is not under test.
        for index, broken in enumerate(broken_facts):
            payload = _payload_for_source(tenant_id=tenant_id, epoch=uuid4(), opaque_source_id=f"broken-{index}.jsonl")
            payload["session_id"] = str(uuid4())
            payload["facts"] = [broken, _fact(ended_at, position=4)]
            response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
            assert response.status_code == 200, (broken, response.text)
            listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": payload["session_id"]})
            assert [(fact["kind"], fact["source_position"]) for fact in listed["facts"]] == [("turn.duration", 4)], broken

        # Codex compaction names what it replaced rather than token counts.
        payload = _payload_for_source(tenant_id=tenant_id, epoch=uuid4(), opaque_source_id="compaction.jsonl")
        payload["session_id"] = str(uuid4())
        payload["facts"] = [
            {"kind": "context.compaction", "at": ended_at.isoformat(), "source_position": 2, "payload": {"replacement_items": 2}}
        ]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text
        listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": payload["session_id"]})
        assert [fact["kind"] for fact in listed["facts"]] == ["context.compaction"]

        # An envelope without the key at all is the pre-facts engine and stays valid.
        payload = _payload_for_source(tenant_id=tenant_id, epoch=uuid4(), opaque_source_id="legacy.jsonl")
        payload["session_id"] = str(uuid4())
        assert "facts" not in payload
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_source_replacement_retires_the_predecessor_epochs_facts(monkeypatch):
    """A rewritten transcript must not keep the old epoch's turns eligible for last_turn or the recap."""
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-facts-epoch-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        first_epoch = uuid4()
        t0 = datetime(2026, 9, 3, 14, 20, 39, tzinfo=UTC)
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=first_epoch)
        payload["session_id"] = str(session_id)
        payload["facts"] = [
            _fact(t0 + timedelta(hours=2), position=3, duration_ms=999_999),
            {"kind": "session.recap", "at": (t0 + timedelta(hours=2)).isoformat(), "source_position": 4, "payload": {"text": "old recap"}},
        ]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        replacement = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4(), data=b"hello again\n")
        replacement["session_id"] = str(session_id)
        replacement["predecessor_source_epoch"] = str(first_epoch)
        replacement["epoch_opened_at"] = "2026-07-12T13:00:00+00:00"
        replacement["facts"] = [_fact(t0, position=3, duration_ms=58_459)]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=replacement, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert [(fact["kind"], fact["source_epoch"]) for fact in listed["facts"]] == [("turn.duration", replacement["source_epoch"])]
        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        facts = provider_facts_from_rows(read["provider_facts"])
        assert last_turn(facts, {})["duration_ms"] == 58_459
        assert recap(facts) is None


@pytest.mark.asyncio
async def test_session_read_serves_the_newest_session_facts_and_every_turn_duration(monkeypatch):
    """A long session's turn facts must never evict the recap, title or usage the chrome shows."""
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-facts-split-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        epoch = uuid4()
        t0 = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=epoch)
        payload["session_id"] = str(session_id)
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        facts = [
            {"kind": "session.recap", "at": t0.isoformat(), "source_position": 1, "payload": {"text": "first recap"}},
            {
                "kind": "session.recap",
                "at": (t0 + timedelta(minutes=1)).isoformat(),
                "source_position": 2,
                "payload": {"text": "newest recap"},
            },
            {"kind": "session.title", "at": t0.isoformat(), "source_position": 3, "payload": {"title": "The provider's name"}},
        ]
        for turn in range(400):
            at = t0 + timedelta(minutes=2 + turn)
            facts.append({"kind": "turn.usage", "at": at.isoformat(), "source_position": 10 + 2 * turn, "payload": {"output_tokens": turn}})
            facts.append(
                {"kind": "turn.duration", "at": at.isoformat(), "source_position": 11 + 2 * turn, "payload": {"duration_ms": 1_000 + turn}}
            )
        await stack.catalog.call(
            "session.provider_facts.insert.v2",
            {"session_id": str(session_id), "source_epoch": str(epoch), "provider_facts": facts},
        )
        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        served = provider_facts_from_rows(read["provider_facts"])
        by_kind: dict[str, list] = {}
        for fact in served:
            by_kind.setdefault(fact["kind"], []).append(fact)
        assert len(by_kind["turn.duration"]) == 400, "every turn keeps its footer"
        assert [fact["payload"]["text"] for fact in by_kind["session.recap"]] == ["newest recap"]
        assert [fact["payload"]["title"] for fact in by_kind["session.title"]] == ["The provider's name"]
        assert [fact["payload"]["output_tokens"] for fact in by_kind["turn.usage"]] == [399]
        # The flat list RPC keeps its newest-N view for other callers.
        listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert len(listed["facts"]) == 200


def test_turn_end_never_crosses_a_user_boundary_or_leaves_its_page():
    t0 = datetime(2026, 9, 3, 14, 0, 0, tzinfo=UTC)
    events = [
        _event("u-1", "user", t0),
        _event("a-1", "assistant", t0 + timedelta(minutes=1)),
        _event("u-2", "user", t0 + timedelta(minutes=2)),
    ]
    # Turn two was stopped before any output: its fact decorates nothing,
    # never the previous turn's reply.
    aborted = {"kind": "turn.duration", "at": t0 + timedelta(minutes=5), "payload": {"duration_ms": 180_000, "outcome": "aborted"}}
    assert turn_ends_by_event([aborted], events, page_is_tail=True) == {}
    # Turn one's fact still lands on its reply.
    finished = {"kind": "turn.duration", "at": t0 + timedelta(minutes=1, seconds=1), "payload": {"duration_ms": 61_000}}
    assert set(turn_ends_by_event([finished], events)) == {"a-1"}
    # On an older page, a fact later than every event belongs to a turn that
    # is not on this page; only the newest page may claim it.
    old_page = [_event("u-1", "user", t0), _event("a-1", "assistant", t0 + timedelta(minutes=1))]
    newest = {"kind": "turn.duration", "at": t0 + timedelta(days=30), "payload": {"duration_ms": 5}}
    assert turn_ends_by_event([newest], old_page, page_is_tail=False) == {}
    assert set(turn_ends_by_event([newest], old_page, page_is_tail=True)) == {"a-1"}


@pytest.mark.asyncio
async def test_provider_title_promotes_an_llm_title_once_and_applies_on_replay(monkeypatch):
    """The provider's name wins the race with the LLM title however the batches land."""
    from zerg.services import storage_session_titles

    scheduled: list[dict] = []
    monkeypatch.setattr(storage_session_titles, "schedule_storage_session_title", lambda candidate: scheduled.append(candidate) or True)

    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-title-race-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(session_id)
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text
        assert len(scheduled) == 1, "the prompt-only batch schedules the LLM title"

        # The LLM wins the race and freezes the anchor.
        await stack.catalog.call(
            "storage.session.title.complete.v2",
            {"session_id": str(session_id), "title": "LLM guess", "completed_at": datetime.now(UTC).isoformat(), "source": "ai"},
        )
        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert (read["session"]["anchor_title"], read["session"]["anchor_title_source"]) == ("LLM guess", "ai")

        # The same envelope replayed with the provider's title (a pre-facts
        # engine catching up) promotes the anchor once.
        payload["facts"] = [
            {"kind": "session.title", "at": "1970-01-01T00:00:00+00:00", "source_position": 0, "payload": {"title": "Claude's own name"}}
        ]
        replay = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert replay.status_code == 200, replay.text
        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert (read["session"]["anchor_title"], read["session"]["anchor_title_source"]) == ("Claude's own name", "provider")

        # A provider anchor is frozen against the LLM and against later names.
        await stack.catalog.call(
            "storage.session.title.complete.v2",
            {"session_id": str(session_id), "title": "Late LLM guess", "completed_at": datetime.now(UTC).isoformat(), "source": "ai"},
        )
        await stack.catalog.call(
            "storage.session.title.complete.v2",
            {"session_id": str(session_id), "title": "Renamed later", "completed_at": datetime.now(UTC).isoformat(), "source": "provider"},
        )
        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert read["session"]["anchor_title"] == "Claude's own name"


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
        "t-1": {"duration_ms": 129_299, "ended_at": facts[0]["at"].isoformat(), "message_count": 898, "outcome": "completed"},
        "a-2": {"duration_ms": 58_459, "ended_at": facts[1]["at"].isoformat(), "message_count": None, "outcome": "completed"},
    }
    assert last_turn(facts, ends) == {
        "duration_ms": 58_459,
        "ended_at": facts[1]["at"].isoformat(),
        "event_id": "a-2",
        "outcome": "completed",
    }
    # Off-page anchors still surface as the session's last turn, without an event.
    assert last_turn(facts, {}) == {
        "duration_ms": 58_459,
        "ended_at": facts[1]["at"].isoformat(),
        "event_id": None,
        "outcome": "completed",
    }


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
    assert items[1]["event"]["turn_end"] == {
        "duration_ms": 129_299,
        "ended_at": facts[0]["at"].isoformat(),
        "message_count": None,
        "outcome": "completed",
    }
    assert envelope["session"]["last_turn"] == {
        "duration_ms": 129_299,
        "ended_at": facts[0]["at"].isoformat(),
        "event_id": "a-1",
        "outcome": "completed",
    }


def test_a_stopped_codex_turn_is_served_as_aborted_not_as_work_done():
    t0 = datetime(2026, 9, 3, 14, 26, 0, tzinfo=UTC)
    events = [_event("u-1", "user", t0), _event("a-1", "assistant", t0 + timedelta(minutes=1))]
    facts = [
        {
            "kind": "turn.duration",
            "at": t0 + timedelta(hours=3),
            "payload": {"duration_ms": 10_839_735, "outcome": "aborted", "reason": "interrupted", "turn_id": "t-2"},
        }
    ]
    decorations = turn_ends_by_event(facts, events)
    assert decorations["a-1"]["outcome"] == "aborted"
    assert last_turn(facts, decorations) == {
        "duration_ms": 10_839_735,
        "ended_at": facts[0]["at"].isoformat(),
        "event_id": "a-1",
        "outcome": "aborted",
    }


@pytest.mark.asyncio
async def test_provider_title_freezes_the_anchor_and_skips_the_llm_title(monkeypatch):
    """Claude names its own session; Longhouse keeps that name instead of buying one."""
    from zerg.services import storage_session_titles

    scheduled: list[dict] = []
    monkeypatch.setattr(storage_session_titles, "schedule_storage_session_title", lambda candidate: scheduled.append(candidate) or True)

    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-title-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(session_id)
        payload["facts"] = [
            {
                "kind": "session.title",
                "at": "1970-01-01T00:00:00+00:00",
                "source_position": 0,
                "payload": {"title": "G55 app tablet UI beautification"},
            }
        ]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert read["found"] is True
        assert read["session"]["anchor_title"] == "G55 app tablet UI beautification"
        assert read["session"]["anchor_title_source"] == "provider"
        assert scheduled == [], "no LLM title is scheduled when the provider already named the session"

        # A later provider title never rewrites the frozen anchor.
        payload["facts"] = [
            {
                "kind": "session.title",
                "at": "1970-01-01T00:00:00+00:00",
                "source_position": 1,
                "payload": {"title": "Something else entirely"},
            }
        ]
        again = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert again.status_code == 200, again.text
        read_again = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert read_again["session"]["anchor_title"] == "G55 app tablet UI beautification"


def test_title_provenance_names_the_provider_when_it_wrote_the_anchor():
    assert resolve_title_provenance(
        anchor_title="G55 app tablet UI beautification",
        first_user_message="make it beautiful",
        user_messages=1,
        title_retry_at=None,
        anchor_title_source="provider",
    ) == ("ready", "provider")
    assert resolve_title_provenance(
        anchor_title="Console session bugs and UX",
        first_user_message="fix it",
        user_messages=1,
        title_retry_at=None,
    ) == ("ready", "ai")


def test_recap_is_the_newest_provider_recap_and_never_synthesised():
    t0 = datetime(2026, 9, 2, 23, 10, 5, tzinfo=UTC)
    facts = [
        {"kind": "session.recap", "at": t0, "payload": {"text": "Rebuilt the DRIVE page. Next: check it in the truck."}},
        {
            "kind": "session.recap",
            "at": t0 + timedelta(minutes=14),
            "payload": {"text": "Fixed 15 defects. Next: deploy the gateway build."},
        },
        {"kind": "turn.duration", "at": t0 + timedelta(minutes=20), "payload": {"duration_ms": 10}},
    ]
    assert recap(facts) == {"text": "Fixed 15 defects. Next: deploy the gateway build.", "at": (t0 + timedelta(minutes=14)).isoformat()}
    assert recap([facts[2]]) is None


def test_workspace_envelope_serves_the_recap():
    session_id = uuid4()
    session = SimpleNamespace(
        provider="claude",
        runtime_display=SimpleNamespace(lifecycle="open"),
        capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
        model_dump=lambda mode: {"id": str(session_id)},
    )
    t0 = datetime(2026, 9, 2, 23, 10, 5, tzinfo=UTC)
    envelope = _workspace_envelope(
        session_id=session_id,
        session=session,
        session_commit_seq="7",
        branch_mode="head",
        anchor="tail",
        cursor=None,
        storage={"commit_seq": "3", "session": {"updated_at": t0.isoformat()}},
        page={"events": [], "total": 0, "generation_id": "g1", "next_cursor": None},
        receipts=[],
        facts=[{"kind": "session.recap", "at": t0, "payload": {"text": "Next: check it in the truck."}}],
    )
    assert envelope["session"]["recap"] == {"text": "Next: check it in the truck.", "at": t0.isoformat()}
    assert envelope["session"]["last_turn"] is None


def test_usage_latest_is_the_newest_turn_ending_usage_with_context_size():
    t0 = datetime(2026, 9, 3, 14, 18, 40, tzinfo=UTC)
    facts = [
        {
            "kind": "turn.usage",
            "at": t0,
            "payload": {
                "model": "claude-opus-5",
                "effort": "high",
                "input_tokens": 2,
                "cache_read_input_tokens": 401_000,
                "cache_creation_input_tokens": 300,
                "output_tokens": 177,
                "thinking_tokens": 12,
            },
        },
        {
            "kind": "turn.usage",
            "at": t0 - timedelta(minutes=5),
            "payload": {"model": "claude-opus-5", "input_tokens": 2, "cache_read_input_tokens": 300_000, "output_tokens": 40},
        },
        {"kind": "turn.usage", "at": t0 + timedelta(minutes=1), "payload": {"model": "claude-opus-5", "output_tokens": "many"}},
    ]
    assert usage_latest(facts) == {
        "model": "claude-opus-5",
        "effort": "high",
        "context_tokens": 401_302,
        "context_window": None,
        "output_tokens": 177,
        "thinking_tokens": 12,
        "at": t0.isoformat(),
    }
    assert usage_latest([]) is None


def test_usage_latest_trusts_the_providers_own_context_accounting():
    """Codex names its context size and window; its input classes overlap, so a sum would be wrong."""
    t0 = datetime(2026, 9, 3, 11, 25, 31, tzinfo=UTC)
    facts = [
        {
            "kind": "turn.usage",
            "at": t0,
            "payload": {
                "model": "gpt-5.6-luna",
                "effort": "xhigh",
                "input_tokens": 25_000,
                "cache_read_input_tokens": 24_700,
                "cache_creation_input_tokens": 300,
                "output_tokens": 210,
                "thinking_tokens": 90,
                "context_tokens": 25_210,
                "context_window": 258_400,
            },
        }
    ]
    assert usage_latest(facts) == {
        "model": "gpt-5.6-luna",
        "effort": "xhigh",
        "context_tokens": 25_210,
        "context_window": 258_400,
        "output_tokens": 210,
        "thinking_tokens": 90,
        "at": t0.isoformat(),
    }


@pytest.mark.asyncio
async def test_usage_error_and_compaction_facts_are_accepted_and_listed(monkeypatch):
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-usage-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(session_id)
        at = datetime(2026, 9, 3, 14, 18, 40, tzinfo=UTC).isoformat()
        payload["facts"] = [
            {
                "kind": "turn.usage",
                "at": at,
                "source_position": 1,
                "payload": {"model": "claude-opus-5", "effort": "high", "cache_read_input_tokens": 401_000, "output_tokens": 177},
            },
            {
                "kind": "turn.api_error",
                "at": at,
                "source_position": 2,
                "payload": {"error": "API Error: 529 Overloaded", "retry_attempt": 1, "max_retries": 10, "retry_in_ms": 1000},
            },
            {
                "kind": "context.compaction",
                "at": at,
                "source_position": 3,
                "payload": {"trigger": "manual", "pre_tokens": 269_584, "post_tokens": 15_694, "duration_ms": 144_947},
            },
        ]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text
        listed = await stack.catalog.call("session.provider_facts.list.v2", {"session_id": str(session_id)})
        assert sorted(fact["kind"] for fact in listed["facts"]) == ["context.compaction", "turn.api_error", "turn.usage"]

        payload["facts"] = [{"kind": "turn.usage", "at": at, "source_position": 4, "payload": {"model": "x"}}]
        rejected = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert rejected.status_code == 422, rejected.text


@pytest.mark.asyncio
async def test_session_read_carries_provenance_so_the_workspace_needs_no_extra_reads(monkeypatch):
    """A busy catalog rejected the separate list calls; the session read carries both now."""
    async with _storage_v2_stack(monkeypatch, render_pool_factory=_InlineRenderPool, prefix="lh2-provenance-") as stack:
        tenant_id = get_settings().archive_primary_tenant_id
        session_id = uuid4()
        payload = _payload(tenant_id=tenant_id, machine_id="cinder", epoch=uuid4())
        payload["session_id"] = str(session_id)
        payload["facts"] = [_fact(datetime(2026, 9, 3, 14, 20, 39, tzinfo=UTC))]
        response = await stack.client.post("/agents/storage/v2/envelopes", json=payload, headers={"X-Longhouse-Storage-Lane": "live"})
        assert response.status_code == 200, response.text

        read = await stack.catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
        assert read["found"] is True
        assert [fact["kind"] for fact in read["provider_facts"]] == ["turn.duration"]
        assert read["input_receipts"] == []

        calls: list[str] = []
        original = stack.catalog.call

        async def recording(method, params=None, **kwargs):
            calls.append(method)
            return await original(method, params, **kwargs)

        monkeypatch.setattr(stack.catalog, "call", recording)
        from zerg.services import storage_v2_workspace

        live_session = SimpleNamespace(
            provider="codex",
            origin_kind=None,
            runtime_display=SimpleNamespace(lifecycle="open"),
            capabilities=SimpleNamespace(live_control_available=True, can_start_turn=True),
            model_dump=lambda mode: {"id": str(session_id)},
        )
        monkeypatch.setattr(storage_v2_workspace, "get_catalogd_client", lambda: stack.catalog)
        monkeypatch.setattr(storage_v2_workspace, "read_live_catalog_session", lambda sid, owner_id: (live_session, None, "1"))

        workspace = await storage_v2_workspace.build_storage_v2_workspace(session_id=session_id, owner_id=1, branch_mode="head", limit=50)
        assert workspace is not None
        assert workspace["session"]["last_turn"]["duration_ms"] == 129_299
        assert "session.provider_facts.list.v2" not in calls
        assert "session.input.receipts.list.v2" not in calls
