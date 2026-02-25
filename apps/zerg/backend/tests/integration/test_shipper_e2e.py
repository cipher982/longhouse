"""Shipper end-to-end integration tests.

Verifies the full pipeline:
  session file on disk → longhouse-engine ship --file → /api/agents/ingest → SQLite DB

Strategy
--------
- Spin up a real uvicorn server against a temp SQLite DB (AUTH_DISABLED=1).
- Run ``longhouse-engine ship --file <fixture>`` as a real subprocess.
- Assert the session + events appear via the REST API.

Fixtures are sanitised real-world session files (no PII):
- ``claude_session.jsonl``   — Claude Code JSONL format
- ``gemini_session.json``    — Gemini CLI JSON format
- ``codex_session.jsonl``    — Codex CLI JSONL format

Marks / skip conditions
-----------------------
- Marked ``integration`` so the normal ``make test`` suite skips them.
- Skipped automatically when ``longhouse-engine`` is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BACKEND_DIR = Path(__file__).parent.parent.parent  # apps/zerg/backend

# Fixture filenames — Claude and Codex use UUID stems so the engine derives
# the session ID directly from the filename.  Gemini uses the sessionId field
# inside the JSON document (parser always prefers the document value).
CLAUDE_FIXTURE = "1dd6c481-7d7b-498a-b492-c33c917889b9.jsonl"
GEMINI_FIXTURE = "gemini_session.json"
CODEX_FIXTURE = "019a4bea-3f39-7fe1-b132-6c14579e806c.jsonl"

# Expected session IDs — must match the fixture files exactly.
CLAUDE_SESSION_ID = "1dd6c481-7d7b-498a-b492-c33c917889b9"
GEMINI_SESSION_ID = "5053c934-f66d-4fea-96af-f95181de5986"
CODEX_SESSION_ID = "019a4bea-3f39-7fe1-b132-6c14579e806c"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 and return the OS-assigned port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, timeout: float = 20.0) -> None:
    """Poll /api/health until the server responds 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{url}/api/health", timeout=1)
            if r.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"Server at {url} did not become ready within {timeout}s")


def _ship(fixture: str, url: str, provider: str, engine_db: Path) -> None:
    """Run ``longhouse-engine ship --file`` against the test server.

    Uses a dedicated ``--db`` path so the engine's spool state is isolated
    from the developer's real spool DB.
    """
    result = subprocess.run(
        [
            "longhouse-engine",
            "ship",
            "--file", str(FIXTURES_DIR / fixture),
            "--url", url,
            "--provider", provider,
            "--db", str(engine_db),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"longhouse-engine exited {result.returncode}\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def _get_session(url: str, session_id: str) -> dict | None:
    """Return the session dict for ``session_id``, or None if not found."""
    r = requests.get(f"{url}/api/agents/sessions/{session_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _get_events(url: str, session_id: str) -> list[dict]:
    """Return all events for a session."""
    r = requests.get(f"{url}/api/agents/sessions/{session_id}/events")
    r.raise_for_status()
    data = r.json()
    # Endpoint returns {"events": [...]}
    return data.get("events", data) if isinstance(data, dict) else data


# ---------------------------------------------------------------------------
# Server fixture (module-scoped — started once, shared across all tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start a real uvicorn server backed by a temp SQLite DB.

    Yields the base URL (e.g. ``http://127.0.0.1:54321``).
    """
    if not shutil.which("longhouse-engine"):
        pytest.skip("longhouse-engine not on PATH — install with: cargo build --release")

    db_path = tmp_path_factory.mktemp("shipper_e2e") / "test.db"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "AUTH_DISABLED": "1",
        "DATABASE_URL": f"sqlite:///{db_path}",
    }

    proc = subprocess.Popen(
        [
            "uv", "run", "--extra", "dev",
            "uvicorn", "zerg.main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_ready(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Claude tests
# ---------------------------------------------------------------------------


class TestClaudeShipping:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship(CLAUDE_FIXTURE, server, "claude", tmp_path / "engine.db")
        session = _get_session(server, CLAUDE_SESSION_ID)
        assert session is not None, "Claude session not found after shipping"
        assert session["provider"] == "claude"

    def test_events_ingested(self, server, tmp_path):
        # Session was already shipped by the previous test (module-scoped server)
        events = _get_events(server, CLAUDE_SESSION_ID)
        assert len(events) >= 2, f"Expected ≥2 events, got {len(events)}"
        roles = {e["role"] for e in events}
        assert "user" in roles
        assert "assistant" in roles

    def test_reship_is_idempotent(self, server, tmp_path):
        """Shipping the same file twice must not duplicate events."""
        events_before = _get_events(server, CLAUDE_SESSION_ID)
        _ship(CLAUDE_FIXTURE, server, "claude", tmp_path / "engine2.db")
        events_after = _get_events(server, CLAUDE_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)} events"
        )


# ---------------------------------------------------------------------------
# Gemini tests
# ---------------------------------------------------------------------------


class TestGeminiShipping:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship(GEMINI_FIXTURE, server, "gemini", tmp_path / "engine.db")
        session = _get_session(server, GEMINI_SESSION_ID)
        assert session is not None, "Gemini session not found after shipping"
        assert session["provider"] == "gemini"

    def test_events_ingested(self, server, tmp_path):
        events = _get_events(server, GEMINI_SESSION_ID)
        assert len(events) >= 2, f"Expected ≥2 events, got {len(events)}"
        roles = {e["role"] for e in events}
        assert "user" in roles
        assert "assistant" in roles

    def test_user_message_content(self, server, tmp_path):
        """The actual user message content from the fixture must be preserved."""
        events = _get_events(server, GEMINI_SESSION_ID)
        user_events = [e for e in events if e["role"] == "user"]
        assert user_events, "No user events found"
        assert "gemini ok" in user_events[0].get("content_text", ""), (
            f"Expected 'gemini ok' in user content, got: {user_events[0].get('content_text')}"
        )

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, GEMINI_SESSION_ID)
        _ship(GEMINI_FIXTURE, server, "gemini", tmp_path / "engine2.db")
        events_after = _get_events(server, GEMINI_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)} events"
        )


# ---------------------------------------------------------------------------
# Codex tests
# ---------------------------------------------------------------------------


class TestCodexShipping:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship(CODEX_FIXTURE, server, "codex", tmp_path / "engine.db")
        session = _get_session(server, CODEX_SESSION_ID)
        assert session is not None, "Codex session not found after shipping"
        assert session["provider"] == "codex"

    def test_events_ingested(self, server, tmp_path):
        events = _get_events(server, CODEX_SESSION_ID)
        assert len(events) >= 2, f"Expected ≥2 events, got {len(events)}"
        roles = {e["role"] for e in events}
        assert "user" in roles
        assert "assistant" in roles

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, CODEX_SESSION_ID)
        _ship(CODEX_FIXTURE, server, "codex", tmp_path / "engine2.db")
        events_after = _get_events(server, CODEX_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)} events"
        )
