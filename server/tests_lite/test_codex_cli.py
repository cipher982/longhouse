from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import claude as claude_cli
from zerg.cli import codex as codex_cli
from zerg.cli.main import app
from zerg.services.managed_session_contracts import list_managed_session_contracts
from zerg.session_loop_mode import SessionLoopMode


@pytest.fixture(autouse=True)
def _isolate_longhouse_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))


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

    monkeypatch.setenv(codex_cli.CODEX_BIN_ENV, str(env_bin))

    assert codex_cli._resolve_codex_binary(str(explicit)) == str(explicit.resolve())
    assert codex_cli._resolve_codex_binary() == str(env_bin.resolve())
    assert codex_cli._resolve_codex_binary_with_source() == {
        "path": str(env_bin.resolve()),
        "source": codex_cli.CODEX_BIN_ENV,
    }


def test_resolve_codex_binary_uses_codex_on_path(monkeypatch):
    monkeypatch.delenv(codex_cli.CODEX_BIN_ENV, raising=False)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

    assert codex_cli._resolve_codex_binary() == "/usr/local/bin/codex"
    assert codex_cli._resolve_codex_binary_with_source() == {"path": "/usr/local/bin/codex", "source": "PATH"}


def test_resolve_codex_binary_returns_none_when_codex_is_missing(monkeypatch):
    monkeypatch.delenv(codex_cli.CODEX_BIN_ENV, raising=False)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: None)

    assert codex_cli._resolve_codex_binary() is None
    assert codex_cli._resolve_codex_binary_with_source() == {"path": None, "source": "missing"}


def test_codex_version_uses_operator_timeout(monkeypatch):
    def fake_run(*_args, **kwargs):
        assert kwargs["timeout"] == codex_cli._CODEX_VERSION_TIMEOUT_SECONDS
        return SimpleNamespace(returncode=0, stdout="codex-cli 9.9.9\n", stderr="")

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    assert codex_cli._codex_version("/tmp/codex") == {"ok": True, "value": "codex-cli 9.9.9", "error": None}


def test_emit_warp_cli_agent_event_writes_osc777(monkeypatch, tmp_path):
    output = StringIO()

    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
    monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "1")
    monkeypatch.setenv("WARP_CLIENT_VERSION", "v0.2026.04.15.08.45.stable_02")
    monkeypatch.setattr(output, "isatty", lambda: True)
    original_open = Path.open

    class FakeTty:
        def __enter__(self):
            return output

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_open(self, *args, **kwargs):
        if str(self) == "/dev/tty":
            return FakeTty()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    codex_cli._emit_warp_cli_agent_event(
        event="session_start",
        session_id="session-123",
        cwd=tmp_path,
        project="demo",
    )

    marker = output.getvalue()
    assert marker.startswith("\033]777;notify;warp://cli-agent;")
    assert marker.endswith("\a")
    payload = json.loads(marker.removeprefix("\033]777;notify;warp://cli-agent;").removesuffix("\a"))
    assert payload == {
        "v": 1,
        "agent": "codex",
        "event": "session_start",
        "session_id": "session-123",
        "cwd": str(tmp_path),
        "project": "demo",
    }


def test_emit_warp_cli_agent_event_skips_non_warp_terminal(monkeypatch, tmp_path):
    output = StringIO()

    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "1")
    monkeypatch.setenv("WARP_CLIENT_VERSION", "v0.2026.04.15.08.45.stable_02")
    monkeypatch.setattr(output, "isatty", lambda: True)
    monkeypatch.setattr(codex_cli.sys, "stdout", output)

    codex_cli._emit_warp_cli_agent_event(
        event="session_start",
        session_id="session-123",
        cwd=tmp_path,
        project=None,
    )

    assert output.getvalue() == ""


