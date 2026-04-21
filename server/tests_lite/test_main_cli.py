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


def _make_payload(**overrides) -> dict:
    payload = {
        "version": "0.2.0",
        "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
        "commit_short": "b672fcca",
        "dirty": False,
        "built_at": "2026-04-21T18:03:12Z",
        "channel": "release",
    }
    payload.update(overrides)
    return payload


class _FakeResource:
    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def is_file(self) -> bool:
        return self._raw is not None

    def read_text(self, encoding: str = "utf-8") -> str:
        assert self._raw is not None
        return self._raw

    def __truediv__(self, _other: str) -> "_FakeResource":
        return self


def _install_resource(monkeypatch, payload: dict | None) -> None:
    raw = None if payload is None else json.dumps(payload)
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(raw))
    build_info.reset_cache()


def test_longhouse_version_flag_release(monkeypatch):
    _install_resource(monkeypatch, _make_payload())

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0 (b672fcca)"


def test_longhouse_version_flag_dev_dirty(monkeypatch):
    _install_resource(monkeypatch, _make_payload(channel="dev", dirty=True))

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "longhouse 0.2.0-dev+b672fcca.dirty"


def test_longhouse_version_flag_json(monkeypatch):
    _install_resource(monkeypatch, _make_payload(channel="dev", dirty=True))

    runner = CliRunner()
    result = runner.invoke(app, ["--version", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["installed_version"] == "0.2.0-dev+b672fcca.dirty"
    assert payload["build"]["commit_short"] == "b672fcca"
    assert payload["build"]["channel"] == "dev"
    assert payload["build"]["dirty"] is True


def test_longhouse_version_flag_missing_identity(monkeypatch):
    _install_resource(monkeypatch, None)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 2, result.output
    combined = (result.output or "") + (result.stderr or "")
    assert "build identity missing" in combined


# Silence unused-path parameter if any pytest collector insists.
_ = Path
