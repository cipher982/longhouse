from __future__ import annotations

import json
import os

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


def test_opencode_command_launches_managed_session_and_passes_extra_args(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []
    run_calls: list[dict] = []

    monkeypatch.setattr(
        opencode_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(opencode_cli, "_resolve_opencode_binary", lambda explicit=None: "/opt/homebrew/bin/opencode")
    monkeypatch.setattr(opencode_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_cli, "get_machine_name_label", lambda: "work-laptop")

    def fake_launch(**kwargs):
        launch_calls.append(kwargs)
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="opencode_process",
        )

    monkeypatch.setattr(opencode_cli, "_launch_managed_local_from_api", fake_launch)
    monkeypatch.setattr(opencode_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(opencode_cli, "_run_native_opencode", lambda **kwargs: run_calls.append(kwargs) or 0)

    result = runner.invoke(
        app,
        [
            "opencode",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
            "--",
            "serve",
            "--port",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert launch_calls[0]["cwd"] == tmp_path
    assert launch_calls[0]["machine_name"] == "work-laptop"
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


def test_opencode_no_attach_prints_tokenless_launch_script_command(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_script = tmp_path / "session-123.launch.sh"
    config_content = tmp_path / "session-123.config-content.json"
    config_content.write_text('{"plugin":[]}\n', encoding="utf-8")
    launch_script_calls: list[dict] = []

    monkeypatch.setattr(
        opencode_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(opencode_cli, "_resolve_opencode_binary", lambda explicit=None: "/opt/homebrew/bin/opencode")
    monkeypatch.setattr(opencode_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(opencode_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(opencode_cli, "_write_opencode_runtime_config_content", lambda **_kwargs: config_content)

    def fake_launch_script(**kwargs):
        launch_script_calls.append(kwargs)
        return launch_script

    monkeypatch.setattr(opencode_cli, "_write_opencode_launch_script", fake_launch_script)
    monkeypatch.setattr(
        opencode_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="opencode_process",
        ),
    )
    monkeypatch.setattr(opencode_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(opencode_cli, "_run_native_opencode", lambda **_kwargs: pytest.fail("should not attach"))

    result = runner.invoke(
        app,
        [
            "opencode",
            "--no-attach",
            "--cwd",
            str(tmp_path),
            "--",
            "serve",
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(launch_script) in result.output
    assert "zdt_test_token" not in result.output
    assert launch_script_calls[0]["runtime_events_url"] == "https://longhouse.test/api/agents/runtime/events/batch"
    assert launch_script_calls[0]["token"] == "zdt_test_token"
    assert launch_script_calls[0]["config_content_path"] == config_content


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


def test_run_native_opencode_exports_managed_session_env(monkeypatch, tmp_path):
    calls: list[dict] = []
    runtime_events: list[dict] = []
    plugin_path = tmp_path / "longhouse-opencode-runtime.mjs"
    plugin_path.write_text("export default {}\n", encoding="utf-8")

    def fake_run(cmd, *, check, cwd, env):
        calls.append({"cmd": cmd, "check": check, "cwd": cwd, "env": env})

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr(opencode_cli, "_ensure_opencode_runtime_plugin", lambda config_dir=None: plugin_path)
    monkeypatch.setattr(
        opencode_cli,
        "_post_opencode_runtime_event",
        lambda **kwargs: runtime_events.append(kwargs),
    )
    monkeypatch.setattr(opencode_cli.subprocess, "run", fake_run)

    exit_code = opencode_cli._run_native_opencode(
        session_id="session-123",
        machine_name="work-laptop",
        opencode_bin="/opt/homebrew/bin/opencode",
        cwd=tmp_path,
        opencode_args=("serve",),
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / "config",
    )

    assert exit_code == 0
    assert calls[0]["cmd"] == ["/opt/homebrew/bin/opencode", "serve"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["env"]["LONGHOUSE_MANAGED_SESSION_ID"] == "session-123"
    assert calls[0]["env"]["LONGHOUSE_DEVICE_ID"] == "work-laptop"
    config = json.loads(calls[0]["env"]["OPENCODE_CONFIG_CONTENT"])
    plugin_spec = config["plugin"][-1]
    assert plugin_spec[0] == plugin_path.resolve().as_uri()
    assert plugin_spec[1] == {
        "runtimeEventsUrl": "https://longhouse.test/api/agents/runtime/events/batch",
        "token": "zdt_test_token",
        "longhouseSessionID": "session-123",
        "deviceID": "work-laptop",
    }
    assert runtime_events[0]["url"] == "https://longhouse.test"
    assert runtime_events[0]["token"] == "zdt_test_token"
    assert runtime_events[0]["event"]["kind"] == "terminal_signal"
    assert runtime_events[0]["event"]["phase"] == "finished"
    assert runtime_events[0]["event"]["source"] == "opencode_event"
    assert runtime_events[0]["event"]["payload"] == {"terminal_state": "session_ended", "exit_code": 0}


def test_run_native_opencode_reports_terminal_on_interruption(monkeypatch, tmp_path):
    runtime_events: list[dict] = []
    plugin_path = tmp_path / "longhouse-opencode-runtime.mjs"
    plugin_path.write_text("export default {}\n", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(opencode_cli, "_ensure_opencode_runtime_plugin", lambda config_dir=None: plugin_path)
    monkeypatch.setattr(
        opencode_cli,
        "_post_opencode_runtime_event",
        lambda **kwargs: runtime_events.append(kwargs),
    )
    monkeypatch.setattr(opencode_cli.subprocess, "run", fake_run)

    with pytest.raises(KeyboardInterrupt):
        opencode_cli._run_native_opencode(
            session_id="session-123",
            machine_name="work-laptop",
            opencode_bin="/opt/homebrew/bin/opencode",
            cwd=tmp_path,
            opencode_args=("serve",),
            url="https://longhouse.test",
            token="zdt_test_token",
        )

    assert runtime_events[0]["event"]["kind"] == "terminal_signal"
    assert runtime_events[0]["event"]["payload"] == {"terminal_state": "session_ended", "exit_code": 1}


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

    assert "OPENCODE_CONFIG_CONTENT=\"$(cat " in command
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
