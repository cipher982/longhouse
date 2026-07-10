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
- ``antigravity_legacy_session.json``  — legacy Antigravity JSON chat format
- ``019a4bea-....jsonl``   — Codex CLI JSONL format
- ``antigravity_legacy_drift.json``    — legacy Antigravity JSON with object-typed content (schema drift)
- ``antigravity_legacy_tool_results.json`` — legacy Antigravity tool call + tool result payloads

Marks / skip conditions
-----------------------
- Marked ``integration`` so the normal ``make test`` suite skips them.
- Skipped automatically when the repo-local engine binary is not built.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import NAMESPACE_URL
from uuid import uuid4
from uuid import uuid5

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
ANTIGRAVITY_LEGACY_FIXTURE = "antigravity_legacy_session.json"
ANTIGRAVITY_LEGACY_DRIFT_FIXTURE = "antigravity_legacy_drift.json"
ANTIGRAVITY_LEGACY_TOOL_RESULTS_FIXTURE = "antigravity_legacy_tool_results.json"
CODEX_FIXTURE = "019a4bea-3f39-7fe1-b132-6c14579e806c.jsonl"

# Expected session IDs — must match the fixture files exactly.
CLAUDE_SESSION_ID = "1dd6c481-7d7b-498a-b492-c33c917889b9"
CLAUDE_NON_TEXT_TOOL_RESULTS_SESSION_ID = "9f0c3c8e-0b6e-4c2d-9b93-5ab2ebf3e101"
ANTIGRAVITY_LEGACY_SESSION_ID = "5053c934-f66d-4fea-96af-f95181de5986"
ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID = "d1f7b8a2-3e4c-4f56-a789-012345678901"
ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID = "f2b84f4d-9149-4ed8-8d65-9dc0b6b0fbe2"
CODEX_SESSION_ID = "019a4bea-3f39-7fe1-b132-6c14579e806c"
OPENCODE_PROVIDER_SESSION_ID = "ses_longhouse_e2e"
OPENCODE_SESSION_ID = str(uuid5(NAMESPACE_URL, f"opencode:{OPENCODE_PROVIDER_SESSION_ID}"))

pytestmark = [
    pytest.mark.integration,
    # Filesystem observation can legitimately consume the 8-second condition
    # budget before teardown gets its separate 5-second graceful-exit window.
    pytest.mark.timeout(30),
]


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


