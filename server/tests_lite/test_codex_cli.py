from __future__ import annotations

import json
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


def test_resolve_codex_binary_prefers_flag_then_env(monkeypatch, tmp_path):
    explicit = tmp_path / "codex-explicit"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    env_bin = tmp_path / "codex-env"
    env_bin.write_text("#!/bin/sh\n")
    env_bin.chmod(0o755)

    monkeypatch.setenv(codex_cli._CODEX_BIN_ENV, str(env_bin))

    assert codex_cli._resolve_codex_binary(str(explicit)) == str(explicit.resolve())
    assert codex_cli._resolve_codex_binary() == str(env_bin.resolve())


def test_resolve_codex_binary_prefers_installed_managed_runtime_before_ambient_path(monkeypatch):
    monkeypatch.delenv(codex_cli._CODEX_BIN_ENV, raising=False)
    monkeypatch.setattr(
        codex_cli,
        "resolve_installed_runtime_artifact",
        lambda component: type("Artifact", (), {"launch_path": "/tmp/codex"})()
        if component == codex_cli.RuntimeComponent.MANAGED_CODEX
        else None,
    )
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

    assert codex_cli._resolve_codex_binary() == "/tmp/codex"


def test_resolve_codex_binary_does_not_fall_back_to_ambient_path(monkeypatch):
    monkeypatch.delenv(codex_cli._CODEX_BIN_ENV, raising=False)
    monkeypatch.setattr(codex_cli, "resolve_installed_runtime_artifact", lambda component: None)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

    assert codex_cli._resolve_codex_binary() is None


def test_active_turn_survived_tui_exit_ignores_completed_rollout(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '\n'.join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-live"}}),
            ]
        )
        + '\n'
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready",
                "ws_url": "ws://127.0.0.1:4800",
                "thread_id": "thr-live",
                "thread_path": str(rollout),
                "active_turn_id": "turn-live",
                "last_turn_status": "inProgress",
            }
        )
    )
    monkeypatch.setattr(codex_cli, "_bridge_readyz_healthy", lambda *_args, **_kwargs: True)

    assert codex_cli._active_turn_survived_tui_exit(str(state_file)) is False


def test_active_turn_survived_tui_exit_preserves_live_turn(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}) + '\n'
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready",
                "ws_url": "ws://127.0.0.1:4800",
                "thread_id": "thr-live",
                "thread_path": str(rollout),
                "active_turn_id": "turn-live",
                "last_turn_status": "inProgress",
            }
        )
    )
    monkeypatch.setattr(codex_cli, "_bridge_readyz_healthy", lambda *_args, **_kwargs: True)

    assert codex_cli._active_turn_survived_tui_exit(str(state_file)) is True


def test_active_turn_survived_tui_exit_requires_healthy_readyz(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}) + '\n'
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready",
                "ws_url": "ws://127.0.0.1:4800",
                "thread_id": "thr-live",
                "thread_path": str(rollout),
                "active_turn_id": "turn-live",
                "last_turn_status": "inProgress",
            }
        )
    )
    monkeypatch.setattr(codex_cli, "_bridge_readyz_healthy", lambda *_args, **_kwargs: False)

    assert codex_cli._active_turn_survived_tui_exit(str(state_file)) is False


def test_active_turn_survived_tui_exit_checks_latest_rollout_when_active_turn_missing(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '\n'.join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-live"}}),
            ]
        )
        + '\n'
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "status": "ready",
                "ws_url": "ws://127.0.0.1:4800",
                "thread_id": "thr-live",
                "thread_path": str(rollout),
                "active_turn_id": None,
                "last_turn_status": "inProgress",
            }
        )
    )
    monkeypatch.setattr(codex_cli, "_bridge_readyz_healthy", lambda *_args, **_kwargs: True)

    assert codex_cli._active_turn_survived_tui_exit(str(state_file)) is False


def test_bridge_readyz_url_uses_http_readyz_endpoint():
    assert codex_cli._bridge_readyz_url("ws://127.0.0.1:61460") == "http://127.0.0.1:61460/readyz"
    assert codex_cli._bridge_readyz_url("wss://example.test/socket") == "https://example.test/socket/readyz"


def test_build_codex_attach_command_carries_managed_override_and_session_env():
    command = codex_cli._build_codex_attach_command(
        codex_bin="/tmp/codex",
        ws_url="ws://127.0.0.1:4800",
        bypass_approvals=False,
        session_id="session-123",
        thread_id="thr_123",
    )

    assert command.startswith("LONGHOUSE_MANAGED_SESSION_ID=session-123 ")
    assert "/tmp/codex -c check_for_update_on_startup=false resume thr_123" in command
    assert "--enable tui_app_server --remote ws://127.0.0.1:4800" in command


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
    bridge_calls: list[dict[str, object]] = []
    native_tui_calls: list[tuple[str, str, str, str, bool]] = []

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
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
    monkeypatch.setattr(codex_cli, "_resolve_codex_binary", lambda _explicit=None: "/tmp/codex")
    monkeypatch.setattr(
        codex_cli,
        "_start_native_codex_bridge",
        lambda **kwargs: bridge_calls.append(kwargs) or ("thr_123", "ws://127.0.0.1:4800", "/tmp/state.json"),
    )
    monkeypatch.setattr(codex_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(
        codex_cli,
        "_run_native_codex_tui",
        lambda *, session_id, codex_bin, ws_url, cwd, bypass_approvals=False: native_tui_calls.append(
            (session_id, codex_bin, ws_url, str(cwd), bypass_approvals)
        )
        or 0,
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
    assert bridge_calls == [
        {
            "session_id": "session-123",
            "cwd": tmp_path,
            "url": "https://longhouse.test",
            "token": "zdt_test_token",
            "codex_bin": "/tmp/codex",
        }
    ]
    assert native_tui_calls == [("session-123", "/tmp/codex", "ws://127.0.0.1:4800", str(tmp_path), False)]


def test_codex_command_stops_before_api_launch_when_preflight_fails(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "_resolve_codex_binary", lambda _explicit=None: "/tmp/codex")
    monkeypatch.setattr(codex_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        codex_cli,
        "_ensure_managed_launch_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit(claude_cli.EXIT_SETUP_FAILED)),
    )
    monkeypatch.setattr(
        codex_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs),
    )

    result = runner.invoke(
        app,
        [
            "codex",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
        ],
    )

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert launch_calls == []


def test_codex_command_exits_on_bridge_failure(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
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
    monkeypatch.setattr(codex_cli, "_resolve_codex_binary", lambda _explicit=None: "/tmp/codex")
    monkeypatch.setattr(
        codex_cli,
        "_start_native_codex_bridge",
        lambda **_kwargs: (_ for _ in ()).throw(codex_cli._NativeBridgeError("engine not found")),
    )

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "Codex bridge failed: engine not found" in result.output


def test_codex_command_exits_when_no_codex_runtime_available(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "_resolve_codex_binary", lambda _explicit=None: None)

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "Managed Codex runtime is not installed yet." in result.output
    assert "longhouse onboard" in result.output
    assert "longhouse machine repair" in result.output
