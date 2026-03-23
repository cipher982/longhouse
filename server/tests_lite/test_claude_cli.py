from __future__ import annotations

import os

import pytest
from click.exceptions import Exit as ClickExit
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import claude as claude_cli
from zerg.cli.main import app
from zerg.session_loop_mode import SessionLoopMode


class _FakeResponse:
    def __init__(self, *, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self) -> dict:
        return self._json_data


class _FakeClient:
    def __init__(self, *, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, *, headers: dict[str, str], json: dict) -> _FakeResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
            }
        )
        return self.response


def test_load_api_credentials_requires_stored_url_and_token(tmp_path):
    with pytest.raises(ClickExit):
        claude_cli._load_api_credentials(url=None, token=None, config_dir=tmp_path)


def test_launch_managed_local_from_api_uses_this_device_endpoint(monkeypatch, tmp_path):
    fake_client = _FakeClient(
        response=_FakeResponse(
            status_code=200,
            json_data={
                "session_id": "session-123",
                "provider_session_id": "provider-123",
                "attach_command": "zsh -lc 'exec tmux attach -t lh-demo'",
                "source_runner_name": "work-laptop",
            },
        )
    )

    monkeypatch.setattr(claude_cli, "_infer_git_context", lambda cwd: ("/tmp/repo", "main"))
    monkeypatch.setattr(claude_cli.httpx, "Client", lambda timeout: fake_client)

    result = claude_cli._launch_managed_local_from_api(
        url="https://longhouse.test",
        token="zdt_test_token",
        cwd=tmp_path,
        project="demo",
        loop_mode=SessionLoopMode.ASSIST,
        name="Demo session",
        machine_name="work-laptop",
    )

    assert result.session_id == "session-123"
    assert result.provider_session_id == "provider-123"
    assert result.attach_command == "zsh -lc 'exec tmux attach -t lh-demo'"
    assert fake_client.calls == [
        {
            "url": "https://longhouse.test/api/sessions/managed-local/this-device",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "json": {
                "cwd": str(tmp_path),
                "provider": "claude",
                "project": "demo",
                "git_repo": "/tmp/repo",
                "git_branch": "main",
                "display_name": "Demo session",
                "loop_mode": "assist",
                "machine_name": "work-laptop",
            },
        }
    ]


def test_claude_command_prints_attach_command_and_auto_attaches(monkeypatch, tmp_path):
    runner = CliRunner()
    attach_calls: list[str] = []

    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec tmux attach -t lh-demo'",
            source_runner_name="work-laptop",
        ),
    )
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(claude_cli, "_run_attach_command", lambda command: attach_calls.append(command) or 0)

    result = runner.invoke(
        app,
        [
            "claude",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
            "--loop-mode",
            "assist",
            "--name",
            "Demo session",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Longhouse: https://longhouse.test" in result.output
    assert "Managed local Claude session launched on this device." in result.output
    assert "Session ID: session-123" in result.output
    assert "Provider session ID: provider-123" in result.output
    assert "Attach: zsh -lc 'exec tmux attach -t lh-demo'" in result.output
    assert "Attaching..." in result.output
    assert attach_calls == ["zsh -lc 'exec tmux attach -t lh-demo'"]
