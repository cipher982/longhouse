from __future__ import annotations

import os

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import claude as claude_cli
from zerg.cli import codex as codex_cli
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


def test_launch_managed_local_from_api_sets_codex_provider(monkeypatch, tmp_path):
    fake_client = _FakeClient(
        response=_FakeResponse(
            status_code=200,
            json_data={
                "session_id": "session-123",
                "provider_session_id": "provider-123",
                "attach_command": "",
                "source_runner_name": "work-laptop",
                "managed_transport": "codex_app_server",
            },
        )
    )

    monkeypatch.setattr(claude_cli, "_infer_git_context", lambda cwd: ("/tmp/repo", "main"))
    monkeypatch.setattr(claude_cli.httpx, "Client", lambda timeout: fake_client)

    result = codex_cli._launch_managed_local_from_api(
        url="https://longhouse.test",
        token="zdt_test_token",
        cwd=tmp_path,
        project="demo",
        loop_mode=SessionLoopMode.AUTOPILOT,
        name="Demo session",
        machine_name="work-laptop",
    )

    assert result.session_id == "session-123"
    assert result.provider_session_id == "provider-123"
    assert fake_client.calls == [
        {
            "url": "https://longhouse.test/api/sessions/managed-local/this-device",
            "headers": {"X-Agents-Token": "zdt_test_token"},
            "json": {
                "cwd": str(tmp_path),
                "provider": "codex",
                "project": "demo",
                "git_repo": "/tmp/repo",
                "git_branch": "main",
                "display_name": "Demo session",
                "loop_mode": "autopilot",
                "machine_name": "work-laptop",
            },
        }
    ]


def test_codex_command_starts_native_bridge_and_attaches(monkeypatch, tmp_path):
    runner = CliRunner()
    open_calls: list[str] = []
    native_tui_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        codex_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: codex_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
        ),
    )
    monkeypatch.setattr(codex_cli, "_start_native_codex_bridge", lambda **_kwargs: ("thr_123", "ws://127.0.0.1:4800"))
    monkeypatch.setattr(codex_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(
        codex_cli,
        "_run_native_codex_tui",
        lambda *, ws_url, cwd: native_tui_calls.append((ws_url, str(cwd))) or 0,
    )
    monkeypatch.setattr(codex_cli, "_open_session_url", lambda url: open_calls.append(url) or True)

    result = runner.invoke(
        app,
        [
            "codex",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
            "--loop-mode",
            "autopilot",
            "--name",
            "Demo session",
            "--open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Longhouse: https://longhouse.test" in result.output
    assert "Longhouse Codex session launched on this machine." in result.output
    assert "Session ID: session-123" in result.output
    assert "Session URL: https://longhouse.test/timeline/session-123" in result.output
    assert "Starting native Codex bridge..." in result.output
    assert "Codex thread: thr_123" in result.output
    assert "Remote target: ws://127.0.0.1:4800" in result.output
    assert "Opening session in browser..." in result.output
    assert "Attaching..." in result.output
    assert open_calls == ["https://longhouse.test/timeline/session-123"]
    assert native_tui_calls == [("ws://127.0.0.1:4800", str(tmp_path))]


def test_codex_command_exits_on_bridge_failure(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        codex_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: codex_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
        ),
    )
    monkeypatch.setattr(
        codex_cli,
        "_start_native_codex_bridge",
        lambda **_kwargs: (_ for _ in ()).throw(codex_cli._NativeBridgeError("engine not found")),
    )

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "Codex bridge failed: engine not found" in result.output
