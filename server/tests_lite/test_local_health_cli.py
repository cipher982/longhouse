from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli.main import app
from zerg.services import local_health as local_health_service


def _service_info(status: str) -> dict:
    return {
        "platform": "macos",
        "status": status,
        "service_name": "com.longhouse.shipper",
        "service_file": "/Users/test/Library/LaunchAgents/com.longhouse.shipper.plist",
        "log_path": "/Users/test/.claude/logs/engine.log.*",
    }


def _write_engine_status(tmp_path: Path, *, age_seconds: int = 0, payload: dict | None = None) -> None:
    claude_dir = tmp_path
    claude_dir.mkdir(parents=True, exist_ok=True)
    status_path = claude_dir / "engine-status.json"
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
    outbox_dir = tmp_path / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / name
    path.write_text(json.dumps({"session_id": "sess-1", "state": "thinking"}))
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))


def test_collect_local_health_healthy(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_health_service, "get_service_info", lambda: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=5)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "healthy"
    assert snapshot["severity"] == "green"
    assert snapshot["headline"] == "Longhouse shipping healthy"
    assert snapshot["engine_status"]["fresh"] is True


def test_collect_local_health_degraded_while_waiting_for_first_status(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_health_service, "get_service_info", lambda: _service_info("running"))

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert snapshot["severity"] == "yellow"
    assert "engine_status_missing" in snapshot["reasons"]
    assert "first local status update" in snapshot["headline"].lower()


def test_collect_local_health_degraded_when_status_is_aging(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_health_service, "get_service_info", lambda: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=90)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "degraded"
    assert "engine_status_aging" in snapshot["reasons"]
    assert "aging" in snapshot["headline"].lower()


def test_collect_local_health_broken_when_service_stopped_with_stuck_outbox(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(local_health_service, "get_service_info", lambda: _service_info("stopped"))
    _write_outbox_file(tmp_path, age_seconds=300)

    snapshot = local_health_service.collect_local_health(tmp_path)

    assert snapshot["health_state"] == "broken"
    assert snapshot["severity"] == "red"
    assert "service_stopped" in snapshot["reasons"]
    assert "outbox_stuck" in snapshot["reasons"]


def test_local_health_command_json_output(monkeypatch, tmp_path: Path):
    runner = CliRunner()
    monkeypatch.setattr(local_health_service, "get_service_info", lambda: _service_info("running"))
    _write_engine_status(tmp_path, age_seconds=2)

    result = runner.invoke(app, ["local-health", "--json", "--claude-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["health_state"] == "healthy"
    assert payload["service"]["status"] == "running"
    assert payload["engine_status"]["exists"] is True
