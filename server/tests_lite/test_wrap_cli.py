from __future__ import annotations

import json

from typer.testing import CliRunner

from zerg.cli import wrap as wrap_cli
from zerg.cli.main import app


def test_wrap_status_json(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(
        wrap_cli,
        "get_wrapper_status",
        lambda: {
            "claude": {"installed": True, "real_binary": "/usr/local/bin/claude"},
            "codex": {"installed": False, "real_binary": "/usr/local/bin/codex"},
            "profile": {"installed": True, "path": "/Users/test/.zshrc"},
        },
    )

    result = runner.invoke(app, ["wrap", "--status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claude"]["installed"] is True
    assert payload["profile"]["path"] == "/Users/test/.zshrc"


def test_wrap_install_json(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr(
        wrap_cli,
        "install_wrappers",
        lambda providers=None: {"claude": "installed in ~/.zshrc"},
    )

    result = runner.invoke(app, ["wrap", "--install", "--provider", "claude", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == "install"
    assert payload["providers"] == ["claude"]
    assert payload["results"]["claude"] == "installed in ~/.zshrc"
