"""Shipper end-to-end integration tests.

Verifies the full pipeline:
  session file on disk → longhouse-engine ship --file → /api/agents/ingest → SQLite DB

Strategy
--------
- Spin up a real uvicorn server against a temp SQLite DB (AUTH_DISABLED=1).
- Mint a real device token from the dev browser surface before shipping.
- Run ``longhouse-engine ship --file <fixture>`` using the REPO-LOCAL binary
  (not the one on PATH) so the tests always use the binary built from the
  current source tree.  This prevents stale-binary false confidence.
- Assert the session + events appear via the REST API with exact contract checks.

Fixtures are sanitised real-world session files (no PII):
- ``1dd6c481-....jsonl``   — Claude Code JSONL format
- ``9f0c3c8e-....jsonl``   — Claude non-text tool results
- ``gemini_session.json``  — Gemini CLI JSON format
- ``019a4bea-....jsonl``   — Codex CLI JSONL format
- ``gemini_drift.json``    — Gemini with object-typed content (schema drift)
- ``gemini_tool_results.json`` — Gemini tool call + tool result payloads

Marks / skip conditions
-----------------------
- Marked ``integration`` so the normal ``make test`` suite skips them.
- Skipped automatically when the repo-local engine binary is not built.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
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
BACKEND_DIR = Path(__file__).parent.parent.parent  # server
REPO_ROOT = BACKEND_DIR.parent                     # repo root

# Always use the repo-local binary so tests are coupled to the current source.
_cargo_profile = os.environ.get("CARGO_PROFILE", "release")
ENGINE_BIN = REPO_ROOT / "engine" / "target" / _cargo_profile / "longhouse-engine"

# Fixture filenames.
CLAUDE_FIXTURE = "1dd6c481-7d7b-498a-b492-c33c917889b9.jsonl"
CLAUDE_NON_TEXT_TOOL_RESULTS_FIXTURE = "9f0c3c8e-0b6e-4c2d-9b93-5ab2ebf3e101.jsonl"
GEMINI_FIXTURE = "gemini_session.json"
GEMINI_DRIFT_FIXTURE = "gemini_drift.json"
GEMINI_TOOL_RESULTS_FIXTURE = "gemini_tool_results.json"
CODEX_FIXTURE = "019a4bea-3f39-7fe1-b132-6c14579e806c.jsonl"

# Expected session IDs — must match the fixture files exactly.
CLAUDE_SESSION_ID = "1dd6c481-7d7b-498a-b492-c33c917889b9"
CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID = "9f0c3c8e-0b6e-4c2d-9b93-5ab2ebf3e101"
GEMINI_SESSION_ID = "5053c934-f66d-4fea-96af-f95181de5986"
GEMINI_DRIFT_SESSION_ID = "d1f7b8a2-3e4c-4f56-a789-012345678901"
GEMINI_TOOL_RESULTS_SESSION_ID = "f2b84f4d-9149-4ed8-8d65-9dc0b6b0fbe2"
CODEX_SESSION_ID = "019a4bea-3f39-7fe1-b132-6c14579e806c"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{url}/api/health", timeout=1)
            if r.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            if proc.poll() is not None:
                break
        time.sleep(0.25)

    stderr_tail = ""
    if proc.stderr is not None:
        try:
            stderr_tail = proc.stderr.read().strip()
        except Exception:
            stderr_tail = ""

    detail = f"\nServer stderr:\n{stderr_tail}" if stderr_tail else ""
    raise TimeoutError(f"Server at {url} did not become ready within {timeout}s.{detail}")


def _server_url(server: str | dict[str, str]) -> str:
    return server["url"] if isinstance(server, dict) else server


def _server_token(server: str | dict[str, str]) -> str:
    if isinstance(server, dict):
        return server["token"]
    raise RuntimeError("Device token missing for machine-auth integration test")


def _mint_device_token(url: str) -> str:
    response = requests.post(
        f"{url}/api/devices/tokens",
        json={"device_id": "shipper-e2e"},
        timeout=5,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token")
    assert isinstance(token, str) and token.startswith("zdt_"), payload
    return token


def _ship(fixture: str, server: str | dict[str, str], provider: str, engine_db: Path) -> None:
    """Run ``longhouse-engine ship --file`` using the repo-local binary."""
    url = _server_url(server)
    token = _server_token(server)
    result = subprocess.run(
        [
            str(ENGINE_BIN),
            "ship",
            "--file", str(FIXTURES_DIR / fixture),
            "--url", url,
            "--token", token,
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


def _get_session(server: str | dict[str, str], session_id: str) -> dict | None:
    r = requests.get(
        f"{_server_url(server)}/api/agents/sessions/{session_id}",
        headers={"X-Agents-Token": _server_token(server)},
        timeout=5,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _get_events(server: str | dict[str, str], session_id: str) -> list[dict]:
    r = requests.get(
        f"{_server_url(server)}/api/agents/sessions/{session_id}/events",
        headers={"X-Agents-Token": _server_token(server)},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("events", data) if isinstance(data, dict) else data


def _export_session(server: str | dict[str, str], session_id: str) -> bytes:
    r = requests.get(
        f"{_server_url(server)}/api/agents/sessions/{session_id}/export",
        headers={"X-Agents-Token": _server_token(server)},
        timeout=10,
    )
    r.raise_for_status()
    return r.content


def _sqlite_rows(server: dict[str, str], query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with sqlite3.connect(server["db_path"]) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


# ---------------------------------------------------------------------------
# Server fixture (module-scoped — started once, shared across all tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Start a real uvicorn server backed by a temp SQLite DB."""
    if not ENGINE_BIN.exists():
        pytest.skip(
            f"Repo-local engine binary not found at {ENGINE_BIN}.\n"
            "Run: cd engine && cargo build --release"
        )

    db_path = tmp_path_factory.mktemp("shipper_e2e") / "test.db"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "AUTH_DISABLED": "1",
        "DATABASE_URL": f"sqlite:///{db_path}",
        "LLM_DISABLED": "1",
        "FERNET_SECRET": os.environ.get(
            "FERNET_SECRET",
            base64.urlsafe_b64encode(os.urandom(32)).decode(),
        ),
        "TRIGGER_SIGNING_SECRET": os.environ.get(
            "TRIGGER_SIGNING_SECRET",
            base64.urlsafe_b64encode(os.urandom(32)).decode(),
        ),
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
        _wait_ready(base_url, proc)
        yield {"url": base_url, "token": _mint_device_token(base_url), "db_path": str(db_path)}
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
        assert session["id"] == CLAUDE_SESSION_ID

    def test_events_ingested(self, server, tmp_path):
        events = _get_events(server, CLAUDE_SESSION_ID)
        assert len(events) == 2, f"Expected exactly 2 events, got {len(events)}"

    def test_event_roles_and_content(self, server, tmp_path):
        events = _get_events(server, CLAUDE_SESSION_ID)
        roles = [e["role"] for e in events]
        assert roles == ["user", "assistant"], f"Expected [user, assistant], got {roles}"
        user_content = events[0].get("content_text", "")
        assert "agent" in user_content.lower() or "mcp" in user_content.lower(), (
            f"Unexpected user content: {user_content!r}"
        )
        assistant_content = events[1].get("content_text", "")
        assert assistant_content, "Assistant event must have non-empty content_text"

    def test_timestamps_are_monotonic(self, server, tmp_path):
        events = _get_events(server, CLAUDE_SESSION_ID)
        timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        assert timestamps == sorted(timestamps), (
            f"Event timestamps not monotonically increasing: {timestamps}"
        )

    def test_session_metadata(self, server, tmp_path):
        # Phase 4 of docs/specs/session-liveness-honesty.md: the engine no
        # longer ships ended_at, and ingest no longer seeds it from the
        # last-event timestamp. last_activity_at is the canonical recency
        # field now; ended_at is null until a real terminal_signal lands.
        session = _get_session(server, CLAUDE_SESSION_ID)
        assert session["started_at"] is not None, "started_at must be set"
        assert session["last_activity_at"] is not None, "last_activity_at must be set"
        assert session["user_messages"] >= 1
        assert session["assistant_messages"] >= 1

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, CLAUDE_SESSION_ID)
        _ship(CLAUDE_FIXTURE, server, "claude", tmp_path / "engine2.db")
        events_after = _get_events(server, CLAUDE_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)}"
        )