def test_codex_doctor_reports_binary_legacy_artifacts_and_bridge_state(monkeypatch, tmp_path):
    runner = CliRunner()
    home = tmp_path / "home"
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\necho codex-cli 9.9.9\n")
    codex_bin.chmod(0o755)
    launcher = home / ".local" / "bin" / "longhouse-codex"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f"#!/bin/sh\n{codex_cli.LEGACY_MANAGED_CODEX_LAUNCHER_MARKER}\n")
    runtime_dir = home / ".longhouse" / "runtimes" / "codex"
    runtime_dir.mkdir(parents=True)
    state_root = tmp_path / "bridge"
    state_root.mkdir()
    state_file = state_root / "session-123.json"
    state_file.write_text(
        json.dumps(
            {
                "session_id": "session-123",
                "cwd": str(tmp_path),
                "codex_bin": str(codex_bin),
                "ws_url": "ws://127.0.0.1:49999",
                "thread_id": "thread-123",
                "thread_path": "",
                "pid": os.getpid(),
                "status": "ready",
                "log_file": str(state_root / "session-123.log"),
                "active_turn_id": None,
                "last_turn_status": None,
                "last_error": None,
                "updated_at": "2026-04-25T00:00:00Z",
            }
        )
    )
    state_file.with_suffix(".lock").touch()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(codex_cli.CODEX_BIN_ENV, raising=False)
    monkeypatch.setattr(
        codex_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="codex-cli 9.9.9\n", stderr=""),
    )
    readyz_calls: list[str | None] = []
    monkeypatch.setattr(codex_cli, "_bridge_readyz_healthy", lambda ws_url: readyz_calls.append(ws_url) or True)

    result = runner.invoke(
        app,
        ["codex", "doctor", "--codex-bin", str(codex_bin), "--state-root", str(state_root), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"codex_binary", "legacy_artifacts", "bridge"}
    assert set(payload["codex_binary"]) == {"path", "source", "version", "env_override"}
    assert set(payload["legacy_artifacts"]) == {"launcher", "managed_runtime_dir"}
    assert set(payload["bridge"]) == {"state_root", "state_root_exists", "readyz_checked", "sessions"}
    assert payload["codex_binary"]["path"] == str(codex_bin.resolve())
    assert payload["codex_binary"]["source"] == "--codex-bin"
    assert payload["codex_binary"]["version"] == {"ok": True, "value": "codex-cli 9.9.9", "error": None}
    assert payload["legacy_artifacts"]["launcher"]["exists"] is True
    assert payload["legacy_artifacts"]["launcher"]["legacy_marker"] is True
    assert payload["legacy_artifacts"]["managed_runtime_dir"]["exists"] is True
    assert payload["bridge"]["state_root"] == str(state_root)
    assert set(payload["bridge"]["sessions"][0]) == {
        "session_id",
        "state_file",
        "log_file",
        "readable",
        "status",
        "pid",
        "pid_alive",
        "app_server_pid",
        "app_server_pid_alive",
        "app_server_pgid",
        "app_server_ws_url",
        "lock_file",
        "lock_file_exists",
        "lock_held",
        "codex_bin",
        "ws_url",
        "readyz_healthy",
        "thread_id",
        "thread_path",
        "last_turn_status",
        "active_turn_id",
        "updated_at",
    }
    assert payload["bridge"]["sessions"][0]["session_id"] == "session-123"
    assert payload["bridge"]["sessions"][0]["pid_alive"] is True
    assert payload["bridge"]["sessions"][0]["lock_file_exists"] is True
    assert payload["bridge"]["sessions"][0]["readyz_healthy"] is None
    assert readyz_calls == []

    result = runner.invoke(
        app,
        [
            "codex",
            "doctor",
            "--codex-bin",
            str(codex_bin),
            "--state-root",
            str(state_root),
            "--check-readyz",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bridge"]["readyz_checked"] is True
    assert payload["bridge"]["sessions"][0]["readyz_healthy"] is True
    assert readyz_calls == ["ws://127.0.0.1:49999"]


def test_active_turn_survived_tui_exit_ignores_completed_rollout(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-live"}}),
            ]
        )
        + "\n"
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
        json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}) + "\n"
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
        json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}) + "\n"
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
        "\n".join(
            [
                json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-live"}}),
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-live"}}),
            ]
        )
        + "\n"
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
    assert "/tmp/codex -c check_for_update_on_startup=false" in command
    assert "resume thr_123" not in command
    assert "--enable tui_app_server --remote ws://127.0.0.1:4800" in command


