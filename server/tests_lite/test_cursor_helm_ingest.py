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


def test_post_delta_surfaces_permanent_4xx_and_retries_transient_failures(monkeypatch):
    import httpx

    payload = _session([_ev("user", "a")])

    def _resp(status: int):
        return httpx.Response(status_code=status, text="{}", request=httpx.Request("POST", "http://x"))

    cases = {200: True, 201: True, 204: True, 429: False, 500: False, 503: False}
    for status, expected in cases.items():
        captured: dict[str, object] = {}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, endpoint, headers=None, content=None):
                captured["endpoint"] = endpoint
                return _resp(status)

        monkeypatch.setattr(httpx, "Client", _FakeClient)
        got = mod._post_delta("http://x/", "tok", payload)
        assert got is expected, f"status {status}: expected {expected}, got {got}"
        assert captured["endpoint"] == "http://x/api/agents/ingest"

    for status in (400, 401, 403, 422, 426):
        class _RejectingClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *_args, **_kwargs):
                return httpx.Response(
                    status_code=status,
                    text='{"detail":{"code":"storage_v2_required"}}',
                    request=httpx.Request("POST", "http://x"),
                )

        monkeypatch.setattr(httpx, "Client", _RejectingClient)
        with pytest.raises(mod.CursorHelmIngestRejected, match=f"HTTP {status}.*storage_v2_required"):
            mod._post_delta("http://x", "tok", payload)


def test_post_delta_returns_false_on_transport_error(monkeypatch):
    import httpx

    payload = _session([_ev("user", "a")])

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    assert mod._post_delta("http://x", "tok", payload) is False


def test_runtime_compatibility_probe_disables_legacy_tailer_after_storage_v2_cutover(monkeypatch):
    import httpx

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, endpoint, headers=None):
            assert endpoint == "https://longhouse.test/api/agents/storage/v2/capabilities"
            assert headers == {"X-Agents-Token": "tok"}
            return httpx.Response(
                200,
                json={"cutover": True},
                request=httpx.Request("GET", endpoint),
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    ok, error = mod.probe_runtime_ingest_compatibility("https://longhouse.test/", "tok")

    assert ok is False
    assert "requires storage-v2" in str(error)


def test_probe_ingest_path_returns_ok_without_database_url():
    """probe_ingest_path must succeed when DATABASE_URL is unset — it exercises
    the exact import + model-build path the tailer uses, catching the class of
    transitive zerg.database import crash that silently killed Helm ingest."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "FERNET_SECRET"}}
    result = subprocess.run(
        [sys.executable, "-c",
         "from zerg.cli.cursor_helm_ingest import probe_ingest_path; "
         "ok, err = probe_ingest_path(); "
         "import sys; sys.exit(0 if ok else 1); print(err)"],
        cwd=str(repo_root / "server"),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"probe_ingest_path failed without DATABASE_URL (the launch self-check "
        f"would warn falsely, or worse a real regression would hide):\n{result.stderr}"
    )


def test_run_transcript_tailer_records_failures_on_shared_best_effort_logger(monkeypatch):
    """The tailer must record decode failures on the shared BestEffortLogger so
    the launcher can summarize ingest health at exit — not swallow them in a
    bare except: pass."""
    from zerg.utils.log import BestEffortLogger

    session_id = str(uuid4())

    def boom_decode(_path):
        raise RuntimeError("synthetic config crash")

    monkeypatch.setattr(mod, "decode_store_db", boom_decode)
    bf = BestEffortLogger("zerg.test.tailer", every=100)

    stop = threading.Event()
    t = threading.Thread(
        target=mod.run_transcript_tailer,
        kwargs={"store_db_path": Path("/tmp/x"), "session_id": session_id, "url": "http://x",
                "token": "t", "stop_event": stop, "poll_seconds": 0.05, "bf": bf},
        daemon=True,
    )
    t.start()
    try:
        deadline = time.time() + 2.0
        while bf.total_failures < 3 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        t.join(timeout=2.0)

    assert bf.total_failures >= 3, "tailer did not record decode failures on the shared logger"
    assert bf.consecutive_failures >= 3
    assert bf.last_error is not None and "synthetic config crash" in bf.last_error