class TestClaudeNonTextToolResults:
    def test_tool_results_are_persisted_for_non_text_payloads(self, server, tmp_path):
        _ship(CLAUDE_NON_TEXT_TOOL_RESULTS_FIXTURE, server, "claude", tmp_path / "engine.db")
        session = _get_session(server, CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID)
        assert session is not None, "Claude non-text tool-results session not found after shipping"
        assert session["provider"] == "claude"

        events = _get_events(server, CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID)
        assert len(events) == 4, f"Expected exactly 4 events, got {len(events)}"

        results = [e for e in events if e["role"] == "tool"]
        assert len(results) == 2, f"Expected 2 tool result events, got {len(results)}"

        outputs = {e["tool_call_id"]: e.get("tool_output_text") for e in results}
        assert outputs["toolu_bdrk_01IMG"] == "[image result]"
        assert outputs["toolu_bdrk_01REF"] == "[tool references: TaskCreate, TaskUpdate, TaskList]"

    def test_tool_call_id_pairing_survives_non_text_payloads(self, server, tmp_path):
        events = _get_events(server, CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID)
        assistants = [
            e for e in events
            if e["role"] == "assistant" and e.get("tool_name")
        ]
        tools = [e for e in events if e["role"] == "tool"]

        assistant_ids = {e.get("tool_call_id") for e in assistants if e.get("tool_call_id")}
        tool_ids = {e.get("tool_call_id") for e in tools if e.get("tool_call_id")}

        assert assistant_ids == {"toolu_bdrk_01IMG", "toolu_bdrk_01REF"}
        assert tool_ids == assistant_ids

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID)
        _ship(CLAUDE_NON_TEXT_TOOL_RESULTS_FIXTURE, server, "claude", tmp_path / "engine2.db")
        events_after = _get_events(server, CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)}"
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
        assert len(events) == 2, f"Expected exactly 2 events, got {len(events)}"

    def test_event_roles_and_content(self, server, tmp_path):
        events = _get_events(server, GEMINI_SESSION_ID)
        roles = [e["role"] for e in events]
        assert roles == ["user", "assistant"], f"Expected [user, assistant], got {roles}"
        # User message asks to reply with "gemini ok"
        user_content = events[0].get("content_text", "")
        assert "gemini ok" in user_content.lower(), (
            f"Expected 'gemini ok' in user content, got: {user_content!r}"
        )
        # Assistant replied with exactly "gemini ok"
        assistant_content = events[1].get("content_text", "")
        assert assistant_content.strip() == "gemini ok", (
            f"Expected assistant content 'gemini ok', got: {assistant_content!r}"
        )

    def test_timestamps_are_monotonic(self, server, tmp_path):
        events = _get_events(server, GEMINI_SESSION_ID)
        timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        assert timestamps == sorted(timestamps)

    def test_session_metadata(self, server, tmp_path):
        # Phase 4: last_activity_at replaces ended_at as the "saw activity"
        # field; ended_at only gets set on a real terminal_signal.
        session = _get_session(server, GEMINI_SESSION_ID)
        assert session["started_at"] is not None
        assert session["last_activity_at"] is not None

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, GEMINI_SESSION_ID)
        _ship(GEMINI_FIXTURE, server, "gemini", tmp_path / "engine2.db")
        events_after = _get_events(server, GEMINI_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)}"
        )