def test_build_codex_attach_command_carries_model_overrides():
    command = codex_cli._build_codex_attach_command(
        codex_bin="/tmp/codex",
        ws_url="ws://127.0.0.1:4800",
        bypass_approvals=False,
        model="gpt-5.4-mini",
        model_reasoning_effort="low",
        session_id="session-123",
    )

    assert "-c model_reasoning_effort=low --model gpt-5.4-mini" in command


def test_start_native_codex_bridge_can_prestart_initial_thread(monkeypatch, tmp_path):
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ws_url": "ws://127.0.0.1:4800",
                    "thread_id": "thr_123",
                    "state_file": "/tmp/state.json",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(codex_cli, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    thread_id, ws_url, state_file = codex_cli._start_native_codex_bridge(
        session_id="session-123",
        cwd=tmp_path,
        url="https://longhouse.test",
        token="zdt_test_token",
        codex_bin="/tmp/codex",
        create_initial_thread=True,
    )

    assert (thread_id, ws_url, state_file) == ("thr_123", "ws://127.0.0.1:4800", "/tmp/state.json")
    assert "--create-initial-thread" in calls[0]["command"]


def test_start_native_codex_bridge_requires_thread_id_when_prestarting(monkeypatch, tmp_path):
    def fake_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ws_url": "ws://127.0.0.1:4800", "state_file": "/tmp/state.json"}),
            stderr="",
        )

    monkeypatch.setattr(codex_cli, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    with pytest.raises(codex_cli._NativeBridgeError, match="did not return thread_id"):
        codex_cli._start_native_codex_bridge(
            session_id="session-123",
            cwd=tmp_path,
            url="https://longhouse.test",
            token="zdt_test_token",
            codex_bin="/tmp/codex",
            create_initial_thread=True,
        )


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
    native_tui_calls: list[tuple[str, str, str, str, bool, str | None, str | None, str | None]] = []
    stop_calls: list[dict[str, object]] = []
    provider_home = tmp_path / ".claude"

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
        lambda *,
        session_id,
        codex_bin,
        ws_url,
        cwd,
        bypass_approvals=False,
        model=None,
        model_reasoning_effort=None,
        thread_id=None: native_tui_calls.append(
            (session_id, codex_bin, ws_url, str(cwd), bypass_approvals, model, model_reasoning_effort, thread_id)
        )
        or 0,
    )
    monkeypatch.setattr(codex_cli, "_open_session_url", lambda url: open_calls.append(url) or True)
    monkeypatch.setattr(codex_cli, "_stop_native_codex_bridge", lambda **kwargs: stop_calls.append(kwargs) or None)

    result = runner.invoke(
        app,
        [
            "codex",
            "--cwd",
            str(tmp_path),
            "--config-dir",
            str(provider_home),
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
            "model": None,
            "model_reasoning_effort": None,
            "create_initial_thread": True,
        }
    ]
    assert native_tui_calls == [
        ("session-123", "/tmp/codex", "ws://127.0.0.1:4800", str(tmp_path), False, None, None, "thr_123")
    ]
    assert list_managed_session_contracts(tmp_path / ".longhouse") == []
    assert stop_calls == [
        {
            "session_id": "session-123",
            "reason": codex_cli._CODEX_STOP_REASON_TERMINAL_DISCONNECTED,
            "timeout_secs": None,
        }
    ]


