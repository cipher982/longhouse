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


def test_machine_reconcile_delegates_to_runtime_reconciler(monkeypatch):
    calls: list[dict[str, object]] = []
    runner = CliRunner()

    monkeypatch.setattr(
        machine_cli,
        "reconcile_local_runtime",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
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
        ),
    )

    result = runner.invoke(app, ["machine", "reconcile", "--claude-dir", "/tmp/.claude"])

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "claude_dir": "/tmp/.claude",
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