# ---------------------------------------------------------------------------
# Gemini schema-drift tests (object content field)
# ---------------------------------------------------------------------------


class TestGeminiSchemaDrift:
    """Verify graceful degradation when Gemini uses object-typed content.

    The parser must not drop the entire session just because one message
    has an unexpected content format.  Valid string-content messages must
    still be shipped.
    """

    def test_partial_session_shipped_despite_object_content(self, server, tmp_path):
        """String-content messages survive even when one uses object content."""
        _ship(GEMINI_DRIFT_FIXTURE, server, "gemini", tmp_path / "engine.db")
        session = _get_session(server, GEMINI_DRIFT_SESSION_ID)
        assert session is not None, (
            "Schema-drift session not found. The parser may have dropped the entire session."
        )

    def test_string_content_messages_preserved(self, server, tmp_path):
        events = _get_events(server, GEMINI_DRIFT_SESSION_ID)
        # Fixture has 4 messages: user(str), gemini(obj), user(str), gemini(str)
        # At minimum the 3 string-content messages must survive
        assert len(events) >= 3, (
            f"Expected ≥3 events from drift fixture (string-content messages preserved), "
            f"got {len(events)}.  Object content in one message must not drop others."
        )

    def test_exact_content_of_string_messages(self, server, tmp_path):
        events = _get_events(server, GEMINI_DRIFT_SESSION_ID)
        user_contents = [
            e.get("content_text", "") for e in events if e["role"] == "user"
        ]
        assert any("valid string message" in c for c in user_contents), (
            f"Expected 'valid string message' in user events. Got: {user_contents}"
        )
        assert any("follow-up after object content" in c for c in user_contents), (
            f"Expected follow-up message preserved. Got: {user_contents}"
        )

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, GEMINI_DRIFT_SESSION_ID)
        _ship(GEMINI_DRIFT_FIXTURE, server, "gemini", tmp_path / "engine2.db")
        events_after = _get_events(server, GEMINI_DRIFT_SESSION_ID)
        assert len(events_after) == len(events_before)