def test_codex_command_fails_before_tui_when_prestart_lacks_thread(monkeypatch, tmp_path):
    runner = CliRunner()
    native_tui_calls: list[dict[str, object]] = []

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

    def fake_start_bridge(**_kwargs):
        raise codex_cli._NativeBridgeError("Native Codex bridge did not return thread_id")

    monkeypatch.setattr(codex_cli, "_start_native_codex_bridge", fake_start_bridge)
    monkeypatch.setattr(codex_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(codex_cli, "_run_native_codex_tui", lambda **kwargs: native_tui_calls.append(kwargs) or 0)

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "Codex bridge failed: Native Codex bridge did not return thread_id" in result.output
    assert native_tui_calls == []


def test_codex_command_no_attach_prints_attach_command(monkeypatch, tmp_path):
    runner = CliRunner()
    provider_home = tmp_path / ".claude"

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
        lambda **_kwargs: ("thr_123", "ws://127.0.0.1:4800", "/tmp/state.json"),
    )

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path), "--config-dir", str(provider_home), "--no-attach"])

    assert result.exit_code == 0, result.output
    assert "Attach: LONGHOUSE_MANAGED_SESSION_ID=session-123" in result.output
    assert "/tmp/codex -c check_for_update_on_startup=false" in result.output
    assert "resume thr_123" not in result.output
    contracts = list_managed_session_contracts(tmp_path / ".longhouse")
    assert contracts[0]["provider"] == "codex"
    assert contracts[0]["workspace"]["cwd"] == str(tmp_path)
    assert contracts[0]["control"]["state_path"] == "/tmp/state.json"
    assert contracts[0]["launch_mode"] == "detached_ui"
    assert not (provider_home / "managed-local" / "contracts").exists()


