# ruff: noqa: I001

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import local_health as local_health_cli
from zerg.cli.main import app
from zerg.services import local_health as local_health_service
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path


def _service_info(
    status: str,
    *,
    service_file: str = "/Users/test/Library/LaunchAgents/com.longhouse.shipper.plist",
) -> dict:
    return {
        "platform": "macos",
        "status": status,
        "service_name": "com.longhouse.shipper",
        "service_file": service_file,
        "log_path": "/Users/test/.longhouse/agent/logs/engine.log.*",
    }


def _write_engine_status(tmp_path: Path, *, age_seconds: int = 0, payload: dict | None = None) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    merged = {
        "version": "0.1.0",
        "daemon_pid": 1234,
        "last_ship_at": "2026-04-07T00:00:00Z",
        "spool_pending_count": 0,
        "spool_dead_count": 0,
        "parse_error_count_1h": 0,
        "consecutive_ship_failures": 0,
        "disk_free_bytes": 20 * 1024 * 1024 * 1024,
        "is_offline": False,
        "recent_dead_letters": [],
        "last_updated": "2026-04-07T00:00:00Z",
    }
    if payload:
        merged.update(payload)
    status_path.write_text(json.dumps(merged))
    timestamp = time.time() - age_seconds
    os.utime(status_path, (timestamp, timestamp))


def _write_outbox_file(tmp_path: Path, *, age_seconds: int = 0, name: str = "prs.1.json") -> None:
    outbox_dir = get_agent_outbox_dir(tmp_path)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / name
    path.write_text(json.dumps({"session_id": "sess-1", "state": "thinking"}))
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))


def _write_local_config(
    tmp_path: Path,
    *,
    url: str,
    machine_name: str,
    runner_enabled: bool | None = None,
) -> None:
    machine_dir = tmp_path / "machine"
    machine_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "config_generation": "test-generation",
        "runtime_url": url,
        "machine_name": machine_name,
        "written_by": "test",
        "written_at": "2026-04-14T00:00:00Z",
    }
    if runner_enabled is not None:
        payload["runner_enabled"] = runner_enabled
    (machine_dir / "state.json").write_text(json.dumps(payload))


def _write_runner_env(tmp_path: Path, *, url: str, runner_name: str) -> Path:
    env_path = tmp_path / "runner.env"
    env_path.write_text(
        "\n".join(
            [
                f"LONGHOUSE_URL={url}",
                f"RUNNER_NAME={runner_name}",
                "RUNNER_SECRET=test-secret",
                "RUNNER_INSTALL_MODE=desktop",
            ]
        )
        + "\n"
    )
    return env_path


def _write_service_plist(
    tmp_path: Path,
    *,
    machine_name: str,
    config_generation: str | None = None,
    state_hash: str | None = None,
) -> Path:
    path = tmp_path / "com.longhouse.shipper.plist"
    payload = {
        "Label": "com.longhouse.shipper",
        "ProgramArguments": [
            "/Users/test/.local/bin/longhouse-engine",
            "connect",
            "--machine-name",
            machine_name,
        ],
    }
    if config_generation or state_hash:
        env = {}
        if config_generation:
            env["LONGHOUSE_MACHINE_GENERATION"] = config_generation
        if state_hash:
            env["LONGHOUSE_MACHINE_STATE_HASH"] = state_hash
        payload["EnvironmentVariables"] = env
    path.write_bytes(plistlib.dumps(payload))
    return path


def _disable_real_runner_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [tmp_path / "missing-runner.env"])
    # Stub the live process scan by default so tests don't pick up the dev
    # box's real Claude/Codex processes. Tests that want process-scan output
    # override this explicitly.
    monkeypatch.setattr(
        local_health_service,
        "_collect_managed_sessions_by_process",
        lambda *, now, existing_session_ids, phase_overlay=None: [],
    )


