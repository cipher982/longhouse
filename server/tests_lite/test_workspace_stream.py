"""Tests for the per-session workspace SSE stream."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")

import zerg.dependencies.auth as _auth_deps  # noqa: F401 — triggers settings init
import zerg.routers.timeline as timeline_mod
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLivePreview
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState


async def _noop_coro() -> None:
    """No-op replacement for wait helpers in tests."""


def _make_db(tmp_path, name="workspace_stream.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


class _DisconnectAfterNCycles:
    def __init__(self, n: int) -> None:
        self._checks = 0
        self._n = n

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._n


def _collect_stream_events(events: list[dict]) -> dict[str, list[dict]]:
    """Group SSE events by type."""
    result: dict[str, list[dict]] = {}
    for event in events:
        name = event["event"]
        result.setdefault(name, []).append(json.loads(event["data"]))
    return result


async def _run_stream(sf, session_id, *, cycles: int = 2, skip_initial: bool = False) -> list[dict]:
    request = _DisconnectAfterNCycles(cycles)
    events: list[dict] = []
    async for event in timeline_mod._session_workspace_stream(
        request,
        session_factory=sf,
        session_id=session_id,
        skip_initial=skip_initial,
    ):
        events.append(event)
    return events


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_emits_connected_and_changed(tmp_path):
    """Stream should emit connected + workspace_changed on first cycle."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    events = asyncio.run(_run_stream(sf, session_id))
    grouped = _collect_stream_events(events)

    assert "connected" in grouped, "Should emit connected event"
    assert "workspace_changed" in grouped, "Should emit workspace_changed event"

    changed = grouped["workspace_changed"][0]
    assert changed["session_id"] == str(session_id)
    assert "latest_event_id" in changed
    assert "thread_session_count" in changed


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_skips_when_unchanged(tmp_path):
    """Stream should not re-emit workspace_changed when signature is stable."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    events = asyncio.run(_run_stream(sf, session_id, cycles=4))
    grouped = _collect_stream_events(events)

    assert len(grouped.get("workspace_changed", [])) == 1, "Should only emit once when unchanged"


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_detects_new_event(tmp_path):
    """Stream should emit workspace_changed when new events are ingested."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    class _MutateAfterFirstCycle:
        def __init__(self):
            self._checks = 0

        async def is_disconnected(self) -> bool:
            self._checks += 1
            # After first full cycle (check 2), inject an event
            if self._checks == 2:
                with sf() as db:
                    event = AgentEvent(
                        session_id=str(session_id),
                        role="user",
                        content_text="Hello",
                        timestamp=now,
                    )
                    db.add(event)
                    db.commit()
            return self._checks > 3

    async def _run():
        request = _MutateAfterFirstCycle()
        events: list[dict] = []
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=False,
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    grouped = _collect_stream_events(events)

    assert len(grouped.get("workspace_changed", [])) == 2, (
        f"Expected 2 workspace_changed events, got {len(grouped.get('workspace_changed', []))}"
    )


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_detects_live_preview_projection_update(tmp_path):
    """Pure live preview updates should advance the detail stream signature."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="codex",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=0,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    class _MutatePreview:
        def __init__(self):
            self._checks = 0

        async def is_disconnected(self) -> bool:
            self._checks += 1
            if self._checks == 2:
                with sf() as db:
                    db.add(
                        SessionLivePreview(
                            session_id=session_id,
                            thread_id="thread-1",
                            turn_key=f"codex_bridge_live:{session_id}:thread-1:turn-1",
                            seq=1,
                            preview_text="hello live",
                            provisional_cursor=f"codex_bridge_live:{session_id}:thread-1:turn-1:1",
                            provisional_complete=0,
                            event_origin="live_provisional",
                            preview_observed_at=now,
                            preview_updated_at=now,
                            source="codex_bridge_live",
                            last_observation_id=f"runtime:codex_bridge_live:preview:{session_id}:1",
                        )
                    )
                    db.commit()
            return self._checks > 3

    async def _run():
        request = _MutatePreview()
        events: list[dict] = []
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=False,
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    grouped = _collect_stream_events(events)

    assert len(grouped.get("workspace_changed", [])) == 2
    preview_changed = grouped["workspace_changed"][1]
    assert preview_changed["latest_event_id"] == -1
    assert preview_changed["transcript_preview"]["text"] == "hello live"
    assert preview_changed["transcript_preview"]["event_origin"] == "live_provisional"


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_detects_presence_change(tmp_path):
    """Stream should detect presence mutations."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    class _MutatePresence:
        def __init__(self):
            self._checks = 0

        async def is_disconnected(self) -> bool:
            self._checks += 1
            if self._checks == 2:
                with sf() as db:
                    state = SessionRuntimeState(
                        runtime_key=f"claude:{session_id}",
                        session_id=session_id,
                        provider="claude",
                        phase="thinking",
                        phase_source="semantic",
                        phase_started_at=now,
                        last_runtime_signal_at=now,
                        last_live_at=now,
                        timeline_anchor_at=now,
                        runtime_version=1,
                    )
                    db.merge(state)
                    db.commit()
            return self._checks > 3

    async def _run():
        request = _MutatePresence()
        events: list[dict] = []
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=False,
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    grouped = _collect_stream_events(events)

    assert len(grouped.get("workspace_changed", [])) == 2


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_missing_session(tmp_path):
    """Stream should emit error for nonexistent session."""
    sf = _make_db(tmp_path)
    fake_id = uuid4()

    events = asyncio.run(_run_stream(sf, fake_id))
    grouped = _collect_stream_events(events)

    assert "connected" in grouped
    assert "error" in grouped
    assert grouped["error"][0]["error"] == "session_not_found"


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_emits_pubsub_seq_and_id(tmp_path):
    """workspace_changed frame should carry pubsub_seq in data and SSE id."""
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    sf = _make_db(tmp_path, name="workspace_stream_seq.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    # Test that consumed_seq drives the SSE id: frame. We can't easily inject
    # wakes in this harness (test mocks _wait_for_session_change to noop), so
    # the initial snapshot emits with pubsub_seq=0. End-to-end seq-after-wake
    # behavior is covered by the SessionPubsub unit tests.
    bus = get_pubsub()
    bus.publish(topic_session(str(session_id)), {"kind": "test", "n": 1})
    events = asyncio.run(_run_stream(sf, session_id, cycles=2))
    grouped = _collect_stream_events(events)
    assert "workspace_changed" in grouped
    changed = grouped["workspace_changed"][0]
    assert changed.get("pubsub_seq") == 0
    changed_events = [e for e in events if e["event"] == "workspace_changed"]
    assert changed_events[0].get("id") is None


def test_workspace_stream_wake_includes_fanout_metadata(tmp_path):
    """Real pubsub wake should carry the shared downlink timing fields."""
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    sf = _make_db(tmp_path, name="workspace_stream_wake.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    async def _run():
        request = _DisconnectAfterNCycles(4)
        events: list[dict] = []

        async def mutate_and_publish():
            await asyncio.sleep(0.01)
            with sf() as db:
                event = AgentEvent(
                    session_id=str(session_id),
                    role="assistant",
                    content_text="Hello from the shared downlink",
                    timestamp=now,
                )
                db.add(event)
                db.commit()
                db.refresh(event)

                get_pubsub().publish(
                    topic_session(str(session_id)),
                    {
                        "kind": "ingest",
                        "session_id": str(session_id),
                        "latest_event_id": event.id,
                        "server_fanout_at_ms": 1_779_220_000_150,
                        "ship_trace_id": "trace-1",
                    },
                )

        publish_task = asyncio.create_task(mutate_and_publish())
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=True,
        ):
            events.append(event)
            if event.get("event") == "workspace_changed":
                break
        await publish_task
        return events

    events = asyncio.run(_run())
    changed_events = [event for event in events if event["event"] == "workspace_changed"]

    assert len(changed_events) == 1
    assert changed_events[0]["id"] == "1"
    changed = json.loads(changed_events[0]["data"])
    assert changed["latest_event_id"] > 0
    assert changed["pubsub_seq"] == 1
    assert changed["server_fanout_at_ms"] == 1_779_220_000_150
    assert changed["transcript_preview"]["text"] == "Hello from the shared downlink"
    assert changed["transcript_preview"]["event_origin"] == "durable"
    assert changed["transcript_preview"]["is_provisional"] is False


def test_workspace_stream_does_not_preview_durable_tool_call(tmp_path):
    """Tool-call ledger updates should wake clients without masquerading as answer text."""
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import reset_pubsub_for_test
    from zerg.services.session_pubsub import topic_session

    reset_pubsub_for_test()
    sf = _make_db(tmp_path, name="workspace_stream_tool_call_preview.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=0,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    async def _run():
        request = _DisconnectAfterNCycles(4)
        events: list[dict] = []

        async def mutate_and_publish():
            await asyncio.sleep(0.01)
            with sf() as db:
                event = AgentEvent(
                    session_id=str(session_id),
                    role="assistant",
                    tool_name="Bash",
                    content_text="tool preamble",
                    timestamp=now,
                )
                db.add(event)
                db.commit()
                db.refresh(event)

                get_pubsub().publish(
                    topic_session(str(session_id)),
                    {
                        "kind": "ingest",
                        "session_id": str(session_id),
                        "latest_event_id": event.id,
                    },
                )

        publish_task = asyncio.create_task(mutate_and_publish())
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=True,
        ):
            events.append(event)
            if event.get("event") == "workspace_changed":
                break
        await publish_task
        return events

    events = asyncio.run(_run())
    changed_events = [event for event in events if event["event"] == "workspace_changed"]

    assert len(changed_events) == 1
    changed = json.loads(changed_events[0]["data"])
    assert changed["latest_event_id"] > 0
    assert changed["transcript_preview"] is None


def test_workspace_stream_can_emit_live_preview_before_db_signature_changes(tmp_path):
    """Live preview wakes should paint even before the durable observation commit."""
    from zerg.services.session_pubsub import publish_session_transcript_preview_update
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    sf = _make_db(tmp_path, name="workspace_stream_precommit_preview.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="codex",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=0,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    async def _run():
        request = _DisconnectAfterNCycles(8)
        events: list[dict] = []

        async def publish_preview():
            await asyncio.sleep(0.01)
            publish_session_transcript_preview_update(
                session_id=str(session_id),
                provider="codex",
                source="codex_bridge_live",
                transcript_preview={
                    "event_id": 3,
                    "text": "hello before sqlite",
                    "event_origin": "live_provisional",
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                    "is_provisional": True,
                    "is_complete": False,
                    "content_cursor": f"codex_bridge_live:{session_id}:thread-1:turn-1:3",
                    "is_stale": False,
                    "stale_reason": None,
                },
            )

        publish_task = asyncio.create_task(publish_preview())
        async for event in timeline_mod._session_workspace_stream(
            request,
            session_factory=sf,
            session_id=session_id,
            skip_initial=True,
        ):
            events.append(event)
            if event.get("event") == "workspace_changed":
                break
        await publish_task
        return events

    events = asyncio.run(_run())
    changed_events = [event for event in events if event["event"] == "workspace_changed"]

    assert len(changed_events) == 1
    changed = json.loads(changed_events[0]["data"])
    assert changed["pubsub_seq"] == 1
    assert changed["latest_event_id"] == -3
    assert changed["latest_event_emitted_at_ms"] == int(now.timestamp() * 1000)
    assert changed["transcript_preview"]["text"] == "hello before sqlite"
    assert changed["transcript_preview"]["event_origin"] == "live_provisional"

    with sf() as db:
        assert db.query(SessionObservation).filter(SessionObservation.session_id == session_id).count() == 0


@patch.object(timeline_mod, "_wait_for_session_change", lambda _sub: _noop_coro())
def test_workspace_stream_skip_initial(tmp_path):
    """skip_initial=True should delay first workspace_changed by one cycle."""
    sf = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="claude",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=1,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    events = asyncio.run(_run_stream(sf, session_id, cycles=3, skip_initial=True))
    grouped = _collect_stream_events(events)

    assert "connected" in grouped
    assert len(grouped.get("workspace_changed", [])) >= 1


def test_codex_live_preview_round_trip_post_to_sse(tmp_path):
    """End-to-end: POST /agents/runtime/events/batch → SSE workspace_changed.

    The earlier tests cover the two halves separately (handler→pubsub and
    pubsub→SSE). This one exercises the seam: a real request hitting the route
    publishes a preview that the SSE generator must surface with the same
    cursor and text the bridge posted.
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from zerg.database import get_db
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.services.session_pubsub import reset_pubsub_for_test

    reset_pubsub_for_test()
    sf = _make_db(tmp_path, name="workspace_stream_round_trip.db")
    now = datetime.now(timezone.utc)

    with sf() as db:
        session = AgentSession(
            provider="codex",
            environment="production",
            project="test",
            started_at=now,
            user_messages=1,
            assistant_messages=0,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id

    from zerg.main import api_app

    def override_db():
        db = sf()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(
        device_id="round-trip", id="token-1", owner_id=1
    )

    payload = {
        "events": [
            {
                "runtime_key": f"codex:{session_id}",
                "session_id": str(session_id),
                "provider": "codex",
                "device_id": "cinder",
                "source": "codex_bridge_live",
                "kind": "progress_signal",
                "occurred_at": now.isoformat(),
                "dedupe_key": f"bridge:live:{session_id}:thread-1:turn-1:7",
                "payload": {
                    "progress_kind": "bridge_live_transcript_delta",
                    "managed_transport": "codex_app_server",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "seq": 7,
                    "method": "item/agentMessage/delta",
                    "delta": "g",
                    "live_text": "round trip working",
                    "turn_completed": False,
                },
            }
        ]
    }

    async def _run():
        request = _DisconnectAfterNCycles(8)
        events: list[dict] = []

        async def fire_post():
            await asyncio.sleep(0.01)
            with TestClient(api_app) as client:
                resp = await asyncio.to_thread(
                    client.post,
                    "/agents/runtime/events/batch",
                    json=payload,
                    headers={"X-Agents-Token": "dev"},
                )
            assert resp.status_code == 200, resp.text

        post_task = asyncio.create_task(fire_post())
        try:
            async for event in timeline_mod._session_workspace_stream(
                request,
                session_factory=sf,
                session_id=session_id,
                skip_initial=True,
            ):
                events.append(event)
                if event.get("event") == "workspace_changed":
                    break
        finally:
            await post_task
        return events

    try:
        events = asyncio.run(_run())
    finally:
        api_app.dependency_overrides.clear()

    changed_events = [event for event in events if event["event"] == "workspace_changed"]
    assert len(changed_events) == 1
    changed = json.loads(changed_events[0]["data"])
    preview = changed["transcript_preview"]
    assert preview["text"] == "round trip working"
    assert preview["event_origin"] == "live_provisional"
    assert preview["is_provisional"] is True
    assert preview["content_cursor"] == f"codex_bridge_live:{session_id}:thread-1:turn-1:7"
    assert changed["latest_event_id"] == -7