def test_codex_command_preserves_bridge_when_active_turn_survives(monkeypatch, tmp_path):
    runner = CliRunner()
    stop_calls: list[dict[str, object]] = []

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
        lambda **_kwargs: ("thr_123", "ws://127.0.0.1:4800", "/tmp/state.json"),
    )
    monkeypatch.setattr(codex_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(codex_cli, "_run_native_codex_tui", lambda **_kwargs: 7)
    monkeypatch.setattr(codex_cli, "_active_turn_survived_tui_exit", lambda state_file: state_file == "/tmp/state.json")
    monkeypatch.setattr(codex_cli, "_stop_native_codex_bridge", lambda **kwargs: stop_calls.append(kwargs) or None)

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "still running and reattachable" in result.output
    assert stop_calls == []


def test_codex_command_signal_cleanup_stops_once(monkeypatch, tmp_path):
    runner = CliRunner()
    stop_calls: list[dict[str, object]] = []
    signal_handlers: dict[object, object] = {}

    def fake_signal(sig, handler):
        if callable(handler):
            signal_handlers[sig] = handler
        return "old-handler"

    def fake_run_native_tui(**_kwargs):
        signal_handlers[codex_cli.signal.SIGHUP](codex_cli.signal.SIGHUP, None)
        raise AssertionError("signal handler should exit")

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
        lambda **_kwargs: ("thr_123", "ws://127.0.0.1:4800", "/tmp/state.json"),
    )
    monkeypatch.setattr(codex_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(codex_cli, "_run_native_codex_tui", fake_run_native_tui)
    monkeypatch.setattr(codex_cli, "_stop_native_codex_bridge", lambda **kwargs: stop_calls.append(kwargs) or None)
    monkeypatch.setattr(codex_cli.signal, "signal", fake_signal)

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 128 + codex_cli.signal.SIGHUP
    assert stop_calls == [
        {
            "session_id": "session-123",
            "reason": codex_cli._CODEX_STOP_REASON_TERMINAL_DISCONNECTED,
            "timeout_secs": codex_cli._CODEX_STOP_SIGNAL_TIMEOUT_SECONDS,
        }
    ]


def test_stop_native_codex_bridge_passes_reason_and_timeout(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(codex_cli, "get_engine_executable", lambda: "/tmp/longhouse-engine")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)

    error = codex_cli._stop_native_codex_bridge(
        session_id="session-123",
        reason=codex_cli._CODEX_STOP_REASON_TERMINAL_DISCONNECTED,
        timeout_secs=0.5,
    )

    assert error is None
    assert calls == [
        {
            "command": [
                "/tmp/longhouse-engine",
                "codex-bridge",
                "stop",
                "--session-id",
                "session-123",
                "--reason",
                "terminal_disconnected",
            ],
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": 0.5,
        }
    ]


def test_signal_cleanup_stops_bridge_once_with_terminal_disconnected(monkeypatch):
    stop_calls: list[dict[str, object]] = []
    signal_calls: list[tuple[object, object]] = []

    stopper = codex_cli._CodexBridgeStopper("session-123")
    monkeypatch.setattr(codex_cli, "_stop_native_codex_bridge", lambda **kwargs: stop_calls.append(kwargs) or None)
    monkeypatch.setattr(
        codex_cli.signal,
        "signal",
        lambda sig, handler: signal_calls.append((sig, handler)) or "old-handler",
    )

    previous = codex_cli._install_codex_signal_cleanup(stopper)
    handler = signal_calls[0][1]

    try:
        handler(codex_cli.signal.SIGHUP, None)
    except SystemExit as exc:
        assert exc.code == 128 + codex_cli.signal.SIGHUP
    else:
        raise AssertionError("expected SystemExit")

    assert stop_calls == [
        {
            "session_id": "session-123",
            "reason": codex_cli._CODEX_STOP_REASON_TERMINAL_DISCONNECTED,
            "timeout_secs": codex_cli._CODEX_STOP_SIGNAL_TIMEOUT_SECONDS,
        }
    ]

    try:
        handler(codex_cli.signal.SIGHUP, None)
    except SystemExit:
        pass
    assert len(stop_calls) == 1

    codex_cli._restore_signal_handlers(previous)
    restored = signal_calls[-len(previous) :]
    assert all(handler == "old-handler" for _sig, handler in restored)


def test_signal_cleanup_preserves_bridge_when_active_turn_survives(monkeypatch):
    stop_calls: list[dict[str, object]] = []
    signal_calls: list[tuple[object, object]] = []

    stopper = codex_cli._CodexBridgeStopper("session-123", state_file="/tmp/state.json")
    monkeypatch.setattr(codex_cli, "_active_turn_survived_tui_exit", lambda state_file: state_file == "/tmp/state.json")
    monkeypatch.setattr(codex_cli, "_stop_native_codex_bridge", lambda **kwargs: stop_calls.append(kwargs) or None)
    monkeypatch.setattr(
        codex_cli.signal,
        "signal",
        lambda sig, handler: signal_calls.append((sig, handler)) or "old-handler",
    )

    previous = codex_cli._install_codex_signal_cleanup(stopper)
    handler = signal_calls[0][1]

    try:
        handler(codex_cli.signal.SIGHUP, None)
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit")

    assert stop_calls == []

    codex_cli._restore_signal_handlers(previous)


def test_run_native_codex_tui_uses_foreground_process_group_when_interactive(monkeypatch, tmp_path):
    foreground_calls: list[dict[str, object]] = []

    monkeypatch.setattr(codex_cli, "_stdio_ttys", lambda: True)
    monkeypatch.setattr(
        codex_cli,
        "_run_foreground_process_group",
        lambda **kwargs: foreground_calls.append(kwargs) or 7,
    )

    exit_code = codex_cli._run_native_codex_tui(
        session_id="session-123",
        codex_bin="/tmp/codex",
        ws_url="ws://127.0.0.1:4800",
        cwd=tmp_path,
        bypass_approvals=True,
    )

    assert exit_code == 7
    assert foreground_calls[0]["cmd"] == [
        "/tmp/codex",
        "-c",
        "check_for_update_on_startup=false",
        "--dangerously-bypass-approvals-and-sandbox",
        "--enable",
        "tui_app_server",
        "--remote",
        "ws://127.0.0.1:4800",
    ]
    assert foreground_calls[0]["cwd"] == tmp_path
    assert foreground_calls[0]["env"]["LONGHOUSE_MANAGED_SESSION_ID"] == "session-123"


def test_run_native_codex_tui_attaches_to_prestarted_thread(monkeypatch, tmp_path):
    foreground_calls: list[dict[str, object]] = []

    monkeypatch.setattr(codex_cli, "_stdio_ttys", lambda: True)
    monkeypatch.setattr(
        codex_cli,
        "_run_foreground_process_group",
        lambda **kwargs: foreground_calls.append(kwargs) or 0,
    )

    exit_code = codex_cli._run_native_codex_tui(
        session_id="session-123",
        codex_bin="/tmp/codex",
        ws_url="ws://127.0.0.1:4800",
        cwd=tmp_path,
        thread_id="thr_123",
    )

    assert exit_code == 0
    assert foreground_calls[0]["cmd"] == [
        "/tmp/codex",
        "-c",
        "check_for_update_on_startup=false",
        "--enable",
        "tui_app_server",
        "--remote",
        "ws://127.0.0.1:4800",
    ]


def test_run_foreground_process_group_hands_terminal_to_child(monkeypatch, tmp_path):
    tcsetpgrp_calls: list[tuple[int, int]] = []
    signal_calls: list[tuple[int, object]] = []
    popen_calls: list[dict[str, object]] = []
    setpgrp_calls: list[bool] = []
    setpgid_calls: list[tuple[int, int]] = []

    class FakeStdin:
        def fileno(self):
            return 10

    class FakeChild:
        pid = 222

        def wait(self):
            return 0

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append({"cmd": cmd, **kwargs})
            kwargs["preexec_fn"]()
            self.pid = FakeChild.pid

        def wait(self):
            return FakeChild().wait()

    monkeypatch.setattr(codex_cli.sys, "stdin", FakeStdin())
    monkeypatch.setattr(codex_cli.os, "getpgrp", lambda: 111)
    monkeypatch.setattr(codex_cli.os, "setpgrp", lambda: setpgrp_calls.append(True))
    monkeypatch.setattr(codex_cli.os, "setpgid", lambda pid, pgrp: setpgid_calls.append((pid, pgrp)))
    monkeypatch.setattr(codex_cli.os, "tcsetpgrp", lambda fd, pgrp: tcsetpgrp_calls.append((fd, pgrp)))
    monkeypatch.setattr(
        codex_cli.signal,
        "signal",
        lambda sig, handler: signal_calls.append((sig, handler)) or "old-handler",
    )
    monkeypatch.setattr(codex_cli.subprocess, "Popen", FakePopen)

    exit_code = codex_cli._run_foreground_process_group(
        cmd=["/tmp/codex"],
        cwd=tmp_path,
        env={"LONGHOUSE_MANAGED_SESSION_ID": "session-123"},
    )

    assert exit_code == 0
    assert popen_calls[0]["cmd"] == ["/tmp/codex"]
    assert popen_calls[0]["cwd"] == str(tmp_path)
    assert popen_calls[0]["env"] == {"LONGHOUSE_MANAGED_SESSION_ID": "session-123"}
    assert setpgrp_calls == [True]
    assert setpgid_calls == [(222, 222)]
    assert tcsetpgrp_calls == [(10, 222), (10, 111)]
    assert signal_calls == [
        (codex_cli.signal.SIGTTOU, codex_cli.signal.SIG_IGN),
        (codex_cli.signal.SIGTTOU, "old-handler"),
    ]


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


def test_codex_command_exits_when_no_codex_executable_available(monkeypatch, tmp_path):
    runner = CliRunner()

    monkeypatch.setattr(
        codex_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(codex_cli, "_resolve_codex_binary", lambda _explicit=None: None)

    result = runner.invoke(app, ["codex", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "Codex executable not found." in result.output
    assert "codex` is on PATH" in result.output
    assert "LONGHOUSE_CODEX_BIN" in result.output
