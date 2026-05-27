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

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import managed_phase_contract
from zerg import provider_release_status
from zerg.cli import local_health as local_health_cli
from zerg.cli import local_health_fast
from zerg.cli.main import app
from zerg.services import local_health as local_health_service
from zerg.services import session_runtime
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path
from zerg.services.machine_state import MachineState
from zerg.services.machine_state import machine_state_source_hash

_REAL_COMPUTE_PROCESS_SNAPSHOT = local_health_service._compute_process_snapshot
_REAL_SCAN_PROVIDER_PROCESSES = local_health_service._scan_provider_processes
_REAL_COLLECT_MANAGED_SESSIONS_BY_PROCESS = local_health_service._collect_managed_sessions_by_process


@pytest.fixture(autouse=True)
def _stub_process_snapshot_by_default(monkeypatch):
    monkeypatch.setattr(local_health_service, "_compute_process_snapshot", lambda: ([], []))


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


def _local_health_fixture_path(name: str) -> Path:
    return Path(__file__).parent / "fixtures" / "local_health" / name


def _load_local_health_fixture(name: str):
    return json.loads(_local_health_fixture_path(name).read_text())


def _fast_local_health_contract_projection(snapshot: dict) -> dict:
    def managed_row(row: dict) -> dict:
        return {
            "session_id": row.get("session_id"),
            "provider": row.get("provider"),
            "provider_session_id": row.get("provider_session_id"),
            "control_path": row.get("control_path"),
            "liveness_model": row.get("liveness_model"),
            "workspace_label": row.get("workspace_label"),
            "cwd": row.get("cwd"),
            "branch": row.get("branch"),
            "state": row.get("state"),
            "raw_phase": row.get("raw_phase"),
            "phase": row.get("phase"),
            "phase_observed_at": row.get("phase_observed_at"),
            "last_activity_at": row.get("last_activity_at"),
            "bridge_status": row.get("bridge_status"),
            "bridge_pid": row.get("bridge_pid"),
            "app_server_pid": row.get("app_server_pid"),
            "thread_subscription_status": row.get("thread_subscription_status"),
            "reason_codes": row.get("reason_codes"),
            "evidence": row.get("evidence"),
        }

    def unmanaged_row(row: dict) -> dict:
        return {
            "provider": row.get("provider"),
            "control_path": row.get("control_path"),
            "liveness_model": row.get("liveness_model"),
            "pid": row.get("pid"),
            "workspace_label": row.get("workspace_label"),
            "cwd": row.get("cwd"),
            "branch": row.get("branch"),
            "started_at": row.get("started_at"),
            "provider_session_id": row.get("provider_session_id"),
            "source_path": row.get("source_path"),
            "observed_at": row.get("observed_at"),
            "evidence": row.get("evidence"),
        }

    return {
        "collection_tier": snapshot["collection_tier"],
        "health_state": snapshot["health_state"],
        "severity": snapshot["severity"],
        "headline": snapshot["headline"],
        "reasons": snapshot["reasons"],
        "managed_summary": snapshot["managed_summary"],
        "managed_sessions": [managed_row(row) for row in snapshot["managed_sessions"]],
        "unmanaged_processes": [unmanaged_row(row) for row in snapshot["unmanaged_processes"]],
    }


def _write_outbox_file(tmp_path: Path, *, age_seconds: int = 0, name: str = "prs.1.json") -> None:
    outbox_dir = get_agent_outbox_dir(tmp_path)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / name
    path.write_text(json.dumps({"session_id": "sess-1", "state": "thinking"}))
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))


def _write_outbox_phase_signal(
    tmp_path: Path,
    *,
    session_id: str,
    state: str,
    provider: str = "claude",
    tool_name: str | None = None,
    occurred_at: str = "2026-04-19T00:04:30Z",
    name: str = "prs.phase.json",
) -> None:
    outbox_dir = get_agent_outbox_dir(tmp_path)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    (outbox_dir / name).write_text(
        json.dumps(
            {
                "session_id": session_id,
                "state": state,
                "provider": provider,
                "tool_name": tool_name,
                "occurred_at": occurred_at,
            }
        )
    )


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


def _machine_state_hash(*, url: str, machine_name: str) -> str | None:
    return machine_state_source_hash(
        MachineState(
            schema_version=1,
            runtime_url=url,
            machine_name=machine_name,
        )
    )


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


def _load_managed_phase_contract() -> list[managed_phase_contract.ManagedPhaseDefinition]:
    return list(managed_phase_contract.managed_phase_definitions())


def _contract_tool_name(case: managed_phase_contract.ManagedPhaseDefinition) -> str | None:
    return "Bash" if case.tool_display_format else None


