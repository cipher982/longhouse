from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg import build_info
from zerg.cli.main import app


def _write_identity(tmp_path: Path, **overrides) -> Path:
    payload = {
        "version": "0.2.0",
        "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
        "commit_short": "b672fcca",
        "dirty": False,
        "built_at": "2026-04-21T18:03:12Z",
        "channel": "release",
    }
    payload.update(overrides)
    path = tmp_path / "build-identity.json"
    path.write_text(json.dumps(payload))
    return path


def test_longhouse_version_flag_release(tmp_path, monkeypatch):
    identity_file = _write_identity(tmp_path)
    monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(identity_file))
    build_info.reset_cache()

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0 (b672fcca)"


def test_longhouse_version_flag_dev_dirty(tmp_path, monkeypatch):
    identity_file = _write_identity(tmp_path, channel="dev", dirty=True)
    monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(identity_file))
    build_info.reset_cache()

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0-dev+b672fcca.dirty"