# ---------------------------------------------------------------------------
# Gemini tool-results tests (tool_call_id pairing + tool outputs)
# ---------------------------------------------------------------------------


class TestGeminiToolResults:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship(GEMINI_TOOL_RESULTS_FIXTURE, server, "gemini", tmp_path / "engine.db")
        session = _get_session(server, GEMINI_TOOL_RESULTS_SESSION_ID)
        assert session is not None, "Gemini tool-results session not found after shipping"
        assert session["provider"] == "gemini"

    def test_tool_calls_and_results_are_ingested(self, server, tmp_path):
        events = _get_events(server, GEMINI_TOOL_RESULTS_SESSION_ID)
        # user + assistant text + 2 assistant tool calls + 2 tool result events
        assert len(events) == 6, f"Expected exactly 6 events, got {len(events)}"

        tool_results = [e for e in events if e["role"] == "tool"]
        assert len(tool_results) == 2, (
            f"Expected 2 Gemini tool result events, got {len(tool_results)}"
        )
        outputs = [e.get("tool_output_text", "") for e in tool_results]
        assert any("README content" in output for output in outputs), (
            f"Expected README output in tool results. Got: {outputs}"
        )
        assert any("cancelled" in output.lower() for output in outputs), (
            f"Expected cancelled/error output in tool results. Got: {outputs}"
        )

    def test_tool_call_id_pairing(self, server, tmp_path):
        events = _get_events(server, GEMINI_TOOL_RESULTS_SESSION_ID)
        assistants = [
            e for e in events
            if e["role"] == "assistant" and e.get("tool_name")
        ]
        tools = [e for e in events if e["role"] == "tool"]

        assistant_ids = {e.get("tool_call_id") for e in assistants if e.get("tool_call_id")}
        tool_ids = {e.get("tool_call_id") for e in tools if e.get("tool_call_id")}

        assert assistant_ids == {"tc-read", "tc-write"}
        assert tool_ids == {"tc-read", "tc-write"}
        assert assistant_ids == tool_ids, "Gemini tool call/result IDs must align"

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, GEMINI_TOOL_RESULTS_SESSION_ID)
        _ship(GEMINI_TOOL_RESULTS_FIXTURE, server, "gemini", tmp_path / "engine2.db")
        events_after = _get_events(server, GEMINI_TOOL_RESULTS_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)}"
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
        assert len(events) == 2, f"Expected exactly 2 events, got {len(events)}"

    def test_event_roles_and_content(self, server, tmp_path):
        events = _get_events(server, CODEX_SESSION_ID)
        roles = [e["role"] for e in events]
        assert roles == ["user", "assistant"], f"Expected [user, assistant], got {roles}"
        user_content = events[0].get("content_text", "")
        assert "1+1" in user_content, (
            f"Expected '1+1' in user content, got: {user_content!r}"
        )
        assistant_content = events[1].get("content_text", "")
        assert "2" in assistant_content, (
            f"Expected '2' in assistant response, got: {assistant_content!r}"
        )

    def test_timestamps_are_monotonic(self, server, tmp_path):
        events = _get_events(server, CODEX_SESSION_ID)
        timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        assert timestamps == sorted(timestamps)

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, CODEX_SESSION_ID)
        _ship(CODEX_FIXTURE, server, "codex", tmp_path / "engine2.db")
        events_after = _get_events(server, CODEX_SESSION_ID)
        assert len(events_after) == len(events_before), (
            f"Re-ship created duplicates: {len(events_before)} → {len(events_after)}"
        )