def _disable_real_runner_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [tmp_path / "missing-runner.env"])
    for env_name in (
        local_health_service.CODEX_BIN_ENV,
        local_health_service.OPENCODE_BIN_ENV,
        local_health_service.ANTIGRAVITY_BIN_ENV,
        provider_release_status.CODEX_RELEASE_STATUS_FILE_ENV,
        provider_release_status.CODEX_RELEASE_STATUS_URL_ENV,
        provider_release_status.PROVIDER_RELEASE_STATUS_DIR_ENV,
        provider_release_status.PROVIDER_RELEASE_STATUS_URL_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    # Stub the live process scan by default so tests don't pick up the dev
    # box's real Claude/Codex processes. Tests that want process-scan output
    # override this explicitly.
    monkeypatch.setattr(local_health_service, "_compute_process_snapshot", lambda: ([], []))
    monkeypatch.setattr(local_health_service, "_scan_provider_processes", lambda: [])
    monkeypatch.setattr(
        local_health_service,
        "_collect_managed_sessions_by_process",
        lambda *, existing_session_ids, phase_overlay=None, scanned_processes=None: [],
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


def _write_managed_session_state_rows(
    tmp_path: Path,
    rows: list[tuple[str, str, str | None, str | None, str, str | None, str, str, str | None]],
) -> None:
    db_path = get_agent_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_session_state (
            session_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            workspace_path TEXT,
            workspace_label TEXT,
            phase_kind TEXT,
            tool_name TEXT,
            phase_source TEXT,
            phase_observed_at TEXT,
            last_activity_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO managed_session_state (
            session_id,
            provider,
            workspace_path,
            workspace_label,
            phase_kind,
            tool_name,
            phase_source,
            phase_observed_at,
            last_activity_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            provider = excluded.provider,
            workspace_path = excluded.workspace_path,
            workspace_label = excluded.workspace_label,
            phase_kind = excluded.phase_kind,
            tool_name = excluded.tool_name,
            phase_source = excluded.phase_source,
            phase_observed_at = excluded.phase_observed_at,
            last_activity_at = excluded.last_activity_at,
            updated_at = excluded.updated_at
        """,
        [
            (
                session_id,
                provider,
                workspace_path,
                workspace_label,
                phase_kind,
                tool_name,
                phase_source,
                phase_observed_at,
                last_activity_at,
                phase_observed_at,
            )
            for (
                session_id,
                provider,
                workspace_path,
                workspace_label,
                phase_kind,
                tool_name,
                phase_source,
                phase_observed_at,
                last_activity_at,
            ) in rows
        ],
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
    assert snapshot["transport_health"]["status"] == "healthy"
    assert snapshot["transport_health"]["status_summary"] == "Shipping healthy."
    assert snapshot["activity_summary"]["exists"] is False
    assert snapshot["launch_readiness"]["state"] == "unconfigured"


def test_collect_local_health_surfaces_control_channel_status(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "control_channel": {
                "enabled": True,
                "status": "disconnected",
                "ws_url": "wss://david010.longhouse.ai/api/agents/control/ws",
                "last_error_code": "connect_failed",
                "last_error_message": "tls handshake failed",
                "reconnect_backoff_seconds": 4,
                "supports": ["codex.send", "codex.launch"],
            }
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    control = snapshot["control_channel"]
    assert control["status"] == "disconnected"
    assert control["can_launch_codex"] is False
    assert control["launch_blocked_by"] == "control_down"
    assert control["last_error_code"] == "connect_failed"
    assert control["supports"] == ["codex.send", "codex.launch"]


def test_collect_local_health_marks_connected_control_channel_launch_ready(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "control_channel": {
                "enabled": True,
                "status": "connected",
                "ws_url": "wss://david010.longhouse.ai/api/agents/control/ws",
                "supports": ["codex.launch"],
            }
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["control_channel"]["can_launch_codex"] is True
    assert snapshot["control_channel"]["launch_blocked_by"] is None


def test_collect_local_health_ignores_fresh_outbox_file(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    _write_outbox_file(tmp_path, age_seconds=0)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert "outbox_backlog" not in snapshot["reasons"]
    assert "outbox_stuck" not in snapshot["reasons"]


def test_collect_local_health_ignores_short_ephemeral_outbox_backlog(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    _write_outbox_file(tmp_path, age_seconds=30)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert "outbox_backlog" not in snapshot["reasons"]
    assert "outbox_stuck" not in snapshot["reasons"]


def test_collect_local_health_degrades_for_old_outbox_file(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    _write_outbox_file(tmp_path, age_seconds=90)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert "outbox_stuck" in snapshot["reasons"]


def test_collect_local_health_surfaces_codex_provider_cli(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else None,
    )
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["codex"] == {
        "path": "/opt/homebrew/bin/codex",
        "source": "PATH",
        "resolution_error": None,
        "env_override": None,
    }


def test_collect_local_health_degrades_for_blocked_provider_release(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(
        local_health_service,
        "collect_provider_release_status",
        lambda provider_clis, *, fast: {
            "schema_version": 1,
            "enabled": True,
            "blocking_count": 1,
            "warning_count": 0,
            "statuses": {
                "codex": {
                    "status": "blocked",
                    "verdict": "red",
                    "failure_code": "managed_tui_attach_active_thread_error",
                    "artifact_version": "0.133.0",
                    "current_version": "codex-cli 0.133.0",
                    "local_version_matches": True,
                }
            },
        },
    )
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert snapshot["headline"] == "Installed provider release is blocked"
    assert "provider_release_blocked" in snapshot["reasons"]
    assert snapshot["provider_release_status"]["statuses"]["codex"]["status"] == "blocked"


def test_collect_local_health_degrades_for_provider_release_warning(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "collect_provider_release_status",
        lambda provider_clis, *, fast: {
            "schema_version": 1,
            "enabled": True,
            "blocking_count": 0,
            "warning_count": 1,
            "statuses": {"codex": {"status": "unknown_for_current_version", "risk": "warning"}},
        },
    )
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert snapshot["headline"] == "Provider release status needs attention"
    assert "provider_release_warning" in snapshot["reasons"]


def test_collect_local_health_surfaces_opencode_provider_cli(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/opencode" if name == "opencode" else None,
    )
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["opencode"] == {
        "path": "/opt/homebrew/bin/opencode",
        "source": "PATH",
        "resolution_error": None,
        "env_override": None,
    }


def test_collect_local_health_surfaces_antigravity_provider_cli(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service.shutil,
        "which",
        lambda name: "/Users/test/.local/bin/agy" if name == "agy" else None,
    )
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["antigravity"] == {
        "path": "/Users/test/.local/bin/agy",
        "source": "PATH",
        "resolution_error": None,
        "env_override": None,
    }


def test_collect_local_health_surfaces_missing_opencode_provider_cli(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(local_health_service.shutil, "which", lambda name: None)
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["opencode"] == {
        "path": None,
        "source": "missing",
        "resolution_error": "`opencode` not found on PATH",
        "env_override": None,
    }


def test_collect_local_health_surfaces_missing_antigravity_provider_cli(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(local_health_service.shutil, "which", lambda name: None)
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["antigravity"] == {
        "path": None,
        "source": "missing",
        "resolution_error": "`agy` not found on PATH",
        "env_override": None,
    }


def test_collect_local_health_surfaces_opencode_env_override(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    opencode_bin = tmp_path / "opencode"
    opencode_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    opencode_bin.chmod(0o755)
    monkeypatch.setenv(local_health_service.OPENCODE_BIN_ENV, str(opencode_bin))
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["opencode"] == {
        "path": str(opencode_bin),
        "source": local_health_service.OPENCODE_BIN_ENV,
        "resolution_error": None,
        "env_override": str(opencode_bin),
    }


def test_collect_local_health_surfaces_antigravity_env_override(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    antigravity_bin = tmp_path / "agy"
    antigravity_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    antigravity_bin.chmod(0o755)
    monkeypatch.setenv(local_health_service.ANTIGRAVITY_BIN_ENV, str(antigravity_bin))
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["antigravity"] == {
        "path": str(antigravity_bin),
        "source": local_health_service.ANTIGRAVITY_BIN_ENV,
        "resolution_error": None,
        "env_override": str(antigravity_bin),
    }


def test_collect_local_health_surfaces_missing_codex_env_override(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setenv(local_health_service.CODEX_BIN_ENV, "/missing/codex")
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["provider_clis"]["codex"] == {
        "path": None,
        "source": local_health_service.CODEX_BIN_ENV,
        "resolution_error": "LONGHOUSE_CODEX_BIN did not resolve to an executable",
        "env_override": "/missing/codex",
    }


def test_collect_local_health_degraded_while_waiting_for_first_status(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert "engine_status_missing" in snapshot["reasons"]
    assert snapshot["transport_health"] is None
    assert "first local status update" in snapshot["headline"].lower()


def test_collect_local_health_uses_shared_transport_burst_classifier(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "ship_attempts_1h": 20,
            "ship_successes_1h": 15,
            "ship_connect_errors_1h": 5,
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert "connect_errors" in snapshot["reasons"]
    assert snapshot["transport_health"]["status"] == "degraded"
    assert snapshot["transport_health"]["status_reason"] == "connect_errors"
    assert snapshot["transport_health"]["status_summary"] == "5 ship connect error(s) in the last hour."


def test_collect_local_health_keeps_recovered_transient_connect_errors_healthy(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "ship_attempts_1h": 14,
            "ship_successes_1h": 12,
            "ship_connect_errors_1h": 2,
            "last_ship_result": "ok",
            "consecutive_ship_failures": 0,
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["transport_health"]["status"] == "healthy"
    assert "connect_errors" not in snapshot["reasons"]


def test_collect_local_health_uses_active_transport_window_when_present(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "ship_attempts_1h": 32,
            "ship_successes_1h": 20,
            "ship_connect_errors_1h": 12,
            "ship_attempts_10m": 4,
            "ship_successes_10m": 4,
            "ship_connect_errors_10m": 0,
            "last_ship_result": "ok",
            "consecutive_ship_failures": 0,
            "spool_pending_count": 0,
            "spool_dead_count": 0,
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["transport_health"]["status"] == "healthy"
    assert snapshot["transport_health"]["ship_connect_errors_1h"] == 12
    assert snapshot["transport_health"]["ship_connect_errors_10m"] == 0
    assert "connect_errors" not in snapshot["reasons"]


def test_collect_local_health_includes_last_transport_error_detail(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(
        tmp_path,
        age_seconds=5,
        payload={
            "ship_attempts_1h": 20,
            "ship_successes_1h": 18,
            "ship_connect_errors_1h": 2,
            "last_ship_result": "connect_error",
            "last_ship_error_kind": "timeout",
            "last_ship_error_message": "request timed out after 60s",
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["transport_health"]["status"] == "degraded"
    assert (
        snapshot["transport_health"]["status_summary"]
        == "2 ship connect error(s) in the last hour. Last error: timeout."
    )
    assert snapshot["transport_health"]["last_ship_error_kind"] == "timeout"
    assert snapshot["transport_health"]["last_ship_error_message"] == "request timed out after 60s"


def test_collect_local_health_flags_non_object_engine_status_payload(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text('"broken"')

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert "engine_status_unreadable" in snapshot["reasons"]
    assert snapshot["transport_health"] is None


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

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-detached",
        {
            "session_id": "sess-detached",
            "pid": 7771,
            "codex_bin": "/opt/homebrew/bin/codex",
            "launch_mode": "detached_ui",
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
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
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
            "control_path": "managed",
            "liveness_model": "codex_bridge",
            "provider_cli": {"path": "/opt/homebrew/bin/codex", "source": "bridge_state"},
            "workspace_label": "zerg",
            "branch": None,
            "state": "detached",
            "raw_phase": None,
            "phase": None,
            "phase_observed_at": None,
            "last_activity_at": "2026-04-17T17:31:00Z",
            "bridge_status": "ready",
            "bridge_pid": 7771,
            "bridge_heartbeat_at": "2026-04-17T17:31:00Z",
            "thread_subscription_status": None,
            "thread_subscription_attempts": 0,
            "thread_subscription_last_error": None,
            "reason_codes": [],
        }
    ]
    assert snapshot["orphan_bridges"] == []


def test_collect_local_health_treats_detached_ui_ready_codex_bridge_as_attached(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "05" / "13" / "rollout-detached-ui.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-detached-ui", "codex", "2026-05-13T23:59:36Z")],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-detached-ui",
        {
            "session_id": "sess-detached-ui",
            "pid": 7771,
            "app_server_pid": 7772,
            "codex_bin": "/opt/homebrew/bin/codex",
            "launch_mode": "detached_ui",
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "thread_id": "thread-detached-ui",
            "thread_path": str(rollout_path),
            "updated_at": "2026-05-13T23:59:39Z",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-detached-ui"},
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_summary"]["attached_count"] == 1
    assert snapshot["managed_summary"]["detached_count"] == 0
    assert snapshot["managed_summary"]["degraded_count"] == 0
    assert snapshot["orphan_bridges"] == []
    assert snapshot["managed_sessions"][0]["session_id"] == "sess-detached-ui"
    assert snapshot["managed_sessions"][0]["state"] == "attached"


def test_collect_local_health_flags_orphaned_managed_bridge(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-orphan",
        {
            "session_id": "sess-orphan",
            "pid": 8881,
            "codex_bin": "/opt/homebrew/bin/codex",
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
            {"pid": 8882, "ppid": 8881, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
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
            "control_path": "managed",
            "liveness_model": "codex_bridge",
            "provider_cli": {"path": "/opt/homebrew/bin/codex", "source": "bridge_state"},
            "pid": 8881,
            "workspace_label": "citi",
            "status": "orphan",
            "started_at": "2026-04-17T18:02:00Z",
            "heartbeat_at": "2026-04-17T18:02:00Z",
            "reason_codes": ["no_managed_session_bound"],
        }
    ]


def test_collect_local_health_flags_dead_codex_bridge_with_orphan_app_server(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    state_file = _write_codex_bridge_state(
        state_dir,
        "sess-dead-bridge",
        {
            "session_id": "sess-dead-bridge",
            "pid": 8881,
            "app_server_pid": 8882,
            "app_server_pgid": 8882,
            "codex_bin": "/opt/homebrew/bin/codex",
            "ws_url": "ws://127.0.0.1:49888",
            "app_server_ws_url": "ws://127.0.0.1:49887",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T18:02:00Z",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch, alive=False)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8882, "ppid": 1, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["headline"] == "Longhouse has orphaned managed sessions"
    assert snapshot["managed_summary"]["orphan_bridge_count"] == 1
    assert snapshot["orphan_bridges"][0]["session_id"] == "sess-dead-bridge"
    assert snapshot["orphan_bridges"][0]["app_server_pid"] == 8882
    assert snapshot["orphan_bridges"][0]["reason_codes"] == ["bridge_process_missing", "provider_child_alive"]
    assert state_file.exists()


def test_collect_local_health_uses_managed_session_phase_state_for_codex_bridge_session(monkeypatch, tmp_path: Path):
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
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-attached",
                "codex",
                "/Users/test/git/zerg-canonical",
                "zerg-canonical",
                "blocked",
                "shell",
                "codex_bridge",
                "2026-04-17T17:31:30Z",
                "2026-04-17T17:31:30Z",
            )
        ],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-attached",
        {
            "session_id": "sess-attached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/bridge-cwd",
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
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_sessions"][0]["workspace_label"] == "zerg-canonical"
    assert snapshot["managed_sessions"][0]["phase"] == "blocked on shell"
    assert snapshot["managed_sessions"][0]["phase_observed_at"] == "2026-04-17T17:31:30Z"
    assert snapshot["managed_sessions"][0]["last_activity_at"] == "2026-04-17T17:31:30Z"


def test_collect_local_health_keeps_attached_codex_idle_from_managed_session_state(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    pinned_now = datetime(2026, 4, 17, 18, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: pinned_now)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-attached", "codex", "2026-04-17T17:30:36Z")],
    )
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-attached",
                "codex",
                "/Users/test/git/zerg",
                "zerg",
                "idle",
                None,
                "codex_bridge",
                "2026-04-17T17:31:30Z",
                "2026-04-17T17:31:30Z",
            )
        ],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-attached",
        {
            "session_id": "sess-attached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:43:00Z",
            "thread_path": str(rollout_path),
            "active_turn_id": None,
            "last_turn_status": "completed",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-attached"},
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_sessions"][0]["state"] == "attached"
    assert snapshot["managed_sessions"][0]["phase"] == "idle"
    assert snapshot["managed_sessions"][0]["phase_observed_at"] == "2026-04-17T17:31:30Z"
    assert snapshot["managed_sessions"][0]["last_activity_at"] == "2026-04-17T17:43:00Z"


def test_collect_local_health_shows_unknown_phase_for_attached_codex_without_managed_session_phase_state(
    monkeypatch, tmp_path: Path
):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    pinned_now = datetime(2026, 4, 17, 18, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: pinned_now)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-attached", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-attached",
        {
            "session_id": "sess-attached",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:43:00Z",
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
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-attached"},
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_sessions"][0]["state"] == "attached"
    assert snapshot["managed_sessions"][0]["phase"] is None
    assert snapshot["managed_sessions"][0]["phase_observed_at"] is None
    assert snapshot["managed_sessions"][0]["last_activity_at"] == "2026-04-17T17:43:00Z"


def test_collect_local_health_flags_unknown_managed_phase_contract_drift(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    pinned_now = datetime(2026, 4, 17, 18, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: pinned_now)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-zerg.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-unknown-phase", "codex", "2026-04-17T17:30:36Z")],
    )
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-unknown-phase",
                "codex",
                "/Users/test/git/zerg",
                "zerg",
                "future_magic",
                None,
                "codex_bridge",
                "2026-04-17T17:31:30Z",
                "2026-04-17T17:31:30Z",
            )
        ],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-unknown-phase",
        {
            "session_id": "sess-unknown-phase",
            "pid": 7771,
            "ws_url": "ws://127.0.0.1:49760",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "updated_at": "2026-04-17T17:43:00Z",
            "thread_path": str(rollout_path),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 7771, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-unknown-phase"},
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49760",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert snapshot["headline"] == "Longhouse saw an unknown managed phase"
    assert "managed_unknown_phase" in snapshot["reasons"]
    assert any("Update the managed phase contract" in action for action in snapshot["suggested_actions"])
    assert snapshot["managed_sessions"][0]["state"] == "attached"
    assert snapshot["managed_sessions"][0]["raw_phase"] == "future_magic"
    assert snapshot["managed_sessions"][0]["phase"] == "unknown phase"
    assert snapshot["managed_sessions"][0]["phase_observed_at"] == "2026-04-17T17:31:30Z"


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

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
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
            {"pid": 7772, "ppid": 7771, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 7773,
                "ppid": 7000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49760",
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

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
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
            {"pid": 8882, "ppid": 8881, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8883,
                "ppid": 8000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49888",
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


def test_collect_local_health_keeps_waiting_for_rollout_session_attached_after_turn_activity(
    monkeypatch, tmp_path: Path
):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-missing.jsonl"
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-bad-thread", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
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
            "thread_subscription_status": "waiting_for_rollout",
            "thread_subscription_attempts": 2,
            "thread_subscription_last_error": (
                'thread/resume failed: {"code":-32600,"message":"no rollout found for thread id thr-live"}'
            ),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8881, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-bad-thread"},
            {"pid": 8882, "ppid": 8881, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8883,
                "ppid": 8000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49888",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["headline"] == "Longhouse shipping healthy"
    assert snapshot["managed_summary"]["degraded_count"] == 0
    assert snapshot["managed_summary"]["detached_count"] == 0
    assert snapshot["managed_sessions"][0]["state"] == "attached"
    assert snapshot["managed_sessions"][0]["thread_subscription_status"] == "waiting_for_rollout"
    assert snapshot["managed_sessions"][0]["thread_subscription_attempts"] == 2
    assert snapshot["managed_sessions"][0]["thread_subscription_last_error"] is not None
    assert snapshot["managed_sessions"][0]["reason_codes"] == []


def test_collect_local_health_marks_failed_thread_subscription_as_degraded(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "17" / "rollout-missing.jsonl"
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-bad-thread", "codex", "2026-04-17T17:30:36Z")],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
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
            "thread_subscription_status": "failed",
            "thread_subscription_attempts": 4,
            "thread_subscription_last_error": 'thread/resume failed: {"code":-32000,"message":"permission denied"}',
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 8881, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-bad-thread"},
            {"pid": 8882, "ppid": 8881, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8883,
                "ppid": 8000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49888",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["headline"] == "Longhouse lost managed session control"
    assert snapshot["managed_summary"]["degraded_count"] == 1
    assert snapshot["managed_summary"]["detached_count"] == 0
    assert snapshot["managed_sessions"][0]["state"] == "degraded"
    assert snapshot["managed_sessions"][0]["thread_subscription_status"] == "failed"
    assert snapshot["managed_sessions"][0]["thread_subscription_attempts"] == 4
    assert snapshot["managed_sessions"][0]["thread_subscription_last_error"] is not None
    assert snapshot["managed_sessions"][0]["reason_codes"] == ["thread_subscription_failed"]


def test_collect_local_health_names_subagent_control_failure(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    rollout_path = tmp_path / "sessions" / "2026" / "04" / "29" / "rollout-child.jsonl"
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-subagent-control", "codex", "2026-04-29T19:48:36Z")],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-subagent-control",
        {
            "session_id": "sess-subagent-control",
            "pid": 8891,
            "ws_url": "ws://127.0.0.1:49889",
            "cwd": "/Users/test/git/chaos",
            "status": "ready",
            "updated_at": "2026-04-29T19:49:00Z",
            "thread_path": str(rollout_path),
            "active_turn_id": "turn-live",
            "last_turn_status": "inProgress",
            "thread_subscription_status": "failed",
            "thread_subscription_attempts": 1,
            "thread_subscription_last_error": (
                "thread/resume returned Codex subagent thread 019ddb6e-114f-7643-89db-86c31a2aa706; "
                "refusing to adopt as managed primary"
            ),
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {
                "pid": 8891,
                "ppid": 1,
                "command": "longhouse-engine codex-bridge run --session-id sess-subagent-control",
            },
            {"pid": 8892, "ppid": 8891, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8893,
                "ppid": 8000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49889",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["managed_sessions"][0]["state"] == "degraded"
    assert snapshot["managed_sessions"][0]["reason_codes"] == ["control_attached_to_subagent"]


def test_codex_source_is_subagent_accepts_known_aliases():
    assert local_health_service._codex_source_is_subagent({"subagent": {}})
    assert local_health_service._codex_source_is_subagent({"subAgent": {}})
    assert local_health_service._codex_source_is_subagent({"sub_agent": {}})
    assert not local_health_service._codex_source_is_subagent({"threadSpawn": {}})


def test_collect_local_health_names_stale_subagent_bridge_path(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    parent_path = tmp_path / "sessions" / "2026" / "04" / "29" / "rollout-parent.jsonl"
    child_path = tmp_path / "sessions" / "2026" / "04" / "29" / "rollout-child.jsonl"
    child_path.parent.mkdir(parents=True, exist_ok=True)
    parent_path.write_text("{}\n")
    child_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "019dd708-573a-7131-a4d9-9ee855520483",
                                "depth": 1,
                            }
                        }
                    }
                },
            }
        )
        + "\n"
    )
    _write_session_binding_rows(
        tmp_path,
        [
            (str(child_path), "sess-subagent-control", "codex", "2026-04-29T19:48:36Z"),
            (str(parent_path), "sess-subagent-control", "codex", "2026-04-30T01:12:27Z"),
        ],
    )

    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-subagent-control",
        {
            "session_id": "sess-subagent-control",
            "pid": 8891,
            "ws_url": "ws://127.0.0.1:49889",
            "cwd": "/Users/test/git/chaos",
            "status": "ready",
            "updated_at": "2026-04-29T19:49:00Z",
            "thread_path": str(child_path),
            "last_turn_status": "completed",
            "thread_subscription_status": "subscribed",
            "thread_subscription_attempts": 12,
            "thread_subscription_last_error": None,
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {
                "pid": 8891,
                "ppid": 1,
                "command": "longhouse-engine codex-bridge run --session-id sess-subagent-control",
            },
            {"pid": 8892, "ppid": 8891, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
            {
                "pid": 8893,
                "ppid": 8000,
                "command": "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:49889",
            },
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["managed_sessions"][0]["state"] == "degraded"
    assert snapshot["managed_sessions"][0]["thread_subscription_status"] == "subscribed"
    assert snapshot["managed_sessions"][0]["reason_codes"] == ["control_attached_to_subagent"]


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
    assert "Run: longhouse machine repair" in snapshot["suggested_actions"]


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
    assert "Run: longhouse machine repair" not in snapshot["suggested_actions"]


def test_collect_local_health_broken_when_launch_config_disagrees(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    stable_home = home / ".longhouse"
    service_file = _write_service_plist(tmp_path, machine_name="cinder.local")
    _write_shipper_db(stable_home, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(
        stable_home,
        url="http://127.0.0.1:8080",
        machine_name="cinder.local",
        runner_enabled=True,
    )
    _write_engine_status(stable_home, age_seconds=5)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    snapshot = local_health_service.collect_local_health(stable_home)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert snapshot["launch_readiness"]["state"] == "broken"
    assert "config_url_runner_url_mismatch" in snapshot["reasons"]
    assert "machine_name_runner_name_mismatch" in snapshot["reasons"]
    assert "launch config" in snapshot["headline"].lower()
    assert "Run: longhouse machine repair" in snapshot["suggested_actions"]


def test_collect_local_health_ignores_global_runner_drift_for_scratch_home(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    scratch_home = home / ".longhouse-dev"
    service_file = _write_service_plist(tmp_path, machine_name="cinder.local")
    _write_shipper_db(scratch_home, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(scratch_home, url="http://127.0.0.1:8080", machine_name="cinder.local")
    _write_engine_status(scratch_home, age_seconds=5)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    snapshot = local_health_service.collect_local_health(scratch_home)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["launch_readiness"]["state"] == "ready"
    assert "config_url_runner_url_mismatch" not in snapshot["reasons"]
    assert "machine_name_runner_name_mismatch" not in snapshot["reasons"]
    assert snapshot["launch_readiness"]["runner_expected"] is False
    assert snapshot["launch_readiness"]["runner"]["exists"] is False


def test_collect_launch_readiness_respects_explicit_control_plane_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    stable_home = home / ".longhouse"
    service_file = _write_service_plist(tmp_path, machine_name="cinder")
    runner_env = _write_runner_env(tmp_path, url="https://demo.longhouse.test", runner_name="cinder")
    _write_local_config(stable_home, url="http://127.0.0.1:8080", machine_name="cinder")
    _write_shipper_db(stable_home, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [runner_env])

    readiness = local_health_service.collect_launch_readiness(
        stable_home,
        runtime_url_override="https://demo.longhouse.test",
        machine_name_override="cinder",
    )

    assert readiness["state"] == "ready"
    assert readiness["control_plane_url"] == "https://demo.longhouse.test"
    assert "config_url_runner_url_mismatch" not in readiness["reasons"]
    assert "machine_name_runner_name_mismatch" not in readiness["reasons"]


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


def test_collect_local_health_downgrades_generation_only_drift(monkeypatch, tmp_path: Path):
    _write_local_config(tmp_path, url="https://demo.longhouse.test", machine_name="cinder")
    service_file = _write_service_plist(
        tmp_path,
        machine_name="cinder",
        config_generation="stale-generation",
        state_hash=_machine_state_hash(
            url="https://demo.longhouse.test",
            machine_name="cinder",
        ),
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

    assert snapshot["health_state"] == "healthy"
    assert snapshot["launch_readiness"]["state"] == "ready"
    assert snapshot["launch_readiness"]["reasons"] == []
    assert snapshot["launch_readiness"]["warnings"] == ["service_generation_mismatch"]


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


def test_local_health_command_fast_json_uses_fast_tier(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    _write_engine_status(tmp_path / ".longhouse", age_seconds=2)

    result = runner.invoke(app, ["local-health", "--fast", "--json", "--claude-dir", str(tmp_path / ".claude")])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["collection_tier"] == "fast"


def test_local_health_command_rejects_fast_and_deep(tmp_path: Path):
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["local-health", "--fast", "--deep", "--json", "--claude-dir", str(tmp_path / ".claude")],
    )

    assert result.exit_code != 0


def test_local_health_command_prints_launch_warnings(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    longhouse_home = tmp_path / ".longhouse"
    _write_local_config(longhouse_home, url="https://demo.longhouse.test", machine_name="cinder")
    service_file = _write_service_plist(
        tmp_path,
        machine_name="cinder",
        config_generation="stale-generation",
        state_hash=_machine_state_hash(
            url="https://demo.longhouse.test",
            machine_name="cinder",
        ),
    )
    _write_shipper_db(longhouse_home, [("/tmp/claude-a.jsonl", "claude", "sess-1", None, "2026-04-14T00:00:00Z")])
    _write_engine_status(longhouse_home, age_seconds=5)
    monkeypatch.setattr(
        local_health_service,
        "get_service_info",
        lambda *args, **kwargs: _service_info("running", service_file=str(service_file)),
    )
    _disable_real_runner_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["local-health", "--claude-dir", str(tmp_path / ".claude")])

    assert result.exit_code == 0, result.output
    assert "Launch warnings" in result.output
    assert "service_generation_mismatch" in result.output


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


def test_collect_local_health_recent_touches_use_workspace_context_and_ignore_meta_files(monkeypatch, tmp_path: Path):
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


def test_collect_local_health_recent_touch_context_scan_counts_physical_lines(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    now = datetime(2026, 4, 12, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)

    session = tmp_path / "projects" / "-Users-davidrose-git-path-fallback" / "claude-session.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "\n".join(
            [
                "",
                "not-json",
                "",
                "still-not-json",
                "",
                json.dumps({"type": "assistant"}),
                json.dumps({"message": {"cwd": "/Users/davidrose/git/late-cwd", "gitBranch": "too-late"}}),
            ]
        )
        + "\n"
    )
    _write_shipper_db(
        tmp_path,
        [(str(session), "claude", "claude-limited-context", None, (now - timedelta(minutes=2)).isoformat())],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["activity_summary"]["recent_touches"] == [
        {
            "provider": "claude",
            "last_updated": (now - timedelta(minutes=2)).isoformat(),
            "workspace_label": "path-fallback",
            "branch": None,
            "is_subagent": False,
        }
    ]


def test_collect_local_health_surfaces_live_unmanaged_processes_separately_from_recent_activity(
    monkeypatch, tmp_path: Path
):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "_scan_provider_processes", _REAL_SCAN_PROVIDER_PROCESSES)
    monkeypatch.setattr(
        local_health_service,
        "_collect_managed_sessions_by_process",
        _REAL_COLLECT_MANAGED_SESSIONS_BY_PROCESS,
    )
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    now = datetime(2026, 4, 22, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)

    managed_a = "55c61956-7554-4713-8c9b-fb0fa6164c2c"
    managed_b = "918ec866-e194-4339-a227-d41c8bf48ea9"
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                managed_a,
                "claude",
                "/Users/test/git/zeta/athena-horizon",
                "athena-horizon",
                "thinking",
                "Read",
                "claude_hook",
                (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
                (now - timedelta(seconds=20)).isoformat().replace("+00:00", "Z"),
            ),
            (
                managed_b,
                "claude",
                "/Users/test/git/zeta/athena-horizon",
                "athena-horizon",
                "needs_user",
                None,
                "claude_hook",
                (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
                (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            ),
        ],
    )
    _write_shipper_db(
        tmp_path,
        [
            (
                "/Users/test/.claude/projects/-Users-test-git-zeta-athena-horizon/55c61956-7554-4713-8c9b-fb0fa6164c2c.jsonl",
                "claude",
                managed_a,
                managed_a,
                (now - timedelta(minutes=1)).isoformat(),
            ),
            (
                "/Users/test/.claude/projects/-Users-test-git-zeta-athena-horizon/918ec866-e194-4339-a227-d41c8bf48ea9.jsonl",
                "claude",
                managed_b,
                managed_b,
                (now - timedelta(minutes=2)).isoformat(),
            ),
            (
                "/Users/test/.codex/sessions/2026/04/22/rollout-mayagents.jsonl",
                "codex",
                "019db63d-983b-77e0-9324-38ffa734d9a5",
                "019db63d-983b-77e0-9324-38ffa734d9a5",
                (now - timedelta(minutes=3)).isoformat(),
            ),
        ],
    )
    _patch_process_iter(
        monkeypatch,
        [
            _FakeProc(
                pid=48145,
                cmdline=["claude", "--session-id", managed_a],
                create_time=(now - timedelta(minutes=28)).timestamp(),
                env={"LONGHOUSE_MANAGED_SESSION_ID": managed_a},
                cwd="/Users/test/git/zeta/athena-horizon",
            ),
            _FakeProc(
                pid=72211,
                cmdline=["claude", "--session-id", managed_b],
                create_time=(now - timedelta(minutes=5)).timestamp(),
                env={"LONGHOUSE_MANAGED_SESSION_ID": managed_b},
                cwd="/Users/test/git/zeta/athena-horizon",
            ),
            _FakeProc(
                pid=48047,
                cmdline=[
                    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex/codex",
                    "-m",
                    "gpt-5.4",
                ],
                create_time=(now - timedelta(minutes=16)).timestamp(),
                env={},
                cwd="/Users/test/git/zerg",
            ),
            _FakeProc(
                pid=55478,
                cmdline=[
                    "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex/codex",
                    "-m",
                    "gpt-5.4",
                ],
                create_time=(now - timedelta(minutes=24)).timestamp(),
                env={},
                cwd="/Users/test/git/me/myagents",
            ),
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert [row["workspace_label"] for row in snapshot["unmanaged_processes"]] == ["zerg", "myagents"]
    assert {row["provider"] for row in snapshot["unmanaged_processes"]} == {"codex"}
    assert {row["session_id"] for row in snapshot["managed_sessions"]} == {managed_a, managed_b}
    assert snapshot["activity_summary"]["provider_counts_recent"] == {"claude": 2, "codex": 1}


def test_collect_local_health_fast_uses_resolved_sessions_without_process_scan(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    monkeypatch.setattr(
        local_health_service,
        "_load_managed_session_phase_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fast local-health must not read phase overlay")),
    )
    managed_id = "55c61956-7554-4713-8c9b-fb0fa6164c2c"
    unmanaged_id = "019dcac2-fd02-7a97-85b8-6f725b9d6252"
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={
            "sessions": [
                {
                    "session_id": managed_id,
                    "provider": "codex",
                    "provider_session_id": "thread-codex",
                    "control_path": "managed",
                    "presentation_state": "managed_attached",
                    "state": "attached",
                    "phase": "thinking",
                    "tool_name": None,
                    "phase_observed_at": "2026-05-05T11:59:58Z",
                    "last_activity_at": "2026-05-05T11:59:58Z",
                    "workspace": {
                        "cwd": "/Users/test/git/zerg",
                        "label": "zerg",
                        "branch": "kernel-canonical-sessions",
                    },
                    "process": {
                        "pid": 4201,
                        "process_start_time": "2026-05-05T11:20:00Z",
                        "started_at": "2026-05-05T11:20:00Z",
                    },
                    "bridge": {
                        "bridge_pid": 4202,
                        "app_server_pid": 4203,
                        "heartbeat_at": "2026-05-05T11:59:58Z",
                        "status": "ready",
                        "thread_subscription_status": "subscribed",
                    },
                    "evidence": {
                        "process_observed": True,
                        "transcript_observed": True,
                        "bridge_state": "ready",
                        "join_keys": [
                            "provider_session_id=thread-codex",
                            "app_server_pid=4203",
                        ],
                    },
                    "reason_codes": [],
                },
                {
                    "provider": "claude",
                    "provider_session_id": unmanaged_id,
                    "control_path": "unmanaged",
                    "presentation_state": "unmanaged",
                    "state": "unmanaged",
                    "last_activity_at": "2026-05-05T11:59:59Z",
                    "workspace": {
                        "cwd": "/Users/test/git/zerg",
                        "label": "zerg",
                        "branch": None,
                    },
                    "process": {
                        "pid": 48145,
                        "process_start_time": "2026-05-05T11:45:00Z",
                        "started_at": "2026-05-05T11:45:00Z",
                    },
                    "bridge": {},
                    "evidence": {
                        "process_observed": True,
                        "transcript_observed": True,
                        "hook_seen_at": "2026-05-05T11:59:59Z",
                        "join_keys": [
                            "provider_session_id=019dcac2-fd02-7a97-85b8-6f725b9d6252",
                            "source_path=/Users/test/.claude/projects/zerg/session.jsonl",
                            "pid=48145",
                        ],
                    },
                    "reason_codes": [],
                },
            ],
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert snapshot["collection_tier"] == "fast"
    assert snapshot["managed_sessions"][0]["session_id"] == managed_id
    assert snapshot["managed_sessions"][0]["provider"] == "codex"
    assert snapshot["managed_sessions"][0]["liveness_model"] == "engine_status"
    assert snapshot["managed_sessions"][0]["phase"] == "thinking"
    assert snapshot["unmanaged_processes"][0]["provider"] == "claude"
    assert snapshot["unmanaged_processes"][0]["pid"] == 48145
    assert snapshot["unmanaged_processes"][0]["workspace_label"] == "zerg"
    assert snapshot["unmanaged_processes"][0]["liveness_model"] == "engine_status"


def test_collect_local_health_fast_flags_missing_resolved_sessions_contract(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={
            "managed_sessions": [
                {
                    "session_id": "legacy-managed",
                    "provider": "codex",
                    "state": "attached",
                    "phase": "thinking",
                    "observed_at": "2026-05-05T11:59:58Z",
                    "lease_ttl_ms": 900000,
                }
            ],
            "unmanaged_session_bindings": [
                {
                    "provider": "claude",
                    "provider_session_id": "legacy-unmanaged",
                    "pid": 48145,
                    "process_start_time": "2026-05-05T11:45:00Z",
                    "cwd": "/Users/test/git/zerg",
                    "observed_at": "2026-05-05T11:59:59Z",
                }
            ],
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert snapshot["collection_tier"] == "fast"
    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert snapshot["headline"] == "Longhouse local status needs a newer engine"
    assert "engine_status_sessions_missing" in snapshot["reasons"]
    assert snapshot["managed_summary"]["canonical_sessions_missing"] is True
    assert snapshot["managed_sessions"] == []
    assert snapshot["unmanaged_processes"] == []


def test_collect_local_health_fast_flags_invalid_resolved_sessions_contract(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={"sessions": {"not": "a-list"}},
    )

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert snapshot["collection_tier"] == "fast"
    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert snapshot["headline"] == "Longhouse local status has invalid session data"
    assert "engine_status_sessions_invalid" in snapshot["reasons"]
    assert snapshot["managed_summary"]["canonical_sessions_invalid"] is True
    assert snapshot["managed_sessions"] == []
    assert snapshot["unmanaged_processes"] == []


def test_collect_local_health_fast_treats_stale_evidence_as_degraded(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={
            "sessions": [
                {
                    "session_id": "55c61956-7554-4713-8c9b-fb0fa6164c2c",
                    "provider": "codex",
                    "provider_session_id": "thread-codex",
                    "control_path": "managed",
                    "presentation_state": "stale_evidence",
                    "state": "reconnecting",
                    "last_activity_at": "2026-05-05T11:59:58Z",
                    "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                    "process": {"pid": 4201},
                    "bridge": {},
                    "evidence": {"process_observed": True, "transcript_observed": True},
                    "reason_codes": ["future_state"],
                }
            ],
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert snapshot["health_state"] == "broken"
    assert snapshot["headline"] == "Longhouse lost managed session control"
    assert snapshot["managed_sessions"][0]["state"] == "degraded"
    assert "managed_session_control_degraded" in snapshot["reasons"]


def test_collect_local_health_fast_sessions_only_golden(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    monkeypatch.setattr(
        local_health_service,
        "_load_managed_session_phase_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fast local-health must not read phase overlay")),
    )
    status_path = get_agent_status_path(tmp_path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(_load_local_health_fixture("engine_status_sessions_only.json")))
    timestamp = time.time() - 1
    os.utime(status_path, (timestamp, timestamp))

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert _fast_local_health_contract_projection(snapshot) == _load_local_health_fixture(
        "fast_snapshot_sessions_only.golden.json"
    )


def test_collect_local_health_fast_prefers_resolved_engine_sessions(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("fast local-health must not scan processes")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    managed_id = "sess-managed-codex"
    unmanaged_id = "sess-unmanaged-claude"
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={
            "sessions": [
                {
                    "session_id": managed_id,
                    "provider": "codex",
                    "provider_session_id": "thread-codex",
                    "control_path": "managed",
                    "presentation_state": "managed_attached",
                    "state": "attached",
                    "phase": "thinking",
                    "tool_name": None,
                    "phase_observed_at": "2026-05-05T11:59:50Z",
                    "last_activity_at": "2026-05-05T11:59:58Z",
                    "workspace": {
                        "cwd": "/Users/test/git/zerg",
                        "label": "zerg",
                        "branch": "session-identity-kernel-cleanup",
                    },
                    "process": {
                        "pid": 4201,
                        "process_start_time": "2026-05-05T11:20:00Z",
                        "started_at": "2026-05-05T11:20:00Z",
                    },
                    "bridge": {
                        "bridge_pid": 4202,
                        "app_server_pid": 4203,
                        "heartbeat_at": "2026-05-05T11:59:58Z",
                        "status": "ready",
                        "thread_subscription_status": "subscribed",
                    },
                    "evidence": {
                        "process_observed": True,
                        "transcript_observed": True,
                        "bridge_state": "ready",
                        "hook_seen_at": "2026-05-05T11:59:57Z",
                        "join_keys": [
                            "provider_session_id=thread-codex",
                            "app_server_pid=4203",
                        ],
                    },
                    "reason_codes": ["bridge_ready"],
                },
                {
                    "session_id": None,
                    "provider": "claude",
                    "provider_session_id": unmanaged_id,
                    "control_path": "unmanaged",
                    "presentation_state": "unmanaged",
                    "state": "unmanaged",
                    "last_activity_at": "2026-05-05T11:59:59Z",
                    "workspace": {
                        "cwd": "/Users/test/git/myagents",
                        "label": "myagents",
                        "branch": None,
                    },
                    "process": {
                        "pid": 48145,
                        "process_start_time": "2026-05-05T11:45:00Z",
                        "started_at": "2026-05-05T11:45:00Z",
                    },
                    "bridge": {},
                    "evidence": {
                        "process_observed": True,
                        "transcript_observed": True,
                        "bridge_state": None,
                        "hook_seen_at": "2026-05-05T11:59:59Z",
                        "join_keys": [
                            "provider_session_id=sess-unmanaged-claude",
                            "source_path=/Users/test/.claude/projects/myagents/session.jsonl",
                            "pid=48145",
                        ],
                    },
                    "reason_codes": [],
                },
            ],
            "managed_sessions": [],
            "unmanaged_session_bindings": [
                {
                    "provider": "codex",
                    "provider_session_id": "legacy-row-should-not-show",
                    "pid": 9999,
                    "process_start_time": "2026-05-05T11:00:00Z",
                    "cwd": "/Users/test/git/legacy",
                    "observed_at": "2026-05-05T11:59:59Z",
                }
            ],
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path, fast=True)

    assert snapshot["collection_tier"] == "fast"
    assert [row["session_id"] for row in snapshot["managed_sessions"]] == [managed_id]
    managed = snapshot["managed_sessions"][0]
    assert managed["provider"] == "codex"
    assert managed["workspace_label"] == "zerg"
    assert managed["branch"] == "session-identity-kernel-cleanup"
    assert managed["phase"] == "thinking"
    assert managed["bridge_pid"] == 4202
    assert managed["app_server_pid"] == 4203
    assert managed["thread_subscription_status"] == "subscribed"
    assert managed["evidence"]["join_keys"] == [
        "provider_session_id=thread-codex",
        "app_server_pid=4203",
    ]
    assert [row["provider_session_id"] for row in snapshot["unmanaged_processes"]] == [unmanaged_id]
    unmanaged = snapshot["unmanaged_processes"][0]
    assert unmanaged["provider"] == "claude"
    assert unmanaged["pid"] == 48145
    assert unmanaged["workspace_label"] == "myagents"
    assert unmanaged["source_path"] == "/Users/test/.claude/projects/myagents/session.jsonl"
    assert unmanaged["liveness_model"] == "engine_status"


def test_collect_local_health_deep_prefers_resolved_engine_sessions(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    monkeypatch.setattr(
        local_health_service,
        "_compute_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("resolved engine sessions are canonical")),
    )
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(local_health_service, "_utc_now", lambda: now)
    _write_engine_status(
        tmp_path,
        age_seconds=1,
        payload={
            "sessions": [
                {
                    "session_id": "c73dbe01-c218-430f-8f80-a78c243fd8f7",
                    "provider": "codex",
                    "provider_session_id": "thread-codex",
                    "control_path": "managed",
                    "presentation_state": "managed_attached",
                    "state": "attached",
                    "phase": "thinking",
                    "tool_name": "Bash",
                    "phase_observed_at": "2026-05-05T11:59:58Z",
                    "last_activity_at": "2026-05-05T11:59:58Z",
                    "workspace": {"cwd": "/Users/test/git/zerg", "label": "zerg"},
                    "process": {"pid": 4201},
                    "bridge": {
                        "bridge_pid": 4202,
                        "app_server_pid": 4203,
                        "heartbeat_at": "2026-05-05T11:59:58Z",
                        "status": "ready",
                        "thread_subscription_status": "subscribed",
                    },
                    "evidence": {"process_observed": True, "transcript_observed": True},
                    "reason_codes": [],
                }
            ],
        },
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["collection_tier"] == "deep"
    assert snapshot["managed_summary"]["attached_count"] == 1
    assert snapshot["managed_summary"]["degraded_count"] == 0
    assert "managed_session_control_degraded" not in snapshot["reasons"]
    assert snapshot["managed_sessions"][0]["liveness_model"] == "engine_status"


def test_collect_local_health_deep_falls_back_when_resolved_sessions_absent(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=1, payload={})

    rollout_path = tmp_path / "sessions" / "2026" / "05" / "05" / "rollout-fallback.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    _write_session_binding_rows(
        tmp_path,
        [(str(rollout_path), "sess-fallback", "codex", "2026-05-05T11:59:58Z")],
    )
    state_dir = tmp_path / ".longhouse" / "managed-local" / "codex-bridge"
    _write_codex_bridge_state(
        state_dir,
        "sess-fallback",
        {
            "session_id": "sess-fallback",
            "pid": 4401,
            "app_server_pid": 4402,
            "codex_bin": "/opt/homebrew/bin/codex",
            "ws_url": "ws://127.0.0.1:50001",
            "cwd": "/Users/test/git/zerg",
            "status": "ready",
            "thread_id": "thread-fallback",
            "thread_path": str(rollout_path),
            "updated_at": "2026-05-05T11:59:58Z",
        },
    )
    monkeypatch.setattr(local_health_service, "_codex_bridge_state_dir", lambda base_dir: state_dir)
    _stub_bridge_alive(monkeypatch)
    monkeypatch.setattr(
        local_health_service,
        "_collect_process_rows",
        lambda: [
            {"pid": 4401, "ppid": 1, "command": "longhouse-engine codex-bridge run --session-id sess-fallback"},
            {"pid": 4402, "ppid": 4401, "command": "/opt/homebrew/bin/codex app-server --listen ws://127.0.0.1:0"},
        ],
    )

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["collection_tier"] == "deep"
    assert "engine_status_sessions_missing" not in snapshot["reasons"]
    assert snapshot["managed_sessions"][0]["session_id"] == "sess-fallback"
    assert snapshot["managed_sessions"][0]["liveness_model"] == "codex_bridge"


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


def test_local_health_menubar_prefers_machine_repair_for_configured_machine(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    calls: list[dict[str, object]] = []

    def fake_run(command, check, cwd):
        calls.append({"command": command, "check": check, "cwd": cwd})

    _write_local_config(tmp_path / ".longhouse", url="https://demo.longhouse.test", machine_name="cinder")
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
        ],
    )

    assert result.exit_code == 1, result.output
    assert "machine repair" in result.output
    assert "connect --install" not in result.output
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
# upgrade path now reconciles local runtime artifacts automatically, so CLI version is
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


def test_fast_local_health_entrypoint_emits_json(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_collect(claude_dir: str | None, *, fast: bool) -> dict[str, object]:
        captured["claude_dir"] = claude_dir
        captured["fast"] = fast
        return {"health_state": "healthy", "severity": "green"}

    monkeypatch.setattr(local_health_fast, "_collect", fake_collect)

    exit_code = local_health_fast.main(["--fast", "--json", "--claude-dir", "/tmp/claude"])

    assert exit_code == 0
    assert captured == {"claude_dir": "/tmp/claude", "fast": True}
    assert json.loads(capsys.readouterr().out) == {"health_state": "healthy", "severity": "green"}


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

    monkeypatch.setattr(local_health_service, "_compute_process_snapshot", _REAL_COMPUTE_PROCESS_SNAPSHOT)
    monkeypatch.setattr(psutil, "process_iter", fake_iter)


def test_process_snapshot_scope_reuses_single_scan(monkeypatch):
    calls = 0

    def fake_compute():
        nonlocal calls
        calls += 1
        return (
            [{"pid": 101, "ppid": 1, "command": "claude --session-id sess-1"}],
            [
                {
                    "session_id": "11111111-2222-3333-4444-555555555555",
                    "provider": "claude",
                    "pid": 101,
                    "cwd": "/Users/test/git/zerg",
                    "workspace_label": "zerg",
                    "device_id": "device-1",
                    "started_at": "2026-04-19T00:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(local_health_service, "_compute_process_snapshot", fake_compute)

    with local_health_service._process_snapshot_scope():
        assert local_health_service._collect_process_rows() == [
            {"pid": 101, "ppid": 1, "command": "claude --session-id sess-1"}
        ]
        assert local_health_service._scan_provider_processes() == [
            {
                "session_id": "11111111-2222-3333-4444-555555555555",
                "provider": "claude",
                "pid": 101,
                "cwd": "/Users/test/git/zerg",
                "workspace_label": "zerg",
                "device_id": "device-1",
                "started_at": "2026-04-19T00:00:00Z",
            }
        ]

    assert calls == 1


def test_collect_local_health_reports_process_scan_payload_contract(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [tmp_path / "missing-runner.env"])
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    session_id = "bfb567fb-7e0f-4552-8411-24f682751484"
    proc = _FakeProc(
        pid=55507,
        cmdline=["/opt/homebrew/bin/claude", "--session-id", session_id],
        create_time=now.timestamp(),
        env={
            "LONGHOUSE_MANAGED_SESSION_ID": session_id,
            "LONGHOUSE_DEVICE_ID": "device-abc",
        },
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["managed_summary"] == {
        "attached_count": 1,
        "detached_count": 0,
        "degraded_count": 0,
        "orphan_bridge_count": 0,
        "latest_activity_at": "2026-04-19T00:00:00Z",
    }
    assert snapshot["managed_sessions"] == [
        {
            "session_id": session_id,
            "provider": "claude",
            "control_path": "managed",
            "liveness_model": "process_scan",
            "provider_cli": {"path": "/opt/homebrew/bin/claude", "source": "process"},
            "pid": 55507,
            "workspace_label": "zerg",
            "cwd": "/Users/test/git/zerg",
            "device_id": "device-abc",
            "started_at": "2026-04-19T00:00:00Z",
            "branch": None,
            "state": "attached",
            "raw_phase": None,
            "phase": None,
            "phase_observed_at": None,
            "last_activity_at": "2026-04-19T00:00:00Z",
            "bridge_status": None,
            "bridge_pid": None,
            "bridge_heartbeat_at": None,
            "reason_codes": [],
        }
    ]
    assert snapshot["orphan_bridges"] == []
    assert snapshot["unmanaged_processes"] == []


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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())

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


def test_process_scan_detects_managed_opencode_via_env(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=55508,
        cmdline=["/opt/homebrew/bin/opencode", "serve"],
        create_time=now.timestamp(),
        env={
            "LONGHOUSE_MANAGED_SESSION_ID": "bfb567fb-7e0f-4552-8411-24f682751484",
            "LONGHOUSE_DEVICE_ID": "device-opencode",
        },
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "bfb567fb-7e0f-4552-8411-24f682751484"
    assert row["provider"] == "opencode"
    assert row["pid"] == 55508
    assert row["cwd"] == "/Users/test/git/zerg"
    assert row["device_id"] == "device-opencode"
    assert row["state"] == "attached"
    assert row["raw_phase"] is None
    assert row["phase"] is None


def test_process_scan_detects_managed_antigravity_via_env(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=55509,
        cmdline=["/Users/test/.local/bin/agy"],
        create_time=now.timestamp(),
        env={
            "LONGHOUSE_MANAGED_SESSION_ID": "bfb567fb-7e0f-4552-8411-24f682751484",
            "LONGHOUSE_DEVICE_ID": "device-antigravity",
        },
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())

    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "bfb567fb-7e0f-4552-8411-24f682751484"
    assert row["provider"] == "antigravity"
    assert row["pid"] == 55509
    assert row["cwd"] == "/Users/test/git/zerg"
    assert row["device_id"] == "device-antigravity"
    assert row["state"] == "attached"


def test_process_scan_uses_phase_overlay_when_available(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    session_id = "bfb567fb-7e0f-4552-8411-24f682751484"
    proc = _FakeProc(
        pid=55507,
        cmdline=["claude", "--session-id", session_id],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": session_id},
        cwd="/Users/test/git/process-cwd",
    )
    _patch_process_iter(monkeypatch, [proc])
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                session_id,
                "claude",
                "/Users/test/git/zerg-canonical",
                "zerg-canonical",
                "running",
                "Bash",
                "claude_hook",
                "2026-04-19T00:04:00Z",
                "2026-04-19T00:05:00Z",
            )
        ],
    )

    rows = local_health_service._collect_managed_sessions_by_process(
        existing_session_ids=set(),
        phase_overlay=local_health_service._load_managed_session_phase_state(tmp_path, now=now),
    )

    assert len(rows) == 1
    assert rows[0]["cwd"] == "/Users/test/git/zerg-canonical"
    assert rows[0]["workspace_label"] == "zerg-canonical"
    assert rows[0]["phase"] == "running Bash"
    assert rows[0]["phase_observed_at"] == "2026-04-19T00:04:00Z"
    assert rows[0]["last_activity_at"] == "2026-04-19T00:05:00Z"


def test_process_scan_humanizes_needs_user_phase(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 5, 0, tzinfo=timezone.utc)
    session_id = "11111111-2222-3333-4444-555555555555"
    proc = _FakeProc(
        pid=55508,
        cmdline=["claude", "--session-id", session_id],
        create_time=now.timestamp() - 60,
        env={"LONGHOUSE_MANAGED_SESSION_ID": session_id},
        cwd="/Users/test/git/citi",
    )
    _patch_process_iter(monkeypatch, [proc])
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                session_id,
                "claude",
                "/Users/test/git/citi",
                "citi",
                "needs_user",
                None,
                "claude_hook",
                "2026-04-19T00:04:30Z",
                "2026-04-19T00:04:30Z",
            )
        ],
    )

    rows = local_health_service._collect_managed_sessions_by_process(
        existing_session_ids=set(),
        phase_overlay=local_health_service._load_managed_session_phase_state(tmp_path, now=now),
    )

    assert len(rows) == 1
    assert rows[0]["phase"] == "ready"
    assert rows[0]["phase_observed_at"] == "2026-04-19T00:04:30Z"
    assert rows[0]["last_activity_at"] == "2026-04-19T00:04:30Z"


def test_process_scan_marks_unknown_phase_contract_drift(monkeypatch, tmp_path: Path):
    now = datetime(2026, 4, 19, 0, 6, 0, tzinfo=timezone.utc)
    session_id = "66666666-7777-8888-9999-000000000000"
    proc = _FakeProc(
        pid=55509,
        cmdline=["claude", "--session-id", session_id],
        create_time=now.timestamp() - 60,
        env={"LONGHOUSE_MANAGED_SESSION_ID": session_id},
        cwd="/Users/test/git/citi",
    )
    _patch_process_iter(monkeypatch, [proc])
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                session_id,
                "claude",
                "/Users/test/git/citi",
                "citi",
                "future_magic",
                None,
                "claude_hook",
                "2026-04-19T00:05:30Z",
                "2026-04-19T00:05:30Z",
            )
        ],
    )

    rows = local_health_service._collect_managed_sessions_by_process(
        existing_session_ids=set(),
        phase_overlay=local_health_service._load_managed_session_phase_state(tmp_path, now=now),
    )

    assert len(rows) == 1
    assert rows[0]["raw_phase"] == "future_magic"
    assert rows[0]["phase"] == "unknown phase"
    assert rows[0]["phase_observed_at"] == "2026-04-19T00:05:30Z"


def test_local_health_phase_contract_covers_every_known_raw_phase():
    contract_raw_phases = {case.raw_phase for case in _load_managed_phase_contract()}
    assert contract_raw_phases == set(session_runtime.KNOWN_PHASES)


def test_local_health_phase_contract_matches_display_labels():
    for case in _load_managed_phase_contract():
        tool_name = _contract_tool_name(case)
        assert local_health_service._phase_display_label(case.raw_phase, tool_name) == case.display_for_tool(tool_name)


def test_managed_phase_contract_swift_generated_is_current():
    root = Path(__file__).resolve().parents[2]
    generated_path = (
        root
        / "desktop"
        / "LonghouseMenuBarHarness"
        / "Sources"
        / "LonghouseMenuBarCore"
        / "ManagedPhaseContract.generated.swift"
    )
    assert generated_path.read_text() == managed_phase_contract.render_swift_source()


def test_local_health_command_surfaces_managed_phase_contract_from_raw_hook_events(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [tmp_path / "missing-runner.env"])
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path / ".longhouse", age_seconds=2)
    now = datetime.now(timezone.utc)
    observed_at = now.isoformat().replace("+00:00", "Z")

    for index, case in enumerate(_load_managed_phase_contract()):
        session_id = f"contract-{case.raw_phase}-{index}"
        proc = _FakeProc(
            pid=60000 + index,
            cmdline=["claude", "--session-id", session_id],
            create_time=now.timestamp(),
            env={"LONGHOUSE_MANAGED_SESSION_ID": session_id},
            cwd="/Users/test/git/citi",
        )
        _patch_process_iter(monkeypatch, [proc])
        tool_name = _contract_tool_name(case)
        _write_outbox_phase_signal(
            tmp_path / ".longhouse",
            session_id=session_id,
            state=case.raw_phase,
            tool_name=tool_name,
            occurred_at=observed_at,
            name=f"prs.{case.raw_phase}.{index}.json",
        )

        result = runner.invoke(app, ["local-health", "--json", "--claude-dir", str(tmp_path / ".claude")])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        managed_session = next(item for item in payload["managed_sessions"] if item["session_id"] == session_id)
        assert managed_session["phase"] == case.display_for_tool(tool_name)


def test_managed_session_phase_state_keeps_persisted_rows_without_freshness_gating(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    stale_observed_at = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-stale",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "running",
                "Bash",
                "claude_hook",
                stale_observed_at,
                stale_observed_at,
            )
        ],
    )

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert overlay["sess-stale"]["phase"] == "running"
    assert overlay["sess-stale"]["observed_at"] == stale_observed_at


def test_managed_session_phase_state_drops_stale_finished_rows_after_retention(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    recent_observed_at = (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    stale_observed_at = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-recent",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "finished",
                None,
                "claude_hook",
                recent_observed_at,
                recent_observed_at,
            ),
            (
                "sess-stale",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "finished",
                None,
                "claude_hook",
                stale_observed_at,
                stale_observed_at,
            ),
        ],
    )

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert overlay["sess-recent"]["phase"] == "finished"
    assert "sess-stale" not in overlay


def test_managed_session_phase_state_keeps_finished_rows_at_retention_boundary(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    boundary_observed_at = (
        (now - timedelta(seconds=local_health_service._MANAGED_FINISHED_RETENTION_SECONDS))
        .isoformat()
        .replace("+00:00", "Z")
    )
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-boundary",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "finished",
                None,
                "claude_hook",
                boundary_observed_at,
                boundary_observed_at,
            )
        ],
    )

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert overlay["sess-boundary"]["phase"] == "finished"


def test_managed_session_phase_state_drops_finished_rows_with_invalid_timestamp(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-invalid",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "finished",
                None,
                "claude_hook",
                "not-a-timestamp",
                "not-a-timestamp",
            )
        ],
    )

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert "sess-invalid" not in overlay


def test_managed_session_phase_state_prefers_newer_outbox_signal(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime.now(tz=timezone.utc)
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-1",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "idle",
                None,
                "claude_hook",
                (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
                (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            )
        ],
    )
    _write_outbox_file(tmp_path, age_seconds=0, name="prs.sess-outbox.json")

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert overlay["sess-1"]["phase"] == "thinking"
    assert overlay["sess-1"]["source"] == "claude_hook"


def test_managed_session_phase_state_keeps_newer_stored_last_activity_when_outbox_wins_phase(
    monkeypatch, tmp_path: Path
):
    _disable_real_runner_env(monkeypatch, tmp_path)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    stored_phase_at = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    stored_last_activity_at = (now - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    outbox_observed_at = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    _write_managed_session_state_rows(
        tmp_path,
        [
            (
                "sess-1",
                "claude",
                "/Users/test/git/citi",
                "citi",
                "idle",
                None,
                "claude_hook",
                stored_phase_at,
                stored_last_activity_at,
            )
        ],
    )
    _write_outbox_phase_signal(
        tmp_path,
        session_id="sess-1",
        state="thinking",
        occurred_at=outbox_observed_at,
        name="prs.sess-1.phase.json",
    )

    overlay = local_health_service._load_managed_session_phase_state(tmp_path, now=now)

    assert overlay["sess-1"]["phase"] == "thinking"
    assert overlay["sess-1"]["observed_at"] == outbox_observed_at
    assert overlay["sess-1"]["last_activity_at"] == stored_last_activity_at
    assert overlay["sess-1"]["workspace_path"] == "/Users/test/git/citi"
    assert overlay["sess-1"]["workspace_label"] == "citi"


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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())

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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
    assert rows == []


def test_collect_unmanaged_processes_reports_live_bare_provider_clis(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    codex_vendor_bin = (
        "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/"
        "aarch64-apple-darwin/codex/codex"
    )
    managed_claude = _FakeProc(
        pid=11467,
        cmdline=["claude", "--session-id", "11111111-2222-3333-4444-555555555555"],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "11111111-2222-3333-4444-555555555555"},
        cwd="/Users/test/git/zeta/athena-horizon",
    )
    unmanaged_zerg_codex = _FakeProc(
        pid=11468,
        cmdline=[codex_vendor_bin, "-m", "gpt-5.4"],
        create_time=(now + timedelta(seconds=30)).timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    unmanaged_myagents_codex = _FakeProc(
        pid=11469,
        cmdline=[codex_vendor_bin, "-m", "gpt-5.4"],
        create_time=(now + timedelta(seconds=60)).timestamp(),
        env={},
        cwd="/Users/test/git/me/myagents",
    )
    unmanaged_opencode = _FakeProc(
        pid=11470,
        cmdline=["/opt/homebrew/bin/opencode", "serve", "--port", "41967"],
        create_time=(now + timedelta(seconds=90)).timestamp(),
        env={},
        cwd="/Users/test/git/open-source-widget",
    )
    unmanaged_antigravity = _FakeProc(
        pid=11471,
        cmdline=["/Users/test/.local/bin/agy"],
        create_time=(now + timedelta(seconds=120)).timestamp(),
        env={},
        cwd="/Users/test/git/antigravity-widget",
    )
    _patch_process_iter(
        monkeypatch,
        [managed_claude, unmanaged_zerg_codex, unmanaged_myagents_codex, unmanaged_opencode, unmanaged_antigravity],
    )

    rows = local_health_service._collect_unmanaged_processes()

    assert rows == [
        {
            "provider": "antigravity",
            "control_path": "unmanaged",
            "liveness_model": "process_scan",
            "provider_cli": {"path": "/Users/test/.local/bin/agy", "source": "process"},
            "pid": 11471,
            "workspace_label": "antigravity-widget",
            "cwd": "/Users/test/git/antigravity-widget",
            "branch": None,
            "started_at": "2026-04-19T00:02:00Z",
        },
        {
            "provider": "opencode",
            "control_path": "unmanaged",
            "liveness_model": "process_scan",
            "provider_cli": {"path": "/opt/homebrew/bin/opencode", "source": "process"},
            "pid": 11470,
            "workspace_label": "open-source-widget",
            "cwd": "/Users/test/git/open-source-widget",
            "branch": None,
            "started_at": "2026-04-19T00:01:30Z",
        },
        {
            "provider": "codex",
            "control_path": "unmanaged",
            "liveness_model": "process_scan",
            "provider_cli": {"path": codex_vendor_bin, "source": "process"},
            "pid": 11469,
            "workspace_label": "myagents",
            "cwd": "/Users/test/git/me/myagents",
            "branch": None,
            "started_at": "2026-04-19T00:01:00Z",
        },
        {
            "provider": "codex",
            "control_path": "unmanaged",
            "liveness_model": "process_scan",
            "provider_cli": {"path": codex_vendor_bin, "source": "process"},
            "pid": 11468,
            "workspace_label": "zerg",
            "cwd": "/Users/test/git/zerg",
            "branch": None,
            "started_at": "2026-04-19T00:00:30Z",
        },
    ]


def test_collect_unmanaged_processes_skips_codex_app_server_helpers(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    app_server = _FakeProc(
        pid=11470,
        cmdline=["/Applications/Codex.app/Contents/Resources/codex", "app-server", "--analytics-default-enabled"],
        create_time=now.timestamp(),
        env={},
    )
    _patch_process_iter(monkeypatch, [app_server])

    rows = local_health_service._collect_unmanaged_processes()

    assert rows == []


def test_collect_unmanaged_processes_skips_managed_codex_remote_tui(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    managed_wrapper = _FakeProc(
        pid=11471,
        cmdline=[
            "/opt/homebrew/bin/codex",
            "-c",
            "check_for_update_on_startup=false",
            "--dangerously-bypass-approvals-and-sandbox",
            "--enable",
            "tui_app_server",
            "--remote",
            "ws://127.0.0.1:51077",
        ],
        create_time=now.timestamp(),
        env={"LONGHOUSE_MANAGED_SESSION_ID": "sess-codex-managed"},
        cwd="/Users/test/git/zeta/athena-horizon",
    )
    _patch_process_iter(monkeypatch, [managed_wrapper])

    rows = local_health_service._collect_unmanaged_processes()

    assert rows == []


def test_collect_unmanaged_processes_detects_node_wrapped_opencode(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=11472,
        cmdline=["node", "/opt/homebrew/bin/opencode", "serve"],
        create_time=now.timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_unmanaged_processes()

    assert len(rows) == 1
    assert rows[0]["provider"] == "opencode"
    assert rows[0]["provider_cli"] == {"path": "node", "source": "process"}


def test_collect_unmanaged_processes_detects_node_wrapped_antigravity(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    proc = _FakeProc(
        pid=11472,
        cmdline=["node", "/opt/homebrew/bin/agy"],
        create_time=now.timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [proc])

    rows = local_health_service._collect_unmanaged_processes()

    assert len(rows) == 1
    assert rows[0]["provider"] == "antigravity"
    assert rows[0]["provider_cli"] == {"path": "node", "source": "process"}


def test_collect_unmanaged_processes_skips_longhouse_opencode_wrappers(monkeypatch):
    now = datetime(2026, 4, 19, 0, 0, 0, tzinfo=timezone.utc)
    direct_wrapper = _FakeProc(
        pid=11473,
        cmdline=["longhouse-opencode", "serve"],
        create_time=now.timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    node_wrapper = _FakeProc(
        pid=11474,
        cmdline=["node", "/usr/local/bin/longhouse-opencode", "serve"],
        create_time=now.timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    antigravity_wrapper = _FakeProc(
        pid=11475,
        cmdline=["node", "/usr/local/bin/longhouse-antigravity"],
        create_time=now.timestamp(),
        env={},
        cwd="/Users/test/git/zerg",
    )
    _patch_process_iter(monkeypatch, [direct_wrapper, node_wrapper, antigravity_wrapper])

    rows = local_health_service._collect_unmanaged_processes()

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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids={"sess-codex-1"})
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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

    rows = local_health_service._collect_managed_sessions_by_process(existing_session_ids=set())
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
        lambda *, existing_session_ids, phase_overlay=None, scanned_processes=None: [
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


def test_bridge_is_alive_keeps_state_when_lock_acquirable(tmp_path: Path) -> None:
    """A free flock means dead; caller keeps state until child cleanup is known."""
    state_file = tmp_path / "sess-dead.json"
    state_file.write_text("{}")
    lock_path = state_file.with_suffix(".lock")
    sock_path = state_file.with_suffix(".sock")
    lock_path.touch()
    sock_path.touch()

    assert local_health_service._bridge_is_alive(state_file) is False
    assert state_file.exists()
    assert lock_path.exists()
    assert sock_path.exists()


def test_bridge_is_alive_reports_dead_when_lock_missing(tmp_path: Path) -> None:
    """Legacy bridges (pre-flock) have no lock sidecar — treat as stale."""
    state_file = tmp_path / "sess-legacy.json"
    state_file.write_text("{}")

    assert local_health_service._bridge_is_alive(state_file) is False


def test_phase_freshness_rust_engine_matches_runtime_reducer() -> None:
    """Drift guard: engine phase freshness must stay aligned with the runtime reducer.

    The engine still filters `phase_ledger[]` using its own freshness map before
    exposing it in engine-status.json. That Rust map must match the hosted
    runtime map, plus the local-engine-only `finished` row window.
    """
    import re

    rust_path = Path(__file__).resolve().parents[2] / "engine" / "src" / "state" / "session_phase.rs"
    text = rust_path.read_text()
    # Strip line + block comments before parsing so a commented-out or
    # example tuple inside the const body can't sneak into `rust_map`.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    match = re.search(
        r"pub const PHASE_FRESHNESS_SECONDS:\s*&\[\(&str,\s*i64\)\]\s*=\s*&\[(.*?)\];",
        text,
        re.DOTALL,
    )
    assert match, "could not locate PHASE_FRESHNESS_SECONDS in Rust source"
    body = match.group(1)
    rust_map: dict[str, int] = {}
    entry_re = re.compile(r'\(\s*"(?P<phase>\w+)"\s*,\s*(?P<expr>[^)]+)\)')
    for m in entry_re.finditer(body):
        # The Rust source uses simple multiplications like `10 * 60`; eval is
        # fine because we match a tight regex first and the comments above
        # are already stripped.
        rust_map[m.group("phase")] = int(eval(m.group("expr"), {"__builtins__": {}}))

    expected = {phase: int(window.total_seconds()) for phase, window in session_runtime.PHASE_FRESHNESS.items()}
    expected["finished"] = 10 * 60
    assert rust_map == expected, f"rust={rust_map} expected={expected}"
