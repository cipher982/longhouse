from __future__ import annotations

import io
import json
import os
import threading
import time

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.cli import opencode as opencode_cli
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli.main import app
from zerg.services import opencode_bridge_state as bridge_state


def _stub_managed_launch(monkeypatch):
    monkeypatch.setattr(
        opencode_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(opencode_cli, "_resolve_opencode_binary", lambda explicit=None: "/opt/homebrew/bin/opencode")
    monkeypatch.setattr(opencode_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_cli, "get_machine_name_label", lambda: "work-laptop")

    def fake_launch(**kwargs):
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="opencode_process",
        )

    monkeypatch.setattr(opencode_cli, "_launch_managed_local_from_api", fake_launch)


def test_opencode_command_launches_managed_session_and_passes_extra_args(monkeypatch, tmp_path):
    runner = CliRunner()
    run_calls: list[dict] = []
    _stub_managed_launch(monkeypatch)
    monkeypatch.setattr(opencode_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(opencode_cli, "_run_native_opencode", lambda **kwargs: run_calls.append(kwargs) or 0)

    result = runner.invoke(
        app,
        ["opencode", "--cwd", str(tmp_path), "--project", "demo", "--", "serve", "--port", "0"],
    )

    assert result.exit_code == 0, result.output
    assert run_calls == [
        {
            "session_id": "session-123",
            "machine_name": "work-laptop",
            "opencode_bin": "/opt/homebrew/bin/opencode",
            "cwd": tmp_path,
            "opencode_args": ("serve", "--port", "0"),
            "url": "https://longhouse.test",
            "token": "zdt_test_token",
            "config_dir": None,
        }
    ]


def test_opencode_command_defaults_to_serve_when_no_args(monkeypatch, tmp_path):
    runner = CliRunner()
    run_calls: list[dict] = []
    _stub_managed_launch(monkeypatch)
    monkeypatch.setattr(opencode_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(opencode_cli, "_run_native_opencode", lambda **kwargs: run_calls.append(kwargs) or 0)

    result = runner.invoke(app, ["opencode", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # No `--` means user passed nothing; launcher itself decides serve coercion.
    assert run_calls[0]["opencode_args"] == ()


def test_ensure_managed_serve_args_defaults_when_empty():
    assert opencode_cli._ensure_managed_serve_args(()) == ("serve", "--port", "0", "--hostname", "127.0.0.1")


def test_ensure_managed_serve_args_passes_through_serve():
    assert opencode_cli._ensure_managed_serve_args(("serve", "--port", "9000")) == ("serve", "--port", "9000")


def test_ensure_managed_serve_args_prepends_when_only_flags():
    assert opencode_cli._ensure_managed_serve_args(("--port", "9000")) == (
        "serve",
        "--port",
        "0",
        "--hostname",
        "127.0.0.1",
        "--port",
        "9000",
    )


def test_ensure_managed_serve_args_rejects_other_subcommand():
    with pytest.raises(opencode_cli._OpenCodeLaunchError, match="serve"):
        opencode_cli._ensure_managed_serve_args(("tui",))


def test_opencode_no_attach_prints_tokenless_launch_script_command(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_script = tmp_path / "session-123.launch.sh"
    config_content = tmp_path / "session-123.config-content.json"
    config_content.write_text('{"plugin":[]}\n', encoding="utf-8")

    _stub_managed_launch(monkeypatch)
    monkeypatch.setattr(opencode_cli, "_write_opencode_runtime_config_content", lambda **_kwargs: config_content)
    monkeypatch.setattr(opencode_cli, "_write_opencode_launch_script", lambda **_kwargs: launch_script)
    monkeypatch.setattr(opencode_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(opencode_cli, "_run_native_opencode", lambda **_kwargs: pytest.fail("should not attach"))

    result = runner.invoke(app, ["opencode", "--no-attach", "--cwd", str(tmp_path), "--", "serve"])

    assert result.exit_code == 0, result.output
    assert str(launch_script) in result.output
    assert "zdt_test_token" not in result.output


def test_opencode_launch_api_wrapper_sets_provider(monkeypatch, tmp_path):
    calls: list[dict] = []

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="opencode_process",
        )

    monkeypatch.setattr(opencode_cli.managed_local_cli, "_launch_managed_local_from_api", fake_launch)

    opencode_cli._launch_managed_local_from_api(
        url="https://longhouse.test",
        token="zdt_test_token",
        cwd=tmp_path,
        project="demo",
        loop_mode=opencode_cli.SessionLoopMode.ASSIST,
        name=None,
        machine_name="work-laptop",
    )

    assert calls[0]["provider"] == "opencode"


class _FakeProcess:
    def __init__(self, *, listen_url: str, exit_code: int = 0) -> None:
        self._lines = [
            "[opencode] starting...\n",
            f"opencode server listening on {listen_url}\n",
            "[opencode] ready\n",
        ]
        self.stdout = io.StringIO("".join(self._lines))
        self.pid = 12345
        self._exit_code = exit_code
        self._reader_done = threading.Event()
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        # Real Popen.wait() returns only after the child exits, by which time
        # stdout has reached EOF. Mimic that ordering: block until the reader
        # thread has fully drained our StringIO and invoked on_url.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.stdout.tell() == len(self.stdout.getvalue()):
                break
            time.sleep(0.005)
        time.sleep(0.05)
        self._reader_done.set()
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True


def test_run_native_opencode_writes_bridge_state_and_terminal_event(monkeypatch, tmp_path):
    runtime_events: list[dict] = []
    plugin_path = tmp_path / "longhouse-opencode-runtime.mjs"
    plugin_path.write_text("export default {}\n", encoding="utf-8")

    captured_env: dict[str, str] = {}

    def fake_popen(cmd, *, cwd, env, stdout, stderr, text, bufsize):
        captured_env.update(env)
        captured_env["__cmd__"] = json.dumps(cmd)
        captured_env["__cwd__"] = str(cwd)
        return _FakeProcess(listen_url="http://127.0.0.1:54321")

    monkeypatch.setattr(opencode_cli, "_ensure_opencode_runtime_plugin", lambda config_dir=None: plugin_path)
    monkeypatch.setattr(opencode_cli, "_post_opencode_runtime_event", lambda **kwargs: runtime_events.append(kwargs))
    monkeypatch.setattr(opencode_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge_state, "generate_server_password", lambda: "test-password-redacted")
    monkeypatch.setattr(opencode_cli, "generate_server_password", lambda: "test-password-redacted")

    config_dir = tmp_path / "config"
    exit_code = opencode_cli._run_native_opencode(
        session_id="session-123",
        machine_name="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        opencode_args=(),  # default to serve
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=config_dir,
    )

    assert exit_code == 0
    assert json.loads(captured_env["__cmd__"]) == [
        "/opt/homebrew/bin/opencode",
        "serve",
        "--port",
        "0",
        "--hostname",
        "127.0.0.1",
    ]
    assert captured_env["LONGHOUSE_MANAGED_SESSION_ID"] == "session-123"
    assert captured_env["LONGHOUSE_DEVICE_ID"] == "work-laptop"
    assert captured_env["OPENCODE_SERVER_PASSWORD"] == "test-password-redacted"
    config = json.loads(captured_env["OPENCODE_CONFIG_CONTENT"])
    plugin_spec = config["plugin"][-1]
    assert plugin_spec[0] == plugin_path.resolve().as_uri()

    assert runtime_events[0]["event"]["kind"] == "terminal_signal"
    assert runtime_events[0]["event"]["payload"] == {"terminal_state": "session_ended", "exit_code": 0}

    # Bridge state should have been removed in the finally block.
    state_path = bridge_state.build_opencode_bridge_state_file(
        session_id="session-123", config_dir=config_dir
    )
    assert not state_path.exists(), "bridge state should be cleaned up after exit"


def test_run_native_opencode_writes_state_with_password_and_url(monkeypatch, tmp_path):
    """Verify the state file is written with the captured URL while the process is alive."""

    plugin_path = tmp_path / "longhouse-opencode-runtime.mjs"
    plugin_path.write_text("export default {}\n", encoding="utf-8")

    state_writes: list[dict] = []
    real_write = bridge_state.write_opencode_bridge_state

    def spy_write(**kwargs):
        state_writes.append(kwargs)
        return real_write(**kwargs)

    def fake_popen(cmd, *, cwd, env, stdout, stderr, text, bufsize):
        return _FakeProcess(listen_url="http://127.0.0.1:54321")

    monkeypatch.setattr(opencode_cli, "_ensure_opencode_runtime_plugin", lambda config_dir=None: plugin_path)
    monkeypatch.setattr(opencode_cli, "_post_opencode_runtime_event", lambda **kwargs: None)
    monkeypatch.setattr(opencode_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode_cli, "generate_server_password", lambda: "test-password-redacted")
    monkeypatch.setattr(opencode_cli, "write_opencode_bridge_state", spy_write)

    opencode_cli._run_native_opencode(
        session_id="session-456",
        machine_name="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        opencode_args=(),
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / "config",
    )

    assert len(state_writes) == 1
    written = state_writes[0]
    assert written["session_id"] == "session-456"
    assert written["server_url"] == "http://127.0.0.1:54321"
    assert written["server_password"] == "test-password-redacted"
    assert written["cwd"] == str(tmp_path)


def test_opencode_config_content_preserves_existing_plugins(tmp_path):
    existing_plugin = ["file:///existing-plugin.mjs", {"enabled": True}]
    content = opencode_cli._opencode_config_content_with_longhouse_plugin(
        existing_content=json.dumps({"plugin": [existing_plugin], "theme": "system"}),
        plugin_path=tmp_path / "longhouse-opencode-runtime.mjs",
        runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
        token="zdt_test_token",
        session_id="session-123",
        device_id="work-laptop",
    )

    config = json.loads(content)
    assert config["theme"] == "system"
    assert config["plugin"][0] == existing_plugin
    assert config["plugin"][1][0] == (tmp_path / "longhouse-opencode-runtime.mjs").resolve().as_uri()
    assert config["plugin"][1][1]["longhouseSessionID"] == "session-123"


def test_opencode_config_content_rejects_invalid_shapes(tmp_path):
    with pytest.raises(opencode_cli._OpenCodeLaunchError, match="JSON object"):
        opencode_cli._opencode_config_content_with_longhouse_plugin(
            existing_content="[]",
            plugin_path=tmp_path / "longhouse-opencode-runtime.mjs",
            runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
            token="zdt_test_token",
            session_id="session-123",
            device_id="work-laptop",
        )

    with pytest.raises(opencode_cli._OpenCodeLaunchError, match="plugin field must be an array"):
        opencode_cli._opencode_config_content_with_longhouse_plugin(
            existing_content=json.dumps({"plugin": {"bad": True}}),
            plugin_path=tmp_path / "longhouse-opencode-runtime.mjs",
            runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
            token="zdt_test_token",
            session_id="session-123",
            device_id="work-laptop",
        )


def test_write_opencode_runtime_config_content_is_private(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)

    path = opencode_cli._write_opencode_runtime_config_content(
        config_dir=tmp_path / "config",
        runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
        token="zdt_test_token",
        session_id="session-123",
        device_id="work-laptop",
    )

    assert path.name == "session-123.config-content.json"
    assert path.stat().st_mode & 0o777 == 0o600
    config = json.loads(path.read_text(encoding="utf-8"))
    plugin_path = tmp_path / "config" / "managed-local" / "opencode" / "longhouse-opencode-runtime.mjs"
    assert plugin_path.exists()
    assert config["plugin"][0][0] == plugin_path.resolve().as_uri()


def test_build_opencode_command_uses_config_content_file_without_echoing_token(tmp_path):
    config_path = tmp_path / "session.config-content.json"
    config_path.write_text('{"plugin":[]}\n', encoding="utf-8")

    command = opencode_cli._build_opencode_command(
        session_id="session-123",
        machine_name="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        opencode_args=("serve",),
        config_content_path=config_path,
    )

    assert 'OPENCODE_CONFIG_CONTENT="$(cat ' in command
    assert str(config_path) in command
    assert "zdt_test_token" not in command


def test_launch_script_closes_session_without_printing_token(tmp_path):
    config_path = tmp_path / "session.config-content.json"
    config_path.write_text('{"plugin":[]}\n', encoding="utf-8")
    launch_script = opencode_cli._write_opencode_launch_script(
        config_dir=tmp_path / "config",
        session_id="session-123",
        device_id="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
        token="zdt_test_token",
        config_content_path=config_path,
    )

    command = opencode_cli._build_opencode_command(
        session_id="session-123",
        machine_name="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        opencode_args=("serve",),
        launch_script_path=launch_script,
    )

    assert oct(launch_script.stat().st_mode & 0o777) == "0o700"
    assert "terminal_signal" in launch_script.read_text(encoding="utf-8")
    assert str(launch_script) in command
    assert "zdt_test_token" not in command
