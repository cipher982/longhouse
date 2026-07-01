"""Tests for the Cursor Helm live transcript tailer.

These cover the high-water-mark / delta logic that makes live ingest
duplicate-safe (the core of docs/specs/cursor-live-ingest.md), the retry
behavior on post failure, and chat-dir discovery. ``decode_store_db`` is
monkeypatched so the tests do not need to build protobuf store.db fixtures;
the real decode path is covered by test_cursor_transcript.py.
"""

from __future__ import annotations

import os
import threading
import time
import types
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.cli import cursor_helm_ingest as mod
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import SessionIngest


def _ev(role: str, text: str, when: datetime | None = None) -> EventIngest:
    return EventIngest(role=role, content_text=text, timestamp=when or datetime.now(timezone.utc), source_path="/tmp/store.db")


def _session(events: list[EventIngest]) -> SessionIngest:
    return SessionIngest(
        provider="cursor",
        environment="production",
        started_at=datetime.now(timezone.utc),
        provider_session_id="cursor-agent-id",
        events=events,
    )


def test_build_delta_payload_stamps_ordinal_and_advances_hwm():
    session_id = str(uuid4())
    decoded = _session([_ev("user", "a"), _ev("assistant", "b"), _ev("assistant", "c")])
    built = mod._build_delta_payload(session_id, decoded, decoded.events, hwm=1)
    assert built is not None
    payload, new_events = built
    assert str(payload.id) == session_id
    assert len(new_events) == 2
    # ordinals are global (0,1,2); shipping from hwm=1 yields ordinals 1 and 2
    assert [e.source_offset for e in payload.events] == [1, 2]
    # binding fields pass through
    assert payload.provider_session_id == "cursor-agent-id"


def test_build_delta_payload_returns_none_when_nothing_new():
    decoded = _session([_ev("user", "a")])
    assert mod._build_delta_payload(str(uuid4()), decoded, decoded.events, hwm=1) is None
    assert mod._build_delta_payload(str(uuid4()), decoded, decoded.events, hwm=5) is None


def test_run_transcript_tailer_ships_two_deltas_with_stable_ordinals(monkeypatch):
    session_id = str(uuid4())
    posts: list[SessionIngest] = []
    decode_calls = {"n": 0}

    def fake_decode(_path):
        decode_calls["n"] += 1
        # call 1: 2 events; call 2+: 4 events (simulates turns committing)
        n = 2 if decode_calls["n"] == 1 else 4
        return types.SimpleNamespace(session=_session([_ev("user" if i == 0 else "assistant", f"m{i}") for i in range(n)]))

    def fake_post(_url, _token, payload):
        posts.append(payload)
        return True

    monkeypatch.setattr(mod, "decode_store_db", fake_decode)
    monkeypatch.setattr(mod, "_post_delta", fake_post)

    stop = threading.Event()
    t = threading.Thread(
        target=mod.run_transcript_tailer,
        kwargs={"store_db_path": Path("/tmp/x"), "session_id": session_id, "url": "http://x", "token": "t",
                "stop_event": stop, "poll_seconds": 0.05},
        daemon=True,
    )
    t.start()
    try:
        deadline = time.time() + 3.0
        while len(posts) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert len(posts) >= 2, f"expected >=2 posts, got {len(posts)}"
    finally:
        stop.set()
        t.join(timeout=3.0)

    # first delta ships ordinals 0,1; second ships ordinals 2,3 (hwm advanced)
    assert [e.source_offset for e in posts[0].events] == [0, 1]
    assert [e.source_offset for e in posts[1].events] == [2, 3]
    assert str(posts[0].id) == session_id
    assert str(posts[1].id) == session_id


def test_run_transcript_tailer_retries_on_post_failure_then_advances(monkeypatch):
    session_id = str(uuid4())
    posts: list[SessionIngest] = []
    post_calls = {"n": 0}

    def fake_decode(_path):
        return types.SimpleNamespace(session=_session([_ev("user", "a"), _ev("assistant", "b")]))

    def fake_post(_url, _token, payload):
        post_calls["n"] += 1
        posts.append(payload)
        # fail the first attempt, succeed after
        return post_calls["n"] >= 2

    monkeypatch.setattr(mod, "decode_store_db", fake_decode)
    monkeypatch.setattr(mod, "_post_delta", fake_post)

    stop = threading.Event()
    t = threading.Thread(
        target=mod.run_transcript_tailer,
        kwargs={"store_db_path": Path("/tmp/x"), "session_id": session_id, "url": "http://x", "token": "t",
                "stop_event": stop, "poll_seconds": 0.05},
        daemon=True,
    )
    t.start()
    try:
        deadline = time.time() + 3.0
        while post_calls["n"] < 2 and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)  # let any spurious third post happen
    finally:
        stop.set()
        t.join(timeout=3.0)

    # First post failed -> hwm did not advance -> second post re-ships the same
    # ordinals [0,1]. After success, hwm=2 and there is nothing new to ship, so
    # no further posts.
    assert len(posts) == 2
    assert [e.source_offset for e in posts[0].events] == [0, 1]
    assert [e.source_offset for e in posts[1].events] == [0, 1]


def test_discover_store_db_override_env(monkeypatch, tmp_path):
    chat_dir = tmp_path / "chat-xyz"
    chat_dir.mkdir()
    store = chat_dir / "store.db"
    store.write_bytes(b"x")
    monkeypatch.setenv("LH_CURSOR_HELM_CHAT_DIR", str(chat_dir))
    assert mod.discover_store_db(datetime.now(timezone.utc)) == store


def test_discover_store_db_finds_newest_after_launch(tmp_path):
    root = tmp_path / "chats"
    root.mkdir()
    # cursor layout is root/<workspace>/<chat>/store.db (iter_local_cursor_stores
    # globs "*/*/store.db").
    old_dir = root / "ws-old" / "chat-a"
    old_dir.mkdir(parents=True)
    old_store = old_dir / "store.db"
    old_store.write_bytes(b"x")
    old_time = time.time() - 3600
    os.utime(old_dir, (old_time, old_time))
    os.utime(old_store, (old_time, old_time))

    new_dir = root / "ws-new" / "chat-b"
    new_dir.mkdir(parents=True)
    new_store = new_dir / "store.db"
    new_store.write_bytes(b"x")

    launch = datetime.now(timezone.utc)
    found = mod.discover_store_db(launch, cursor_root=root)
    assert found == new_store


def test_discover_store_db_returns_none_when_nothing_new(tmp_path):
    root = tmp_path / "chats"
    root.mkdir()
    old_dir = root / "ws" / "chat"
    old_dir.mkdir(parents=True)
    s = old_dir / "store.db"
    s.write_bytes(b"x")
    old_time = time.time() - 3600
    os.utime(old_dir, (old_time, old_time))
    os.utime(s, (old_time, old_time))
    assert mod.discover_store_db(datetime.now(timezone.utc), cursor_root=root) is None
