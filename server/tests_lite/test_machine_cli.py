# ruff: noqa: I001

from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import machine as machine_cli
from zerg.cli.main import app


def _fake_reconcile_result():
    return SimpleNamespace(
        machine_state=SimpleNamespace(
            config_generation="20260414-test",
            runtime_url="https://demo.longhouse.test",
        ),
        install_result=SimpleNamespace(
            machine_name="cinder",
            engine_runtime=SimpleNamespace(path="/tmp/longhouse-engine", installed_now=False),
            service_result={
                "message": "ok",
                "service": "launchd",
                "plist_path": "/tmp/test.plist",
            },
            hooks=SimpleNamespace(actions=["hooks installed"], warning=None),
            desktop_app_result=None,
        ),
    )


def _fake_repair_result():
    return SimpleNamespace(
        reconcile_result=_fake_reconcile_result(),
        spool_replay=SimpleNamespace(
            success=True,
            warning=None,
            summary={"spool_replayed": 2, "spool_pending": 0},
        ),
        health_snapshot={
            "health_state": "healthy",
            "severity": "green",
            "headline": "Longhouse shipping healthy",
            "reasons": [],
            "suggested_actions": [],
        },
    )


def test_machine_reconcile_delegates_to_runtime_reconciler(monkeypatch):
    calls: list[dict[str, object]] = []
    runner = CliRunner()

    monkeypatch.setattr(
        machine_cli,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(kwargs) or _fake_reconcile_result(),
    )

    result = runner.invoke(app, ["machine", "reconcile", "--claude-dir", "/tmp/.claude"])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "claude_dir": "/tmp/.claude",
            "machine_name": None,
            "menubar": None,
            "runtime_url": None,
            "topology_intent": None,
            "written_by": "machine-reconcile",
        }
    ]
    assert "Reconciled machine generation 20260414-test" in result.output
    assert "URL: https://demo.longhouse.test" in result.output


def test_machine_reconcile_reports_missing_machine_state(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        machine_cli,
        "reconcile_local_runtime",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Machine state missing runtime_url")),
    )

    result = runner.invoke(app, ["machine", "reconcile"])

    assert result.exit_code == 1
    assert "Machine state missing runtime_url" in result.output


def test_machine_configure_reconciles_with_overrides(monkeypatch):
    calls: list[dict[str, object]] = []
    runner = CliRunner()

    monkeypatch.setattr(
        machine_cli,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(kwargs) or _fake_reconcile_result(),
    )

    result = runner.invoke(
        app,
        [
            "machine",
            "configure",
            "--url",
            "https://prod.longhouse.test",
            "--machine-name",
            "Cinder Local",
            "--topology-intent",
            "connect-remote",
            "--menubar",
            "--claude-dir",
            "/tmp/.claude",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "claude_dir": "/tmp/.claude",
            "machine_name": "Cinder Local",
            "menubar": True,
            "runtime_url": "https://prod.longhouse.test",
            "topology_intent": "connect-remote",
            "written_by": "machine-configure",
        }
    ]
    assert "Updated machine config and reconciled machine generation 20260414-test" in result.output
    assert "--topology-intent is legacy metadata" in result.output


def test_machine_configure_requires_override():
    runner = CliRunner()

    result = runner.invoke(app, ["machine", "configure"])

    assert result.exit_code == 1
    assert "Specify at least one config override" in result.output


def test_machine_repair_delegates_to_canonical_repair_flow(monkeypatch):
    runner = CliRunner()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        machine_cli,
        "repair_machine_runtime",
        lambda **kwargs: calls.append(kwargs) or _fake_repair_result(),
    )

    result = runner.invoke(app, ["machine", "repair", "--claude-dir", "/tmp/.claude"])

    assert result.exit_code == 0, result.output
    assert calls == [{"claude_dir": "/tmp/.claude"}]
    assert "Repaired machine generation 20260414-test" in result.output
    assert "Queued shipping replayed=2, pending=0" in result.output
    assert "Longhouse shipping healthy" in result.output


def test_machine_repair_reports_missing_machine_state(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        machine_cli,
        "repair_machine_runtime",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Machine state missing runtime_url")),
    )

    result = runner.invoke(app, ["machine", "repair"])

    assert result.exit_code == 1
    assert "Machine state missing runtime_url" in result.output
