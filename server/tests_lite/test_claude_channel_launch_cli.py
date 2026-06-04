from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from typer.testing import CliRunner

from zerg.cli.claude_channel import app

_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _invoke_launch(monkeypatch, tmp_path, extra_args):
    """Invoke `claude-channel launch`, capturing the kwargs passed downstream."""

    captured: dict = {}

    def _fake_launch(**kwargs):
        captured.update(kwargs)
        return {"session_id": kwargs["session_id"], "ok": True}

    # The launch command imports the detached launcher lazily from zerg.cli.claude.
    import zerg.cli.claude as claude_module

    monkeypatch.setattr(claude_module, "_launch_detached_native_claude_channel", _fake_launch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "launch",
            "--session-id",
            _SESSION_ID,
            "--cwd",
            str(tmp_path),
            "--api-url",
            "https://example.test",
            "--api-token",
            "device-token",
            *extra_args,
        ],
    )
    return result, captured


def test_launch_without_resume_passes_resume_false(monkeypatch, tmp_path):
    result, captured = _invoke_launch(monkeypatch, tmp_path, [])
    assert result.exit_code == 0, result.output
    assert captured["resume"] is False


def test_launch_with_resume_flag_passes_resume_true(monkeypatch, tmp_path):
    result, captured = _invoke_launch(monkeypatch, tmp_path, ["--resume"])
    assert result.exit_code == 0, result.output
    assert captured["resume"] is True
    # The longhouse session id is always passed through unchanged.
    assert captured["session_id"] == _SESSION_ID