def test_full_ship_replays_pending_spool_even_without_new_files(server, tmp_path):
    """One-shot ship should flush existing spool backlog, not only newly discovered files."""

    temp_home = tmp_path / "home"
    (temp_home / ".claude" / "projects").mkdir(parents=True)
    session_id = "7f2c2a10-1111-2222-3333-444455556666"
    session_file = tmp_path / f"{session_id}.jsonl"
    session_file.write_text(
        "\n".join(
            [
                r'{"type":"user","uuid":"spool-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello from spool replay"}}',
                r'{"type":"assistant","uuid":"spool-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"spool replay ok"}]}}',
            ]
        )
        + "\n"
    )
    engine_db = tmp_path / "engine.db"
    env = {**os.environ, "HOME": str(temp_home)}

    queue_result = subprocess.run(
        [
            str(ENGINE_BIN),
            "ship",
            "--file",
            str(session_file),
            "--url",
            "http://127.0.0.1:9",
            "--token",
            "zdt_test",
            "--provider",
            "claude",
            "--db",
            str(engine_db),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert queue_result.returncode == 0, (
        f"initial spooling run failed\nstdout: {queue_result.stdout}\nstderr: {queue_result.stderr}"
    )
    assert _get_session(server, session_id) is None, "spooled session should not reach the API before replay"

    replay_result = subprocess.run(
        [
            str(ENGINE_BIN),
            "ship",
            "--url",
            _server_url(server),
            "--token",
            _server_token(server),
            "--db",
            str(engine_db),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert replay_result.returncode == 0, (
        f"spool replay run failed\nstdout: {replay_result.stdout}\nstderr: {replay_result.stderr}"
    )

    summary_start = replay_result.stdout.find("{")
    assert summary_start >= 0, f"expected JSON summary in stdout, got: {replay_result.stdout!r}"
    summary = json.loads(replay_result.stdout[summary_start:])
    assert summary["files_shipped"] == 0
    assert summary["spool_replayed"] == 1
    assert summary["spool_pending"] == 0

    session = _get_session(server, session_id)
    assert session is not None, "pending spool backlog should replay even when there are no new files to scan"
    assert session["provider"] == "claude"


@pytest.mark.parametrize(
    ("fixture", "provider", "session_id", "expect_event_raw_payload"),
    [
        (CLAUDE_FIXTURE, "claude", CLAUDE_SESSION_ID, True),
        (GEMINI_FIXTURE, "gemini", GEMINI_SESSION_ID, False),
        (CODEX_FIXTURE, "codex", CODEX_SESSION_ID, True),
    ],
)
def test_archival_rows_are_stored_compressed_on_real_ingest(
    server,
    tmp_path,
    fixture: str,
    provider: str,
    session_id: str,
    expect_event_raw_payload: bool,
):
    """Full shipper path must persist archival payloads in codec=1 form."""

    _ship(fixture, server, provider, tmp_path / f"{provider}-storage.db")

    event_rows = _sqlite_rows(
        server,
        "SELECT raw_json, raw_json_z, raw_json_codec FROM events WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    source_line_rows = _sqlite_rows(
        server,
        "SELECT raw_json, raw_json_z, raw_json_codec FROM source_lines WHERE session_id = ? ORDER BY id",
        (session_id,),
    )

    assert event_rows, f"{provider} ingest produced no event rows"
    assert source_line_rows, f"{provider} ingest produced no source_line rows"
    assert all(row["raw_json_codec"] == 1 for row in source_line_rows)
    assert all(row["raw_json_z"] is not None for row in source_line_rows)
    assert all(row["raw_json"] == "" for row in source_line_rows)

    compressed_event_rows = [row for row in event_rows if row["raw_json_z"] is not None]
    legacy_null_event_rows = [row for row in event_rows if row["raw_json_z"] is None]

    assert all(row["raw_json"] is None for row in event_rows)
    assert all(row["raw_json_codec"] == 1 for row in compressed_event_rows)
    assert all(row["raw_json_codec"] == 0 for row in legacy_null_event_rows)
    if expect_event_raw_payload:
        assert len(compressed_event_rows) == len(event_rows), (
            f"{provider} ingest should preserve raw payloads on every event row"
        )


@pytest.mark.parametrize(
    ("fixture", "provider", "session_id"),
    [
        (CLAUDE_FIXTURE, "claude", CLAUDE_SESSION_ID),
        (GEMINI_FIXTURE, "gemini", GEMINI_SESSION_ID),
        (CODEX_FIXTURE, "codex", CODEX_SESSION_ID),
    ],
)
def test_export_roundtrip_matches_original_fixture_bytes(server, tmp_path, fixture: str, provider: str, session_id: str):
    """The exact shipped transcript must remain exportable after compressed ingest."""

    _ship(fixture, server, provider, tmp_path / f"{provider}-export.db")

    exported = _export_session(server, session_id)
    expected = (FIXTURES_DIR / fixture).read_bytes()
    assert exported == expected