def _write_shipper_db(tmp_path: Path, rows: list[tuple[str, str, str | None, str | None, str]]) -> None:
    db_path = get_agent_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE file_state (
            path TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            queued_offset INTEGER NOT NULL DEFAULT 0,
            acked_offset INTEGER NOT NULL DEFAULT 0,
            session_id TEXT,
            provider_session_id TEXT,
            last_updated TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO file_state (path, provider, session_id, provider_session_id, last_updated)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _write_session_binding_rows(tmp_path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    db_path = get_agent_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_binding (
            path TEXT NOT NULL,
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO session_binding (path, session_id, provider, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _write_session_phase_rows(
    tmp_path: Path,
    rows: list[tuple[str, str, str, str | None, str, str]],
) -> None:
    db_path = get_agent_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_phase_state (
            session_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            phase TEXT NOT NULL,
            tool_name TEXT,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO session_phase_state (session_id, provider, phase, tool_name, source, observed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            provider = excluded.provider,
            phase = excluded.phase,
            tool_name = excluded.tool_name,
            source = excluded.source,
            observed_at = excluded.observed_at
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _write_codex_bridge_state(state_dir: Path, session_id: str, payload: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{session_id}.json"
    path.write_text(json.dumps(payload))
    # Also create the sidecar lock file so _bridge_is_alive's flock probe can
    # find it. Tests stub `_bridge_is_alive` to return True instead of
    # actually holding a lock — creating the lock file here just prevents
    # the probe's "no lock file = stale, purge" branch from running.
    (state_dir / f"{session_id}.lock").touch()
    return path


def _stub_bridge_alive(monkeypatch, alive: bool = True) -> None:
    monkeypatch.setattr(local_health_service, "_bridge_is_alive", lambda _path: alive)


def test_collect_local_health_healthy(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["severity"] == "green"
    assert snapshot["headline"] == "Longhouse shipping healthy"
    assert snapshot["engine_status"]["fresh"] is True
    assert snapshot["activity_summary"]["exists"] is False
    assert snapshot["launch_readiness"]["state"] == "unconfigured"


def test_collect_local_health_degraded_while_waiting_for_first_status(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert "engine_status_missing" in snapshot["reasons"]
    assert "first local status update" in snapshot["headline"].lower()


def test_collect_local_health_degraded_when_status_is_aging(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=90)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert "engine_status_aging" in snapshot["reasons"]
    assert "aging" in snapshot["headline"].lower()


def test_collect_local_health_flags_detached_managed_session(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-detached", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-detached",
        {
            "session_id": "sess-detached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:31:00Z",
            "thread_path": str(rollout_path),
            "last_turn_status": "completed",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-detached"},
            {"pid": 7772, "ppid": 7771, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert snapshot["headline"] == "Managed session is running in background"
    assert "managed_session_detached" in snapshot["reasons"]
    assert snapshot["managed_summary"] == {
        "attached_count": 0,
        "detached_count": 1,
        "degraded_count": 0,
        "orphan_bridge_count": 0,
        "latest_activity_at": "2026-04-17T17:31:00Z",
    }
    assert snapshot["managed_sessions"] == [
        {
            "session_id": "sess-detached",
            "provider": "codex",
            "workspace_label": "zerg",
            "branch": None,
            "state": "detached",
            "phase": None,
            "phase_observed_at": None,
            "last_activity_at": "2026-04-17T17:31:00Z",
            "bridge_status": "ready",
            "bridge_pid": 7771,
            "bridge_heartbeat_at": "2026-04-17T17:31:00Z",
            "reason_codes": [],
        }
    ]
    assert snapshot["orphan_bridges"] == []


def test_collect_local_health_flags_orphaned_managed_bridge(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-orphan",
        {
            "session_id": "sess-orphan",
            "pid": 8881,
            "ws_url": "ws://127.0.0.1:49888",
            "cwd": "/Users/test/git/citi",
            "status": "ready",
            "updated_at": "2026-04-17T18:02:00Z",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8881, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-orphan"},
            {"pid": 8882, "ppid": 8881, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert snapshot["headline"] == "Longhouse has orphaned managed sessions"
    assert "orphaned_managed_bridge" in snapshot["reasons"]
    assert snapshot["managed_summary"] == {
        "attached_count": 0,
        "detached_count": 0,
        "degraded_count": 0,
        "orphan_bridge_count": 1,
        "latest_activity_at": "2026-04-17T18:02:00Z",
    }
    assert snapshot["managed_sessions"] == []
    assert snapshot["orphan_bridges"] == [
        {
            "session_id": "sess-orphan",
            "provider": "codex",
            "pid": 8881,
            "workspace_label": "citi",
            "status": "orphan",
            "started_at": "2026-04-17T18:02:00Z",
            "heartbeat_at": "2026-04-17T18:02:00Z",
            "reason_codes": ["no_managed_session_bound"],
        }
    ]


def test_collect_local_health_uses_local_phase_overlay_for_codex_bridge_session(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    pinned_now = datetime(2026, 4, 17, 17, 32, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: pinned_now)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-attached", "codex", "2026-04-17T17:30:36Z")],
    )
    _write_session_phase_rows(
        tmp_path,
        [
            (
                "sess-attached",
                "codex",
                "blocked",
                "shell",
                "codex_bridge",
                "2026-04-17T17:31:30Z",
            )
        ],
    )

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-attached",
        {
            "session_id": "sess-attached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:31:00Z",
            "thread_path": str(rollout_path),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-attached"},
            {"pid": 7772, "ppid": 7771, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/Users/test/.longhouse/runtimes/codex/current/longhouse-codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_sessions"][0]["phase"] == "blocked on shell"
    assert snapshot["managed_sessions"][0]["phase_observed_at"] == "2026-04-17T17:31:30Z"
    assert snapshot["managed_sessions"][0]["last_activity_at"] == "2026-04-17T17:31:30Z"


def test_collect_local_health_recognizes_remote_tui_attach_without_resume_token(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-attached", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-attached",
        {
            "session_id": "sess-attached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:31:00Z",
            "thread_path": str(rollout_path),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-attached"},
            {"pid": 7772, "ppid": 7771, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/Users/test/.longhouse/runtimes/codex/current/longhouse-codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_summary"]["attached_count"] == 1
    assert snapshot["managed_summary"]["detached_count"] == 0
    assert snapshot["managed_sessions"][0]["state"] == "attached"


def test_collect_local_health_does_not_flag_missing_rollout_before_first_turn(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-missing.jsonl"
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-bad-thread", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-bad-thread",
        {
            "session_id": "sess-bad-thread",
            "pid": 8881,
            "ws_url": "ws://127.0.0.1:49888",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T18:02:00Z",
            "thread_path": str(rollout_path),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8881, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-bad-thread"},
            {"pid": 8882, "ppid": 8881, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8883,
                "ppid": 8000,
                "command": "/Users/test/.longhouse/runtimes/codex/current/longhouse-codex --enable tui_app_server --remote ws://127.0.0.1:49888",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["headline"] == "Longhouse shipping healthy"
    assert snapshot["managed_summary"]["attached_count"] == 1
    assert snapshot["managed_summary"]["degraded_count"] == 0
    assert snapshot["managed_sessions"][0]["state"] == "attached"
    assert snapshot["managed_sessions"][0]["reason_codes"] == []


def test_collect_local_health_marks_missing_rollout_thread_as_degraded_after_turn_activity(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-missing.jsonl"
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-bad-thread", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".claude" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-bad-thread",
        {
            "session_id": "sess-bad-thread",
            "pid": 8881,
            "ws_url": "ws://127.0.0.1:49888",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T18:02:00Z",
            "thread_path": str(rollout_path),
            "active_turn_id": "turn-live",
            "last_turn_status": "inProgress",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8881, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-bad-thread"},
            {"pid": 8882, "ppid": 8881, "command": "longhouse-codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8883,
                "ppid": 8000,
                "command": "/Users/test/.longhouse/runtimes/codex/current/longhouse-codex --enable tui_app_server --remote ws://127.0.0.1:49888",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["headline"] == "Longhouse lost managed session control"
    assert snapshot["managed_summary"]["degraded_count"] == 1
    assert snapshot["managed_summary"]["detached_count"] == 0
    assert snapshot["managed_sessions"][0]["state"] == "degraded"
    assert snapshot["managed_sessions"][0]["reason_codes"] == ["thread_subscription_failed"]


def test_collect_local_health_broken_when_service_stopped_with_stuck_outbox(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("stopped"))
    _write_local_config(tmp_path, url="https://demo.longhouse.test", machine_name="cinder")
    _write_outbox_file(tmp_path, age_seconds=300)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert "service_stopped" in snapshot["reasons"]
    assert "outbox_stuck" in snapshot["reasons"]
    assert "Run: longhouse machine reconcile" in snapshot["suggested_actions"]


def test_collect_local_health_flags_missing_shipper_state_without_suggesting_reconcile(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    service_file = _write_service_plist(tmp_path, machine_name="cinder")
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("stopped", service_file=str(service_file)),
    )
    _write_local_config(tmp_path, url="https://demo.longhouse.test", machine_name="cinder")

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert "shipper_state_missing" in snapshot["reasons"]
    assert snapshot["headline"] == "Longhouse shipper state is missing"
    assert f"Inspect or restore shipper state: {get_agent_db_path(tmp_path)}" in snapshot["suggested_actions"]
    assert "Run: longhouse machine reconcile" not in snapshot["suggested_actions"]


def test_collect_local_health_broken_when_launch_config_disagrees(monkeypatch, tmp_path: Path):
    service_file = _write_service_plist(tmp_path, machine_name="cinder.local")
    _write_shipper_db(tmp_path, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(
        tmp_path,
        url="http://127.0.0.1:8080",
        machine_name="cinder.local",
        runner_enabled=True,
    )
    _write_engine_status(tmp_path, age_seconds=5)
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert snapshot["launch_readiness"]["state"] == "broken"
    assert "config_url_runner_url_mismatch" in snapshot["reasons"]
    assert "machine_name_runner_name_mismatch" in snapshot["reasons"]
    assert "launch config" in snapshot["headline"].lower()
    assert "Run: longhouse machine reconcile" in snapshot["suggested_actions"]


def test_collect_local_health_ignores_runner_drift_when_runner_not_enabled(monkeypatch, tmp_path: Path):
    service_file = _write_service_plist(tmp_path, machine_name="cinder.local")
    _write_shipper_db(tmp_path, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(tmp_path, url="http://127.0.0.1:8080", machine_name="cinder.local")
    _write_engine_status(tmp_path, age_seconds=5)
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["launch_readiness"]["state"] == "ready"
    assert "config_url_runner_url_mismatch" not in snapshot["reasons"]
    assert "machine_name_runner_name_mismatch" not in snapshot["reasons"]
    assert snapshot["launch_readiness"]["runner_expected"] is False


def test_collect_local_health_flags_service_generation_drift(monkeypatch, tmp_path: Path):
    _write_local_config(tmp_path, url="https://demo.longhouse.test", machine_name="cinder")
    service_file = _write_service_plist(
        tmp_path,
        machine_name="cinder",
        config_generation="stale-generation",
        state_hash="stale-hash",
    )
    _write_shipper_db(tmp_path, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    _write_engine_status(tmp_path, age_seconds=5)
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    _disable_real_runner_env(monkeypatch, tmp_path)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["launch_readiness"]["state"] == "broken"
    assert "service_generation_mismatch" in snapshot["reasons"]
    assert "service_state_hash_mismatch" in snapshot["reasons"]
    assert snapshot["launch_readiness"]["service_config_generation"] == "stale-generation"
    assert snapshot["launch_readiness"]["service_state_hash"] == "stale-hash"


def test_collect_local_health_ignores_invalid_stored_url(monkeypatch, tmp_path: Path):
    service_file = _write_service_plist(tmp_path, machine_name="test-box")
    _write_shipper_db(tmp_path, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(
        tmp_path,
        url="https://<typer.models.OptionInfo object at 0x1234>",
        machine_name="test-box",
    )
    _write_engine_status(tmp_path, age_seconds=5)
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["launch_readiness"]["stored_url"] is None
    assert "config_url_runner_url_mismatch" not in snapshot["reasons"]
    assert "machine_state_missing_runtime_url" in snapshot["reasons"]
    assert "Run: longhouse connect --install" in snapshot["suggested_actions"]


def test_local_health_command_json_output(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path / ".longhouse", age_seconds=2)

    result = runner.invoke(app, ["local-health", "--json", "--claude-dir", str(tmp_path / ".claude")])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["health_state"] == "healthy"
    assert payload["service"]["status"] == "running"
    assert payload["engine_status"]["exists"] is True
    assert "activity_summary" in payload
    assert payload["launch_readiness"]["state"] == "unconfigured"


def test_collect_local_health_includes_activity_summary(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    now = datetime(2026, 4, 12, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    local_now = now.astimezone()
    start_of_day_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    recent = now - timedelta(minutes=4)
    earlier_today = now - timedelta(hours=2)
    before_today = start_of_day_local.astimezone(timezone.utc) - timedelta(minutes=5)

    _write_shipper_db(
        tmp_path,
        [
            ("/tmp/claude-a.jsonl", "claude", "claude-a", None, recent.isoformat()),
            ("/tmp/codex-b.jsonl", "codex", None, "codex-b", earlier_today.isoformat()),
            ("/tmp/gemini-c.jsonl", "gemini", "gemini-c", None, before_today.isoformat()),
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)
    activity = snapshot["activity_summary"]

    assert activity["exists"] is True
    assert activity["error"] is None
    assert activity["sessions_today"] == 2
    assert activity["sessions_recent"] == 1
    assert activity["provider_counts_today"] == {"claude": 1, "codex": 1}
    assert activity["provider_counts_recent"] == {"claude": 1}
    assert activity["latest_activity_at"] == recent.isoformat()
    assert activity["recent_window_minutes"] == local_health_service.ACTIVITY_RECENT_MINUTES
    assert activity["session_recency_bands"] == [
        {"label": "0-1m", "session_count": 0},
        {"label": "1-5m", "session_count": 1},
        {"label": "5-15m", "session_count": 0},
        {"label": "15-60m", "session_count": 0},
        {"label": "1-6h", "session_count": 1},
        {"label": "6h+", "session_count": 0},
    ]
    assert activity["recent_touches"] == [
        {
            "provider": "claude",
            "last_updated": recent.isoformat(),
            "workspace_label": None,
            "branch": None,
            "is_subagent": False,
        },
        {
            "provider": "codex",
            "last_updated": earlier_today.isoformat(),
            "workspace_label": None,
            "branch": None,
            "is_subagent": False,
        },
        {
            "provider": "gemini",
            "last_updated": before_today.isoformat(),
            "workspace_label": None,
            "branch": None,
            "is_subagent": False,
        },
    ]


def test_collect_local_health_recent_touches_use_workspace_context_and_ignore_meta_files(
    monkeypatch, tmp_path: Path
):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    now = datetime(2026, 4, 12, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)

    claude_session = tmp_path / "projects" / "-Users-davidrose-git-crims" / "claude-session.jsonl"
    claude_session.parent.mkdir(parents=True, exist_ok=True)
    claude_session.write_text(
        "\n".join(
            [
                json.dumps({"type": "system"}),
                json.dumps({"type": "assistant"}),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "cwd": "/Users/davidrose/git/crims",
                            "gitBranch": "feature/recent-activity",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    codex_session = tmp_path / "sessions" / "2026" / "04" / "12" / "rollout-zerg.jsonl"
    codex_session.parent.mkdir(parents=True, exist_ok=True)
    codex_session.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "cwd": "/Users/davidrose/git/zerg",
                    "git": {"branch": "main"},
                },
            }
        )
        + "\n"
    )

    ignored_meta = tmp_path / "projects" / "-Users-davidrose-git-crims" / "claude-session.meta.json"
    ignored_meta.write_text("{}\n")

    _write_shipper_db(
        tmp_path,
        [
            (str(claude_session), "claude", "claude-crims", None, (now - timedelta(minutes=2)).isoformat()),
            (str(codex_session), "codex", None, "codex-zerg", (now - timedelta(minutes=4)).isoformat()),
            (str(ignored_meta), "claude", "claude-meta", None, (now - timedelta(minutes=1)).isoformat()),
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)
    activity = snapshot["activity_summary"]

    assert activity["sessions_recent"] == 2
    assert activity["provider_counts_recent"] == {"claude": 1, "codex": 1}
    assert activity["recent_touches"] == [
        {
            "provider": "claude",
            "last_updated": (now - timedelta(minutes=2)).isoformat(),
            "workspace_label": "crims",
            "branch": "feature/recent-activity",
            "is_subagent": False,
        },
        {
            "provider": "codex",
            "last_updated": (now - timedelta(minutes=4)).isoformat(),
            "workspace_label": "zerg",
            "branch": "main",
            "is_subagent": False,
        },
    ]


def test_local_health_menubar_requires_installed_app(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    calls: list[dict[str, object]] = []

    def fake_run(command, check, cwd):
        calls.append({"command": command, "check": check, "cwd": cwd})

    monkeypatch.setattr(local_health_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(local_health_cli, "get_zerg_url", lambda config_dir=None: "https://demo.longhouse.test")
    monkeypatch.setattr(local_health_cli, "_prebuilt_runtime_artifact", lambda component: None)

    result = runner.invoke(
        app,
        [
            "local-health",
            "--claude-dir",
            str(tmp_path / ".claude"),
            "menubar",
            "--refresh-seconds",
            "7",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "connect --install" in result.output
    assert calls == []


def test_local_health_window_launch_without_url(monkeypatch):
    runner = CliRunner()
    calls: list[list[str]] = []

    def fake_run(command, check, cwd):
        calls.append(command)

    monkeypatch.setattr(local_health_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(local_health_cli, "get_zerg_url", lambda config_dir=None: None)
    monkeypatch.setattr(local_health_cli, "_resolve_local_runtime_url", lambda claude_dir=None: None)
    monkeypatch.setattr(local_health_cli, "_prebuilt_runtime_artifact", lambda component: None)

    result = runner.invoke(app, ["local-health", "window"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    command = calls[0]
    assert "LonghouseMenuBarHarnessApp" in command
    assert "--health-exec" in command
    assert "--ui-url" not in command


def test_local_health_menubar_uses_prebuilt_binary_when_installed(monkeypatch):
    runner = CliRunner()
    calls: list[list[str]] = []

    def fake_run(command, check, cwd):
        calls.append(command)

    monkeypatch.setattr(local_health_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        local_health_cli,
        "_prebuilt_runtime_artifact",
        lambda component: SimpleNamespace(
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        ),
    )
    monkeypatch.setattr(local_health_cli, "get_zerg_url", lambda config_dir=None: "https://longhouse.ai")

    result = runner.invoke(app, ["local-health", "menubar"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == "/Applications/Longhouse.app/Contents/MacOS/Longhouse"
    assert "--health-exec" in calls[0]
    assert "swift" not in calls[0]


# ---------------------------------------------------------------------------
# local-health surfaces the CLI update cache as bundle update state. The CLI's
# upgrade path now reconciles engine + Codex automatically, so CLI version is
# a faithful proxy for the local runtime bundle.
# ---------------------------------------------------------------------------


def _write_update_cache(
    longhouse_home: Path,
    *,
    update_available: bool,
    installed: str = "0.1.8",
    latest: str = "0.1.9",
) -> Path:
    longhouse_home.mkdir(parents=True, exist_ok=True)
    cache = {
        "checked_at": "2026-04-11T10:00:00+00:00",
        "installed_version": installed,
        "latest_version": latest,
        "update_available": update_available,
        "upgrade_command": "uv tool upgrade longhouse",
        "install_method": "uv",
        "install_source": "pypi",
        "package_name": "longhouse",
        "error": None,
    }
    path = longhouse_home / "update-check.json"
    path.write_text(json.dumps(cache))
    return path


def test_collect_local_health_surfaces_update_info_from_cli_cache(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    longhouse_home = tmp_path / ".longhouse"
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setattr("zerg.cli.update_manager.current_installed_version", lambda: "0.1.8")
    _write_engine_status(tmp_path, age_seconds=5)
    _write_update_cache(longhouse_home, update_available=True, installed="0.1.8", latest="0.1.9")

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["update_info"] == {
        "installed_version": "0.1.8",
        "latest_version": "0.1.9",
        "update_available": True,
        "upgrade_command": "uv tool upgrade longhouse",
        "checked_at": "2026-04-11T10:00:00+00:00",
        "supported": True,
        "reason": None,
    }


def test_collect_local_health_ignores_stale_update_cache_when_version_moved(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    longhouse_home = tmp_path / ".longhouse"
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    # Already on 0.1.11 but cache still reflects a 0.1.8 check — do not nag.
    monkeypatch.setattr("zerg.cli.update_manager.current_installed_version", lambda: "0.1.11")
    _write_engine_status(tmp_path, age_seconds=5)
    _write_update_cache(longhouse_home, update_available=True, installed="0.1.8", latest="0.1.9")

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["update_info"]["update_available"] is False
    assert snapshot["update_info"]["installed_version"] == "0.1.11"
    assert snapshot["update_info"]["supported"] is True


def test_update_info_present_in_json_cli_output(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    longhouse_home = tmp_path / ".longhouse"
    monkeypatch.setenv("LONGHOUSE_HOME", str(longhouse_home))
    monkeypatch.setattr("zerg.cli.update_manager.current_installed_version", lambda: "0.1.8")
    _write_engine_status(tmp_path / ".longhouse", age_seconds=5)
    _write_update_cache(longhouse_home, update_available=True)

    runner = CliRunner()
    result = runner.invoke(app, ["local-health", "--json", "--claude-dir", str(tmp_path / ".claude")])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["update_info"]["update_available"] is True
    assert payload["update_info"]["latest_version"] == "0.1.9"
    assert payload["update_info"]["upgrade_command"] == "uv tool upgrade longhouse"
    assert payload["update_info"]["supported"] is True


# ----------------------------------------------------------------------------
# Process-scan managed session detection
# ----------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for psutil.Process for process_iter fixtures."""

    def __init__(
        self,
        *,
        pid: int,
        cmdline: list[str],
        create_time: float,
        env: dict | None = None,
        cwd: str | None = None,
        real_uid: int | None = None,
        env_raises: bool = False,
        cwd_raises: bool = False,
    ) -> None:
        self.info = {"pid": pid, "cmdline": cmdline, "create_time": create_time}
        self._env = env
        self._cwd = cwd
        self._real_uid = real_uid if real_uid is not None else os.getuid()
        self._env_raises = env_raises
        self._cwd_raises = cwd_raises

    def uids(self):
        return SimpleNamespace(real=self._real_uid)

    def environ(self):
        import psutil

        if self._env_raises:
            raise psutil.AccessDenied()
        return self._env or {}

    def cwd(self):
        import psutil

        if self._cwd_raises:
            raise psutil.AccessDenied()
        return self._cwd


def _patch_process_iter(monkeypatch, procs: list[_FakeProc]) -> None:
    import psutil

    def fake_iter(_attrs=None):
        return iter(procs)

    monkeypatch.setattr(psutil, "process_iter", fake_iter)


def test_process_scan_detects_claude_via_env(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=55507,
        cmdline=["claude", "--dangerously-skip-permissions", "--session-id", "bfb567fb-7e0f-4552-8411-24f682751484"],
        create_time=now.timestamp(),
        env={
            "LONGHOUSE_MANAGED_SESSION_ID": "bfb567fb-7e0f-4552-8411-24f682751484",
            "LONGHOUSE_DEVICE_ID": "device-abc",
            "LONGHOUSE_HOOK_TOKEN": "zdt_secret_do_not_leak",
        },
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "bfb567fb-7e0f-4552-8411-24f682751484"
    assert row["provider"] == "claude"
    assert row["pid"] == 55507
    assert row["cwd"] == "/Users/test/git/zerg"
    assert row["workspace_label"] == "zerg"
    assert row["device_id"] == "device-abc"
    assert row["state"] == "attached"
    blob = json.dumps(row)
    assert "zdt_secret_do_not_leak" not in blob
    assert "LONGHOUSE_HOOK_TOKEN" not in blob


def test_process_scan_uses_phase_overlay_when_available(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    session_id = "bfb567fb-7e0f-4552-8411-24f682751484"
    proc = _FakeProc(
        pid=55507,
        cmdline=["claude", "--session-id", session_id],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": session_id},
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])
    _write_session_phase_rows(
        tmp_path,
        [
            (
                session_id,
                "claude",
                "running",
                "Bash",
                "claude_hook",
                "2026-04-19T00:04:00Z",
            )
        ],
    )

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now,
        existing_session_ids=set(),
        phase_overlay=local_health_service._load_managed_session_phase_overlay(tmp_path, now=now),
    )

    assert len(rows) == 1
    assert rows[0]["phase"] == "running Bash"
    assert rows[0]["phase_observed_at"] == "2026-04-19T00:04:00Z"
    assert rows[0]["last_activity_at"] == "2026-04-19T00:04:00Z"


def test_phase_overlay_drops_stale_rows_past_freshness_window(monkeypatch, tmp_path: Path):
    """Old running/thinking rows from a prior runtime must not show phantom phases.

    Phase-specific freshness: `running` is 10 minutes. A row older than that
    should be suppressed so the menu bar renders "Phase unknown" instead of an
    out-of-date activity label.
    """
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    stale_observed_at = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    _write_session_phase_rows(
        tmp_path,
        [
            (
                "sess-stale",
                "claude",
                "running",
                "Bash",
                "claude_hook",
                stale_observed_at,
            )
        ],
    )

    overlay = local_health_service._load_managed_session_phase_overlay(tmp_path, now=now)

    assert "sess-stale" not in overlay


def test_phase_overlay_prefers_newer_outbox_signal(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime.now(tz=timezone.utc)
    _write_session_phase_rows(
        tmp_path,
        [
            (
                "sess-1",
                "claude",
                "idle",
                None,
                "claude_hook",
                (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            )
        ],
    )
    _write_outbox_file(tmp_path, age_seconds=0, name="prs.sess-outbox.json")

    overlay = local_health_service._load_managed_session_phase_overlay(tmp_path, now=now)

    assert overlay["sess-1"]["phase"] == "thinking"
    assert overlay["sess-1"]["source"] == "claude_hook"


def test_process_scan_falls_back_to_argv_when_env_empty(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=9001,
        cmdline=["claude", "--session-id", "11111111-2222-3333-4444-555555555555"],
        create_time=now.timestamp(),
        env_raises=True,
        cwd="/Users/test/launchctl-session",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )

    assert len(rows) == 1
    assert rows[0]["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert rows[0]["provider"] == "claude"
    assert rows[0]["device_id"] is None


def test_process_scan_skips_unmanaged_bare_cli(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=11466,
        cmdline=["claude", "--dangerously-skip-permissions"],
        create_time=now.timestamp(),
        env={},  # no LONGHOUSE_MANAGED_SESSION_ID, no argv session-id
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert rows == []


def test_process_scan_skips_other_user_processes(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=777,
        cmdline=["claude", "--session-id", "11111111-2222-3333-4444-555555555555"],
        create_time=now.timestamp(),
        real_uid=os.getuid() + 1,  # different user
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert rows == []


def test_process_scan_dedupes_against_existing_bridge_ids(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=8881,
        cmdline=["/opt/codex", "codex-bridge"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "sess-codex-1"},
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids={"sess-codex-1"}
    )
    assert rows == []


def test_process_scan_rejects_empty_env_session_id(monkeypatch):
    """Empty-string LONGHOUSE_MANAGED_SESSION_ID must fall through to argv fallback."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=10001,
        cmdline=["claude", "--session-id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": ""},
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert len(rows) == 1
    assert rows[0]["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_process_scan_rejects_whitespace_only_env_session_id(monkeypatch):
    """Whitespace session id should be treated as absent, not valid."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=10002,
        cmdline=["claude"],  # no argv fallback either
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "   \t\n"},
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert rows == []


def test_process_scan_rejects_non_uuid_argv_session_id(monkeypatch):
    """--session-id with a non-UUID value must not be accepted."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=10003,
        cmdline=["claude", "--session-id", "not-a-uuid"],
        create_time=now.timestamp(),
        env={},
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert rows == []


def test_process_scan_env_wins_over_argv(monkeypatch):
    """When both env and argv carry a session id, env is authoritative."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=10004,
        cmdline=["claude", "--session-id", "argv1234-5678-4444-8888-aaaaaaaaaaaa"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "env11111-2222-4333-8444-555566667777"},
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert len(rows) == 1
    assert rows[0]["session_id"] == "env11111-2222-4333-8444-555566667777"


def test_process_scan_continues_past_access_denied_env(monkeypatch):
    """AccessDenied on one proc's environ() must not abort the scan."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    blocked = _FakeProc(
        pid=20001,
        cmdline=["claude", "--session-id", "11111111-1111-4111-8111-111111111111"],
        create_time=now.timestamp(),
        env_raises=True,  # argv fallback still works
    )
    visible = _FakeProc(
        pid=20002,
        cmdline=["claude"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "22222222-2222-4222-8222-222222222222"},
    )
    _patch_process_iter(monkeypatch, [blocked, visible])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    session_ids = {row["session_id"] for row in rows}
    assert session_ids == {
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    }


def test_process_scan_dedupes_duplicate_session_ids(monkeypatch):
    """Two claude procs advertising the same session id -> first wins, no duplicate row."""
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    shared = "33333333-3333-4333-8333-333333333333"
    first = _FakeProc(
        pid=30001,
        cmdline=["claude"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": shared},
        cwd="/Users/test/first",
    )
    second = _FakeProc(
        pid=30002,
        cmdline=["claude"],
        create_time=now.timestamp() + 1,
        env={"LONGHOUSE_MANAGED_SESSION_ID": shared},
        cwd="/Users/test/second",
    )
    _patch_process_iter(monkeypatch, [first, second])

    rows = local_health_service._collect_managed_sessions_by_process(
        now=now, existing_session_ids=set()
    )
    assert len(rows) == 1
    assert rows[0]["pid"] == 30001
    assert rows[0]["cwd"] == "/Users/test/first"


def test_merge_bridge_row_wins_on_session_id_collision():
    bridge_row = {
        "session_id": "shared-sid",
        "provider": "codex",
        "workspace_label": "zerg",
        "state": "attached",
        "bridge_status": "ready",
        "bridge_pid": 7777,
        "last_activity_at": "2026-04-19T00:00:00Z",
        "reason_codes": [],
    }
    process_row = {
        "session_id": "shared-sid",
        "provider": "codex",
        "pid": 9999,
        "workspace_label": "zerg",
        "cwd": "/Users/test/git/zerg",
        "state": "attached",
        "bridge_status": None,  # process scan can't see this
        "last_activity_at": "2026-04-19T00:00:30Z",
        "reason_codes": [],
    }
    process_only_row = {
        "session_id": "claude-sid",
        "provider": "claude",
        "pid": 1234,
        "state": "attached",
        "last_activity_at": "2026-04-19T00:00:15Z",
        "reason_codes": [],
    }

    summary, sessions, orphans = local_health_service._merge_managed_sessions(
        bridge_summary={"latest_activity_at": "2026-04-19T00:00:00Z"},
        bridge_sessions=[bridge_row],
        bridge_orphans=[],
        process_sessions=[process_row, process_only_row],
    )

    assert orphans == []
    by_sid = {row["session_id"]: row for row in sessions}
    # bridge wins — keeps bridge_status
    assert by_sid["shared-sid"]["bridge_status"] == "ready"
    assert by_sid["shared-sid"]["bridge_pid"] == 7777
    # process-only row is appended
    assert by_sid["claude-sid"]["provider"] == "claude"
    assert summary["attached_count"] == 2


def test_merge_returns_none_summary_when_nothing_present():
    summary, sessions, orphans = local_health_service._merge_managed_sessions(
        bridge_summary=None,
        bridge_sessions=[],
        bridge_orphans=[],
        process_sessions=[],
    )
    assert summary is None
    assert sessions == []
    assert orphans == []


def test_collect_local_health_reports_claude_managed_session_via_process_scan(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    monkeypatch.setattr(
        local_health_service,
        "_collect_managed_sessions_by_process",
        lambda *, now, existing_session_ids, phase_overlay=None: [
            {
                "session_id": "bfb567fb-7e0f-4552-8411-24f682751484",
                "provider": "claude",
                "pid": 55507,
                "workspace_label": "zerg",
                "cwd": "/Users/test/git/zerg",
                "device_id": "device-abc",
                "started_at": "2026-04-19T00:00:00Z",
                "branch": None,
                "state": "attached",
                "phase": "waiting for input",
                "last_activity_at": "2026-04-19T00:00:00Z",
                "bridge_status": None,
                "bridge_pid": None,
                "bridge_heartbeat_at": None,
                "reason_codes": [],
            }
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_summary"]["attached_count"] == 1
    assert snapshot["managed_sessions"][0]["provider"] == "claude"
    assert snapshot["managed_sessions"][0]["session_id"] == "bfb567fb-7e0f-4552-8411-24f682751484"
    assert snapshot["orphan_bridges"] == []


def test_bridge_is_alive_detects_held_flock(tmp_path: Path) -> None:
    """A held flock on the sidecar means the bridge is alive."""
    import fcntl

    state_file = tmp_path / "sess-live.json"
    state_file.write_text("{}")
    lock_path = state_file.with_suffix(".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert local_health_service._bridge_is_alive(state_file) is True
        # Probe must not have deleted live bridge's files.
        assert state_file.exists()
        assert lock_path.exists()
    finally:
        os.close(fd)


def test_bridge_is_alive_purges_stale_files_when_lock_acquirable(tmp_path: Path) -> None:
    """A free flock means the bridge is gone; probe cleans up all sidecars."""
    state_file = tmp_path / "sess-dead.json"
    state_file.write_text("{}")
    lock_path = state_file.with_suffix(".lock")
    sock_path = state_file.with_suffix(".sock")
    lock_path.touch()
    sock_path.touch()

    assert local_health_service._bridge_is_alive(state_file) is False
    assert not state_file.exists()
    assert not lock_path.exists()
    assert not sock_path.exists()


def test_bridge_is_alive_purges_when_lock_missing(tmp_path: Path) -> None:
    """Legacy bridges (pre-flock) have no lock sidecar — treat as stale."""
    state_file = tmp_path / "sess-legacy.json"
    state_file.write_text("{}")

    assert local_health_service._bridge_is_alive(state_file) is False


def test_phase_freshness_local_health_matches_session_runtime() -> None:
    """Drift guard: the two copies of the phase freshness map must agree.

    `_PHASE_FRESHNESS_SECONDS` in local_health.py is duplicated from
    `PHASE_FRESHNESS` in session_runtime.py because the CLI path cannot
    import the server runtime module (it transitively requires
    `DATABASE_URL`). This test catches drift between the two copies.
    """
    from zerg.services.session_runtime import PHASE_FRESHNESS

    local_copy = local_health_service._PHASE_FRESHNESS_SECONDS
    for phase, seconds in local_copy.items():
        assert phase in PHASE_FRESHNESS, f"{phase} missing from session_runtime.PHASE_FRESHNESS"
        assert seconds == int(PHASE_FRESHNESS[phase].total_seconds()), (
            f"{phase}: local={seconds}s vs runtime={int(PHASE_FRESHNESS[phase].total_seconds())}s"
        )
    for phase in PHASE_FRESHNESS:
        assert phase in local_copy, f"{phase} missing from local_health._PHASE_FRESHNESS_SECONDS"
