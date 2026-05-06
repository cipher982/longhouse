# ruff: noqa: I001

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services import machine_repair
from zerg.services import local_health as local_health_service
from zerg.services import machine_state as machine_state_service
from zerg.services import local_runtime_installer as local_runtime_installer_service


def test_recommended_machine_repair_command_prefers_machine_repair_when_state_is_complete():
    assert (
        machine_repair.recommended_machine_repair_command(can_reconcile_from_state=True)
        == "Run: longhouse machine repair"
    )
    assert (
        machine_repair.recommended_machine_repair_command(can_reconcile_from_state=False)
        == "Run: longhouse connect --install"
    )


def test_can_repair_machine_from_state_requires_runtime_url_and_machine_name(tmp_path):
    assert machine_repair.can_repair_machine_from_state(state_root=tmp_path / ".longhouse") is False

    machine_state_service.write_machine_state(
        base_dir=tmp_path / ".longhouse",
        written_by="test",
        runtime_url="https://demo.longhouse.test",
        machine_name="cinder",
    )

    assert machine_repair.can_repair_machine_from_state(state_root=tmp_path / ".longhouse") is True


def test_replay_machine_backlog_accepts_log_prefixed_json(monkeypatch):
    monkeypatch.setattr(machine_repair, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(
        machine_repair.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='2026-04-23T17:12:43Z INFO replaying\\n{"status":"ok","spool_replayed":2,"spool_pending":0}\n',
            stderr="",
        ),
    )

    result = machine_repair.replay_machine_backlog(
        url="https://demo.longhouse.test",
        token="zdt_test",
        claude_dir="/tmp/.claude",
    )

    assert result.attempted is True
    assert result.success is True
    assert result.warning is None
    assert result.summary == {"status": "ok", "spool_replayed": 2, "spool_pending": 0}


def test_replay_machine_backlog_ignores_trailing_output_after_json(monkeypatch):
    monkeypatch.setattr(machine_repair, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(
        machine_repair.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='2026-04-23T17:12:43Z INFO replaying\\n{"status":"ok","spool_replayed":3,"spool_pending":1}\\n2026-04-23T17:12:44Z INFO done\\n',
            stderr="",
        ),
    )

    result = machine_repair.replay_machine_backlog(
        url="https://demo.longhouse.test",
        token="zdt_test",
        claude_dir="/tmp/.claude",
    )

    assert result.attempted is True
    assert result.success is True
    assert result.summary == {"status": "ok", "spool_replayed": 3, "spool_pending": 1}


def test_replay_machine_backlog_reports_progress_summary(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(machine_repair, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(
        machine_repair.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"status":"ok","spool_replayed":2,"spool_pending":1,"spool_dead":0}\n',
            stderr="",
        ),
    )

    result = machine_repair.replay_machine_backlog(
        url="https://demo.longhouse.test",
        token="zdt_test",
        claude_dir="/tmp/.claude",
        progress=events.append,
    )

    assert result.success is True
    assert events == [
        "Starting queued shipping replay with longhouse-engine ship --json.",
        "Queued shipping replay finished: replayed=2, pending=1, dead=0.",
    ]


def test_replay_machine_backlog_reports_timeout_progress(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(machine_repair, "get_engine_executable", lambda: "/tmp/longhouse-engine")

    def fail_with_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["longhouse-engine", "ship"], timeout=30)

    monkeypatch.setattr(machine_repair.subprocess, "run", fail_with_timeout)

    result = machine_repair.replay_machine_backlog(
        url="https://demo.longhouse.test",
        token="zdt_test",
        claude_dir="/tmp/.claude",
        progress=events.append,
    )

    assert result.success is False
    assert "timed out after 30 seconds" in str(result.warning)
    assert events == [
        "Starting queued shipping replay with longhouse-engine ship --json.",
        "Queued shipping replay timed out after 30 seconds; the Machine Agent will keep retrying in the background.",
    ]