def _create_opencode_db(home: Path) -> Path:
    db_path = home / ".local" / "share" / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE session (
                id text PRIMARY KEY,
                parent_id text,
                directory text,
                path text,
                title text,
                version text,
                time_created integer NOT NULL,
                time_updated integer NOT NULL
            );
            CREATE TABLE message (
                id text PRIMARY KEY,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY,
                message_id text NOT NULL,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO session (id, parent_id, directory, path, title, version, time_created, time_updated)
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                OPENCODE_PROVIDER_SESSION_ID,
                "/tmp/opencode-work",
                "tmp/opencode-work",
                "OpenCode e2e",
                "1.15.7",
                1_779_000_000_000,
                1_779_000_001_300,
            ),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (
                "msg_user",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_010,
                1_779_000_000_020,
                json.dumps({"role": "user"}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_user",
                "msg_user",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_011,
                1_779_000_000_011,
                json.dumps({"type": "text", "text": "hello from opencode e2e"}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_file",
                "msg_user",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_012,
                1_779_000_000_012,
                json.dumps(
                    {
                        "type": "file",
                        "mime": "image/png",
                        "filename": "clipboard",
                        "url": "data:image/png;base64," + ("A" * 900),
                        "source": {
                            "type": "file",
                            "path": "clipboard",
                            "text": {"value": "[Image 1]", "start": 0, "end": 9},
                        },
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (
                "msg_assistant",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_100,
                1_779_000_001_300,
                json.dumps({"role": "assistant"}),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_tool",
                "msg_assistant",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_110,
                1_779_000_000_190,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_opencode_e2e",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pwd"},
                            "output": "/tmp/opencode-work\n",
                        },
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_patch",
                "msg_assistant",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_000_500,
                1_779_000_000_500,
                json.dumps(
                    {
                        "type": "patch",
                        "hash": "abc123",
                        "files": ["/tmp/opencode-work/a.txt", "/tmp/opencode-work/b.txt"],
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_text",
                "msg_assistant",
                OPENCODE_PROVIDER_SESSION_ID,
                1_779_000_001_200,
                1_779_000_001_300,
                json.dumps({"type": "text", "text": "opencode e2e done"}),
            ),
        )
    return db_path


def _ship_opencode_sqlite(server: str | dict[str, str], tmp_path: Path, engine_db: Path) -> None:
    if isinstance(server, dict):
        home = Path(server["db_path"]).parent / "opencode-home"
    else:
        home = tmp_path / "opencode-home"
    shutil.rmtree(home, ignore_errors=True)
    _create_opencode_db(home)
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [
            str(ENGINE_BIN),
            "ship",
            "--url",
            _server_url(server),
            "--token",
            _server_token(server),
            "--provider",
            "opencode",
            "--db",
            str(engine_db),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
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


def _wait_for_session_events(
    server: str | dict[str, str],
    session_id: str,
    *,
    min_events: int,
    timeout: float = 8.0,
) -> list[dict]:
    http = requests.Session()
    http.trust_env = False
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            session_response = http.get(
                f"{_server_url(server)}/api/agents/sessions/{session_id}",
                headers={"X-Agents-Token": _server_token(server)},
                timeout=1,
            )
            if session_response.status_code != 404:
                session_response.raise_for_status()
                events_response = http.get(
                    f"{_server_url(server)}/api/agents/sessions/{session_id}/events",
                    headers={"X-Agents-Token": _server_token(server)},
                    timeout=1,
                )
                events_response.raise_for_status()
                data = events_response.json()
                events = data.get("events", data) if isinstance(data, dict) else data
                if len(events) >= min_events:
                    return events
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)

    raise AssertionError(f"Timed out waiting for {min_events} events for {session_id}; last_error={last_error!r}")


def _wait_for_log_contains(log_dir: Path, needle: str, *, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    last_text = ""
    while time.monotonic() < deadline:
        texts = []
        for path in sorted(log_dir.glob("engine.log.*")):
            texts.append(path.read_text(errors="replace"))
        last_text = "\n".join(texts)
        if needle in last_text:
            return last_text
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for engine log to contain {needle!r}\n{last_text}")


def _read_engine_logs(log_dir: Path) -> str:
    return "\n".join(path.read_text(errors="replace") for path in sorted(log_dir.glob("engine.log.*")))


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


def _session_ship_traces(server: dict[str, str], session_id: str) -> list[dict]:
    rows = _sqlite_rows(
        server,
        """
        SELECT payload_json
        FROM session_observations
        WHERE session_id = ?
          AND source = 'agents_ingest_trace'
        ORDER BY id ASC
        """,
        (session_id,),
    )
    traces = []
    for row in rows:
        stored = json.loads(row["payload_json"])
        trace = stored.get("payload", {}).get("ship_trace")
        if isinstance(trace, dict):
            traces.append(trace)
    return traces


def _wait_for_ship_trace(
    server: dict[str, str],
    session_id: str,
    *,
    offset: int | None,
    new_offset: int,
    timeout: float = 8.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for trace in _session_ship_traces(server, session_id):
            offset_matches = offset is None or trace.get("offset") == offset
            if offset_matches and trace.get("new_offset") == new_offset:
                return trace
        time.sleep(0.1)
    raise AssertionError(
        f"Timed out waiting for ship trace {session_id} offset={offset} new_offset={new_offset}; "
        f"traces={_session_ship_traces(server, session_id)!r}"
    )


def _start_connect_daemon(
    server: dict[str, str],
    tmp_path: Path,
    *,
    project_name: str,
    machine_name: str,
    create_codex_root: bool = False,
) -> dict:
    session_id = str(uuid4())
    home = tmp_path / "home"
    claude_root = home / ".claude"
    projects_dir = claude_root / "projects" / project_name
    projects_dir.mkdir(parents=True)
    if create_codex_root:
        (home / ".codex" / "sessions").mkdir(parents=True)
    transcript = projects_dir / f"{session_id}.jsonl"
    transcript.touch()
    longhouse_home = Path("/tmp") / f"lh-e2e-{session_id[:8]}"
    log_dir = tmp_path / "logs"

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude_root),
        "LONGHOUSE_HOME": str(longhouse_home),
        "LONGHOUSE_LOG_DIR": str(log_dir),
    }
    proc = subprocess.Popen(
        [
            str(ENGINE_BIN),
            "connect",
            "--url",
            _server_url(server),
            "--token",
            _server_token(server),
            "--db",
            str(tmp_path / "engine.db"),
            "--compression",
            "gzip",
            "--fallback-scan-secs",
            "300",
            "--spool-replay-secs",
            "300",
            "--machine-name",
            machine_name,
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "session_id": session_id,
        "home": home,
        "transcript": transcript,
        "proc": proc,
        "log_dir": log_dir,
        "longhouse_home": longhouse_home,
    }


def _terminate_process(proc: subprocess.Popen[str]) -> str:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    output = ""
    for pipe in (proc.stdout, proc.stderr):
        if pipe is None:
            continue
        try:
            output += pipe.read()
        except Exception:
            pass
    return output


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


def test_connect_daemon_ships_claude_transcript_from_filesystem_watch(server, tmp_path):
    """Daemon connect mode ships a new transcript from the filesystem watcher hot lane."""
    session_id = str(uuid4())
    home = tmp_path / "home"
    claude_root = home / ".claude"
    projects_dir = claude_root / "projects" / "watcher-project"
    projects_dir.mkdir(parents=True)
    transcript = projects_dir / f"{session_id}.jsonl"
    transcript.touch()
    engine_db = tmp_path / "engine.db"
    longhouse_home = Path("/tmp") / f"lh-e2e-{session_id[:8]}"
    log_dir = tmp_path / "logs"

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude_root),
        "LONGHOUSE_HOME": str(longhouse_home),
        "LONGHOUSE_LOG_DIR": str(log_dir),
    }
    proc = subprocess.Popen(
        [
            str(ENGINE_BIN),
            "connect",
            "--url",
            _server_url(server),
            "--token",
            _server_token(server),
            "--db",
            str(engine_db),
            "--compression",
            "gzip",
            "--fallback-scan-secs",
            "300",
            "--spool-replay-secs",
            "300",
            "--machine-name",
            "shipper-e2e-watcher",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_log_contains(log_dir, "Daemon ready")
        fixture_text = (FIXTURES_DIR / CLAUDE_FIXTURE).read_text()
        with transcript.open("a") as f:
            f.write(fixture_text.replace(CLAUDE_SESSION_ID, session_id))
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        events = _wait_for_session_events(server, session_id, min_events=2)
        assert len(events) >= 2
        assert _sqlite_rows(
            server,
            "SELECT COUNT(*) AS count FROM events WHERE session_id = ?",
            (session_id,),
        )[0]["count"] >= 2
    except Exception:
        daemon_output = _terminate_process(proc)
        raise AssertionError(
            f"daemon watcher integration failed\n{daemon_output}\n{_read_engine_logs(log_dir)}"
        ) from None
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        shutil.rmtree(longhouse_home, ignore_errors=True)


def _ask_user_transcript_lines(session_id: str) -> tuple[list[dict], dict]:
    initial_lines = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp/longhouse-test",
            "sessionId": session_id,
            "version": "2.0.76",
            "type": "user",
            "uuid": "ask-user-prompt",
            "timestamp": "2026-01-10T12:00:00.000Z",
            "message": {"role": "user", "content": "Choose the implementation path."},
        },
        {
            "parentUuid": "ask-user-prompt",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp/longhouse-test",
            "sessionId": session_id,
            "version": "2.0.76",
            "type": "assistant",
            "uuid": "ask-user-tool-call",
            "timestamp": "2026-01-10T12:00:10.000Z",
            "message": {
                "id": "msg_ask_user",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ask_user",
                        "name": "AskUserQuestion",
                        "input": {
                            "question": "How should I fix the drag feel?",
                            "choices": ["Use dnd-kit", "Keep inset line"],
                        },
                    }
                ],
                "stop_reason": "tool_use",
            },
        },
    ]
    answer_line = {
        "parentUuid": "ask-user-tool-call",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp/longhouse-test",
        "sessionId": session_id,
        "version": "2.0.76",
        "type": "user",
        "uuid": "ask-user-answer",
        "timestamp": "2026-01-10T12:00:40.000Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_ask_user",
                    "content": "User has answered your questions: Use dnd-kit.",
                }
            ],
        },
    }
    return initial_lines, answer_line


