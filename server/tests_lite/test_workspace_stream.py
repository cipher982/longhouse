"""Tests for the per-session workspace SSE stream."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import zerg.dependencies.auth as _auth_deps  # noqa: F401 — triggers settings init
import zerg.routers.timeline as timeline_mod
from zerg.database import make_engine, make_sessionmaker
from zerg.database import Base
from zerg.models.agents import AgentEvent, AgentSession, SessionRuntimeState


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
        request, session_factory=sf, session_id=session_id, skip_initial=skip_initial,
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
            request, session_factory=sf, session_id=session_id, skip_initial=False,
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    grouped = _collect_stream_events(events)

    assert len(grouped.get("workspace_changed", [])) == 2, (
        f"Expected 2 workspace_changed events, got {len(grouped.get('workspace_changed', []))}"
    )


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
            request, session_factory=sf, session_id=session_id, skip_initial=False,
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