def test_repair_machine_runtime_reconciles_replays_and_collects_health(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        local_runtime_installer_service,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(("reconcile", kwargs))
        or SimpleNamespace(
            machine_state=SimpleNamespace(
                runtime_url="https://demo.longhouse.test",
                config_generation="20260423-test",
            ),
            install_result=SimpleNamespace(machine_name="cinder"),
        ),
    )
    monkeypatch.setattr(
        machine_repair,
        "load_token",
        lambda config_dir: calls.append(("load_token", config_dir)) or "zdt_test",
    )
    monkeypatch.setattr(
        machine_repair,
        "replay_machine_backlog",
        lambda **kwargs: calls.append(("replay", kwargs))
        or machine_repair.SpoolReplayResult(
            attempted=True,
            success=True,
            summary={"spool_replayed": 1, "spool_pending": 0},
        ),
    )
    monkeypatch.setattr(
        local_health_service,
        "collect_local_health",
        lambda state_root: calls.append(("health", state_root))
        or {"health_state": "healthy", "severity": "green", "headline": "Longhouse shipping healthy"},
    )

    result = machine_repair.repair_machine_runtime(claude_dir=str(tmp_path / ".claude"))

    assert result.spool_replay.success is True
    assert result.health_snapshot["health_state"] == "healthy"
    assert calls == [
        ("reconcile", {"claude_dir": str(tmp_path / ".claude"), "written_by": "machine-repair"}),
        ("load_token", tmp_path / ".longhouse"),
        (
            "replay",
            {
                "url": "https://demo.longhouse.test",
                "token": "zdt_test",
                "claude_dir": str(tmp_path / ".claude"),
                "progress": None,
            },
        ),
        ("health", tmp_path / ".longhouse"),
    ]


def test_repair_machine_runtime_reports_progress_steps(monkeypatch, tmp_path):
    events: list[str] = []
    monkeypatch.setattr(
        local_runtime_installer_service,
        "reconcile_local_runtime",
        lambda **kwargs: SimpleNamespace(
            machine_state=SimpleNamespace(
                runtime_url="https://demo.longhouse.test",
                config_generation="20260423-test",
            ),
            install_result=SimpleNamespace(machine_name="cinder"),
        ),
    )
    monkeypatch.setattr(machine_repair, "load_token", lambda config_dir: "zdt_test")

    def replay_stub(**kwargs):
        assert callable(kwargs["progress"])
        kwargs["progress"]("queued replay progress")
        return machine_repair.SpoolReplayResult(
            attempted=True,
            success=True,
            summary={"spool_replayed": 1, "spool_pending": 0},
        )

    monkeypatch.setattr(machine_repair, "replay_machine_backlog", replay_stub)
    monkeypatch.setattr(
        local_health_service,
        "collect_local_health",
        lambda state_root: {"health_state": "healthy", "severity": "green", "headline": "Longhouse shipping healthy"},
    )

    result = machine_repair.repair_machine_runtime(claude_dir=str(tmp_path / ".claude"), progress=events.append)

    assert result.spool_replay.success is True
    assert events == [
        "Step 1/4: reconciling local runtime from canonical machine state.",
        "Step 2/4: replaying queued shipping backlog.",
        "queued replay progress",
        "Step 3/4: collecting post-repair local health snapshot.",
        "Step 4/4: repair complete; local health is healthy.",
    ]


def test_repair_machine_runtime_skips_backlog_replay_without_token(monkeypatch, tmp_path):
    replay_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        local_runtime_installer_service,
        "reconcile_local_runtime",
        lambda **kwargs: SimpleNamespace(
            machine_state=SimpleNamespace(
                runtime_url="https://demo.longhouse.test",
                config_generation="20260423-test",
            ),
            install_result=SimpleNamespace(machine_name="cinder"),
        ),
    )
    monkeypatch.setattr(machine_repair, "load_token", lambda config_dir: None)
    monkeypatch.setattr(
        machine_repair,
        "replay_machine_backlog",
        lambda **kwargs: replay_calls.append(kwargs),
    )
    monkeypatch.setattr(
        local_health_service,
        "collect_local_health",
        lambda state_root: {"health_state": "degraded", "severity": "yellow", "headline": "Launch ready"},
    )

    result = machine_repair.repair_machine_runtime(claude_dir=str(tmp_path / ".claude"))

    assert replay_calls == []
    assert result.spool_replay.attempted is False
    assert result.spool_replay.warning == "No device token configured; skipped queued shipping replay."
