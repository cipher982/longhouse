from __future__ import annotations

import json
import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli.main import app
from zerg.cli import runtime_artifact_smoke
from zerg.services.runtime_artifacts import RuntimeArtifactKind
from zerg.services.runtime_artifacts import RuntimeComponent


def test_runtime_artifact_smoke_command_outputs_json(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        runtime_artifact_smoke,
        "ensure_runtime_artifact",
        lambda component, overwrite=False: SimpleNamespace(
            component=component,
            path="/tmp/longhouse-engine",
            launch_path="/tmp/longhouse-engine",
            source="https://github.com/cipher982/longhouse/releases/download/v0.1.8/longhouse-engine-linux-x64",
            installed_now=True,
            kind=RuntimeArtifactKind.EXECUTABLE,
        ),
    )

    result = runner.invoke(app, ["runtime-artifact-smoke", "engine", "--overwrite", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "component": RuntimeComponent.ENGINE.value,
        "path": "/tmp/longhouse-engine",
        "launch_path": "/tmp/longhouse-engine",
        "source": "https://github.com/cipher982/longhouse/releases/download/v0.1.8/longhouse-engine-linux-x64",
        "installed_now": True,
        "kind": RuntimeArtifactKind.EXECUTABLE.value,
    }


def test_runtime_artifact_smoke_command_surfaces_install_errors(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        runtime_artifact_smoke,
        "ensure_runtime_artifact",
        lambda component, overwrite=False: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = runner.invoke(app, ["runtime-artifact-smoke", "engine"])

    assert result.exit_code == 1
    assert "boom" in result.output


def test_runtime_artifact_install_command_outputs_json(monkeypatch):
    runner = CliRunner()

    monkeypatch.setattr(
        runtime_artifact_smoke,
        "ensure_runtime_artifact",
        lambda component, overwrite=False: SimpleNamespace(
            component=component,
            path="/Applications/Longhouse.app",
            launch_path="/Applications/Longhouse.app/Contents/MacOS/Longhouse",
            source="https://github.com/cipher982/longhouse/releases/download/v0.1.8/Longhouse-macos-arm64.zip",
            installed_now=False,
            kind=RuntimeArtifactKind.APP_BUNDLE,
        ),
    )

    result = runner.invoke(app, ["runtime-artifact-install", "desktop-app", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "component": RuntimeComponent.DESKTOP_APP.value,
        "path": "/Applications/Longhouse.app",
        "launch_path": "/Applications/Longhouse.app/Contents/MacOS/Longhouse",
        "source": "https://github.com/cipher982/longhouse/releases/download/v0.1.8/Longhouse-macos-arm64.zip",
        "installed_now": False,
        "kind": RuntimeArtifactKind.APP_BUNDLE.value,
    }
