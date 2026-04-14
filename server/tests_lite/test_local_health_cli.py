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

from zerg.cli.main import app
from zerg.cli import local_health as local_health_cli
from zerg.services import local_health as local_health_service
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.longhouse_paths import get_agent_outbox_dir
from zerg.services.longhouse_paths import get_agent_status_path


def _service_info(status: str, *, service_file: str = "/Users/test/Library/LaunchAgents/com.longhouse.shipper.plist") -> dict:
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


def _write_local_config(tmp_path: Path, *, url: str, machine_name: str) -> None:
    machine_dir = tmp_path / "machine"
    machine_dir.mkdir(parents=True, exist_ok=True)
    (machine_dir / "target-url").write_text(url + "\n")
    (machine_dir / "name").write_text(machine_name + "\n")


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


def _write_service_plist(tmp_path: Path, *, machine_name: str) -> Path:
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
    path.write_bytes(plistlib.dumps(payload))
    return path


def _disable_real_runner_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(local_health_service, "_candidate_runner_env_paths", lambda: [tmp_path / "missing-runner.env"])


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


def test_collect_local_health_broken_when_service_stopped_with_stuck_outbox(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("stopped"))
    _write_outbox_file(tmp_path, age_seconds=300)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert "service_stopped" in snapshot["reasons"]
    assert "outbox_stuck" in snapshot["reasons"]


def test_collect_local_health_broken_when_launch_config_disagrees(monkeypatch, tmp_path: Path):
    service_file = _write_service_plist(tmp_path, machine_name="cinder.local")
    runner_env = _write_runner_env(tmp_path, url="https://david010.longhouse.ai", runner_name="cinder")
    _write_local_config(tmp_path, url="http://127.0.0.1:8080", machine_name="cinder.local")
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
    assert any("connect --install" in action for action in snapshot["suggested_actions"])


def test_collect_local_health_ignores_invalid_stored_url(monkeypatch, tmp_path: Path):
    service_file = _write_service_plist(tmp_path, machine_name="test-box")
    runner_env = _write_runner_env(tmp_path, url="https://david010.longhouse.ai", runner_name="cinder")
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
    assert any(
        action == "Run: longhouse connect --install --url https://david010.longhouse.ai --machine-name cinder"
        for action in snapshot["suggested_actions"]
    )


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
    monkeypatch.setattr(local_health_cli, "get_zerg_url", lambda config_dir=None: "https://david010.longhouse.ai")
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
# update_info in health snapshot
# ---------------------------------------------------------------------------


def _write_update_cache(tmp_path: Path, *, update_available: bool, installed: str = "0.1.8", latest: str = "0.1.9") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    path = tmp_path / "update-check.json"
    path.write_text(json.dumps(cache))
    return path


def test_collect_local_health_includes_update_info_when_update_available(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    # Point update_manager at our tmp dir for the cache file
    monkeypatch.setattr("zerg.cli.update_manager._get_longhouse_home", lambda: tmp_path)
    _write_update_cache(tmp_path, update_available=True, installed="0.1.8", latest="0.1.9")

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["update_info"] is not None
    info = snapshot["update_info"]
    assert info["update_available"] is True
    assert info["installed_version"] == "0.1.8"
    assert info["latest_version"] == "0.1.9"
    assert info["upgrade_command"] == "uv tool upgrade longhouse"
    assert "checked_at" in info


def test_collect_local_health_includes_update_info_when_up_to_date(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    monkeypatch.setattr("zerg.cli.update_manager._get_longhouse_home", lambda: tmp_path)
    _write_update_cache(tmp_path, update_available=False, installed="0.1.9", latest="0.1.9")

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["update_info"] is not None
    assert snapshot["update_info"]["update_available"] is False


def test_collect_local_health_update_info_is_none_when_no_cache(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    # No update-check.json written — cache is absent
    monkeypatch.setattr("zerg.cli.update_manager._get_longhouse_home", lambda: tmp_path)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["update_info"] is None


def test_collect_local_health_update_info_survives_corrupt_cache(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    monkeypatch.setattr("zerg.cli.update_manager._get_longhouse_home", lambda: tmp_path)
    (tmp_path / "update-check.json").write_text("not-json{{")

    snapshot = local_health_service.collect_local_health(tmp_path)

    # Corrupt cache must not crash — just returns None
    assert snapshot["update_info"] is None


def test_update_info_present_in_json_cli_output(monkeypatch, tmp_path: Path):
    _disable_real_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(local_health_service, "get_service_info", lambda *args, **kwargs: _service_info("running"))
    _write_engine_status(tmp_path / ".longhouse", age_seconds=5)

    monkeypatch.setattr("zerg.cli.update_manager._get_longhouse_home", lambda: tmp_path / ".longhouse")
    _write_update_cache(tmp_path / ".longhouse", update_available=True)

    runner = CliRunner()
    result = runner.invoke(app, ["local-health", "--json", "--claude-dir", str(tmp_path / ".claude")])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["update_info"]["update_available"] is True