def _write_outbox_presence(
    longhouse_home: Path,
    *,
    session_id: str,
    phase: str,
    transcript: Path,
    index: int,
) -> None:
    outbox_dir = longhouse_home / "agent" / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "provider": "claude",
        "state": phase,
        "tool_name": "AskUserQuestion" if phase == "blocked" else "",
        "cwd": "/tmp/longhouse-test",
        "transcript_path": str(transcript),
    }
    (outbox_dir / f"phase-{index}-{phase}.json").write_text(
        json.dumps(payload, separators=(",", ":")),
        encoding="utf-8",
    )


def _phase_matrix_line(session_id: str, phase: str, index: int) -> dict:
    return {
        "parentUuid": None if index == 0 else f"phase-{index - 1}",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp/longhouse-test",
        "sessionId": session_id,
        "version": "2.0.76",
        "type": "assistant",
        "uuid": f"phase-{index}",
        "timestamp": f"2026-01-10T12:01:{index:02d}.000Z",
        "message": {
            "id": f"msg_phase_{index}",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": f"hot lane append while phase is {phase}",
                }
            ],
        },
    }


def test_connect_daemon_ships_ask_user_answer_append_from_filesystem_watch(server, tmp_path):
    """A blocked AskUserQuestion answer append ships through the filesystem hot lane."""
    daemon = _start_connect_daemon(
        server,
        tmp_path,
        project_name="ask-user-project",
        machine_name="shipper-e2e-ask-user",
    )
    session_id = daemon["session_id"]
    transcript = daemon["transcript"]
    proc = daemon["proc"]
    log_dir = daemon["log_dir"]
    longhouse_home = daemon["longhouse_home"]
    initial_lines, answer_line = _ask_user_transcript_lines(session_id)

    try:
        _wait_for_log_contains(log_dir, "Daemon ready")
        with transcript.open("a") as f:
            for line in initial_lines:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        initial_bytes = transcript.stat().st_size
        initial_events = _wait_for_session_events(server, session_id, min_events=2)
        assert any(
            e["role"] == "assistant"
            and e.get("tool_name") == "AskUserQuestion"
            and e.get("tool_call_id") == "toolu_ask_user"
            for e in initial_events
        ), initial_events

        with transcript.open("a") as f:
            f.write(json.dumps(answer_line, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        final_bytes = transcript.stat().st_size
        events = _wait_for_session_events(server, session_id, min_events=3)
        ask_result = next(
            (
                e for e in events
                if e["role"] == "tool"
                and e.get("tool_call_id") == "toolu_ask_user"
                and "Use dnd-kit" in (e.get("tool_output_text") or e.get("content_text") or "")
            ),
            None,
        )
        assert ask_result is not None, events

        trace = _wait_for_ship_trace(
            server,
            session_id,
            offset=initial_bytes,
            new_offset=final_bytes,
        )
        assert trace["work_context"] == "live_transcript"
        assert trace["observation_source"] == "fsevent"
        assert trace["range_bytes"] == final_bytes - initial_bytes
    except Exception:
        daemon_output = _terminate_process(proc)
        raise AssertionError(
            f"daemon AskUserQuestion watcher integration failed\n{daemon_output}\n{_read_engine_logs(log_dir)}"
        ) from None
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        shutil.rmtree(longhouse_home, ignore_errors=True)


def test_connect_daemon_phase_signals_do_not_gate_filesystem_hot_lane(server, tmp_path):
    """Filesystem appends ship live regardless of provider phase overlays."""
    daemon = _start_connect_daemon(
        server,
        tmp_path,
        project_name="phase-matrix-project",
        machine_name="shipper-e2e-phase-matrix",
    )
    session_id = daemon["session_id"]
    transcript = daemon["transcript"]
    proc = daemon["proc"]
    log_dir = daemon["log_dir"]
    longhouse_home = daemon["longhouse_home"]

    try:
        _wait_for_log_contains(log_dir, "Daemon ready")

        phases = ["thinking", "running", "blocked", "needs_user", "idle"]
        for index, phase in enumerate(phases):
            _write_outbox_presence(
                longhouse_home,
                session_id=session_id,
                phase=phase,
                transcript=transcript,
                index=index,
            )

            offset = transcript.stat().st_size
            with transcript.open("a") as f:
                f.write(
                    json.dumps(
                        _phase_matrix_line(session_id, phase, index),
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                f.flush()
                os.fsync(f.fileno())
            os.utime(transcript, None)
            new_offset = transcript.stat().st_size

            events = _wait_for_session_events(server, session_id, min_events=index + 1)
            assert any(
                f"hot lane append while phase is {phase}" in (e.get("content_text") or "")
                for e in events
            ), events

            trace = _wait_for_ship_trace(
                server,
                session_id,
                offset=offset,
                new_offset=new_offset,
            )
            assert trace["work_context"] == "live_transcript"
            assert trace["observation_source"] == "fsevent"
    except Exception:
        daemon_output = _terminate_process(proc)
        raise AssertionError(
            f"daemon phase matrix watcher integration failed\n{daemon_output}\n{_read_engine_logs(log_dir)}"
        ) from None
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        shutil.rmtree(longhouse_home, ignore_errors=True)


def test_connect_daemon_waits_for_complete_ask_user_answer_line(server, tmp_path):
    """A partial AskUserQuestion answer append does not advance the cursor."""
    daemon = _start_connect_daemon(
        server,
        tmp_path,
        project_name="ask-user-partial-project",
        machine_name="shipper-e2e-ask-user-partial",
    )
    session_id = daemon["session_id"]
    transcript = daemon["transcript"]
    proc = daemon["proc"]
    log_dir = daemon["log_dir"]
    longhouse_home = daemon["longhouse_home"]
    initial_lines, answer_line = _ask_user_transcript_lines(session_id)

    try:
        _wait_for_log_contains(log_dir, "Daemon ready")
        with transcript.open("a") as f:
            for line in initial_lines:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        initial_bytes = transcript.stat().st_size
        _wait_for_session_events(server, session_id, min_events=2)

        answer_jsonl = json.dumps(answer_line, separators=(",", ":")) + "\n"
        split_at = len(answer_jsonl) // 2
        with transcript.open("a") as f:
            f.write(answer_jsonl[:split_at])
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        time.sleep(0.4)
        assert len(_get_events(server, session_id)) == 2
        assert not any(
            trace.get("offset") == initial_bytes
            for trace in _session_ship_traces(server, session_id)
        )

        with transcript.open("a") as f:
            f.write(answer_jsonl[split_at:])
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        final_bytes = transcript.stat().st_size
        events = _wait_for_session_events(server, session_id, min_events=3)
        ask_results = [
            e for e in events
            if e["role"] == "tool"
            and e.get("tool_call_id") == "toolu_ask_user"
            and "Use dnd-kit" in (e.get("tool_output_text") or e.get("content_text") or "")
        ]
        assert len(ask_results) == 1, events

        trace = _wait_for_ship_trace(
            server,
            session_id,
            offset=initial_bytes,
            new_offset=final_bytes,
        )
        assert trace["work_context"] == "live_transcript"
        assert trace["observation_source"] == "fsevent"
        assert trace["range_bytes"] == final_bytes - initial_bytes
    except Exception:
        daemon_output = _terminate_process(proc)
        raise AssertionError(
            f"daemon partial AskUserQuestion watcher integration failed\n{daemon_output}\n{_read_engine_logs(log_dir)}"
        ) from None
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        shutil.rmtree(longhouse_home, ignore_errors=True)


def test_connect_daemon_ships_codex_transcript_from_filesystem_watch(server, tmp_path):
    """Codex transcripts under ~/.codex/sessions use the filesystem watcher hot lane."""
    daemon = _start_connect_daemon(
        server,
        tmp_path,
        project_name="unused-claude-project",
        machine_name="shipper-e2e-codex-watcher",
        create_codex_root=True,
    )
    session_id = daemon["session_id"]
    home = daemon["home"]
    proc = daemon["proc"]
    log_dir = daemon["log_dir"]
    longhouse_home = daemon["longhouse_home"]
    codex_sessions = home / ".codex" / "sessions" / "2026" / "01" / "10"
    codex_sessions.mkdir(parents=True)
    transcript = codex_sessions / f"rollout-2026-01-10T11-00-00-{session_id}.jsonl"
    transcript.touch()

    try:
        _wait_for_log_contains(log_dir, "Daemon ready")
        fixture_text = (FIXTURES_DIR / CODEX_FIXTURE).read_text()
        payload = fixture_text.replace(CODEX_SESSION_ID, session_id)
        with transcript.open("a") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.utime(transcript, None)

        final_bytes = transcript.stat().st_size
        events = _wait_for_session_events(server, session_id, min_events=2)
        assert [event["role"] for event in events[:2]] == ["user", "assistant"]

        trace = _wait_for_ship_trace(
            server,
            session_id,
            offset=None,
            new_offset=final_bytes,
        )
        assert trace["work_context"] == "live_transcript"
        assert trace["observation_source"] == "fsevent"
        assert trace["provider"] == "codex"
        assert trace["range_bytes"] == final_bytes - trace["offset"]
    except Exception:
        daemon_output = _terminate_process(proc)
        raise AssertionError(
            f"daemon Codex watcher integration failed\n{daemon_output}\n{_read_engine_logs(log_dir)}"
        ) from None
    finally:
        if proc.poll() is None:
            _terminate_process(proc)
        shutil.rmtree(longhouse_home, ignore_errors=True)


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
        _ship(ANTIGRAVITY_LEGACY_FIXTURE, server, "antigravity", tmp_path / "engine.db")
        session = _get_session(server, ANTIGRAVITY_LEGACY_SESSION_ID)
        assert session is not None, "Gemini session not found after shipping"
        assert session["provider"] == "antigravity"

    def test_events_ingested(self, server, tmp_path):
        events = _get_events(server, ANTIGRAVITY_LEGACY_SESSION_ID)
        assert len(events) == 2, f"Expected exactly 2 events, got {len(events)}"

    def test_event_roles_and_content(self, server, tmp_path):
        events = _get_events(server, ANTIGRAVITY_LEGACY_SESSION_ID)
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
        events = _get_events(server, ANTIGRAVITY_LEGACY_SESSION_ID)
        timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        assert timestamps == sorted(timestamps)

    def test_session_metadata(self, server, tmp_path):
        # Phase 4: last_activity_at replaces ended_at as the "saw activity"
        # field; ended_at only gets set on a real terminal_signal.
        session = _get_session(server, ANTIGRAVITY_LEGACY_SESSION_ID)
        assert session["started_at"] is not None
        assert session["last_activity_at"] is not None

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, ANTIGRAVITY_LEGACY_SESSION_ID)
        _ship(ANTIGRAVITY_LEGACY_FIXTURE, server, "antigravity", tmp_path / "engine2.db")
        events_after = _get_events(server, ANTIGRAVITY_LEGACY_SESSION_ID)
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
        _ship(ANTIGRAVITY_LEGACY_DRIFT_FIXTURE, server, "antigravity", tmp_path / "engine.db")
        session = _get_session(server, ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID)
        assert session is not None, (
            "Schema-drift session not found. The parser may have dropped the entire session."
        )

    def test_string_content_messages_preserved(self, server, tmp_path):
        events = _get_events(server, ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID)
        # Fixture has 4 messages: user(str), gemini(obj), user(str), gemini(str)
        # At minimum the 3 string-content messages must survive
        assert len(events) >= 3, (
            f"Expected ≥3 events from drift fixture (string-content messages preserved), "
            f"got {len(events)}.  Object content in one message must not drop others."
        )

    def test_exact_content_of_string_messages(self, server, tmp_path):
        events = _get_events(server, ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID)
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
        events_before = _get_events(server, ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID)
        _ship(ANTIGRAVITY_LEGACY_DRIFT_FIXTURE, server, "antigravity", tmp_path / "engine2.db")
        events_after = _get_events(server, ANTIGRAVITY_LEGACY_DRIFT_SESSION_ID)
        assert len(events_after) == len(events_before)


# ---------------------------------------------------------------------------
# Gemini tool-results tests (tool_call_id pairing + tool outputs)
# ---------------------------------------------------------------------------


class TestGeminiToolResults:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship(ANTIGRAVITY_LEGACY_TOOL_RESULTS_FIXTURE, server, "antigravity", tmp_path / "engine.db")
        session = _get_session(server, ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID)
        assert session is not None, "Gemini tool-results session not found after shipping"
        assert session["provider"] == "antigravity"

    def test_tool_calls_and_results_are_ingested(self, server, tmp_path):
        events = _get_events(server, ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID)
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
        events = _get_events(server, ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID)
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
        events_before = _get_events(server, ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID)
        _ship(ANTIGRAVITY_LEGACY_TOOL_RESULTS_FIXTURE, server, "antigravity", tmp_path / "engine2.db")
        events_after = _get_events(server, ANTIGRAVITY_LEGACY_TOOL_RESULTS_SESSION_ID)
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


# ---------------------------------------------------------------------------
# OpenCode SQLite tests
# ---------------------------------------------------------------------------


class TestOpenCodeSQLiteShipping:
    def test_session_appears_in_db(self, server, tmp_path):
        _ship_opencode_sqlite(server, tmp_path, tmp_path / "engine.db")
        session = _get_session(server, OPENCODE_SESSION_ID)
        assert session is not None, "OpenCode SQLite session not found after shipping"
        assert session["provider"] == "opencode"
        assert session["id"] == OPENCODE_SESSION_ID

    def test_events_ingested(self, server, tmp_path):
        events = _get_events(server, OPENCODE_SESSION_ID)
        assert len(events) == 6, f"Expected exactly 6 events, got {len(events)}"

    def test_event_roles_and_content(self, server, tmp_path):
        events = _get_events(server, OPENCODE_SESSION_ID)
        roles = [event["role"] for event in events]
        assert roles == ["user", "user", "assistant", "tool", "assistant", "assistant"]
        contents = [event.get("content_text") or event.get("tool_output_text") or "" for event in events]
        assert "hello from opencode e2e" in contents
        assert "Attached file: [Image 1] (clipboard, image/png)" in contents
        assert "/tmp/opencode-work\n" in contents
        assert "Patch: /tmp/opencode-work/a.txt, /tmp/opencode-work/b.txt" in contents
        assert "opencode e2e done" in contents

    def test_provider_session_id_is_native_opencode_id(self, server, tmp_path):
        rows = _sqlite_rows(
            server,
            """
            SELECT alias_value
            FROM session_thread_aliases
            WHERE provider = ?
              AND alias_kind = ?
              AND alias_value = ?
            """,
            ("opencode", "provider_session_id", OPENCODE_PROVIDER_SESSION_ID),
        )
        assert rows and rows[0]["alias_value"] == OPENCODE_PROVIDER_SESSION_ID

    def test_reship_is_idempotent(self, server, tmp_path):
        events_before = _get_events(server, OPENCODE_SESSION_ID)
        _ship_opencode_sqlite(server, tmp_path, tmp_path / "engine2.db")
        events_after = _get_events(server, OPENCODE_SESSION_ID)
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
        (ANTIGRAVITY_LEGACY_FIXTURE, "antigravity", ANTIGRAVITY_LEGACY_SESSION_ID, False),
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
        (ANTIGRAVITY_LEGACY_FIXTURE, "antigravity", ANTIGRAVITY_LEGACY_SESSION_ID),
        (CODEX_FIXTURE, "codex", CODEX_SESSION_ID),
    ],
)
def test_export_roundtrip_matches_original_fixture_bytes(server, tmp_path, fixture: str, provider: str, session_id: str):
    """The exact shipped transcript must remain exportable after compressed ingest."""

    _ship(fixture, server, provider, tmp_path / f"{provider}-export.db")

    exported = _export_session(server, session_id)
    expected = (FIXTURES_DIR / fixture).read_bytes()
    assert exported == expected
