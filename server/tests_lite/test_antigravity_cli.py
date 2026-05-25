from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from zerg.cli import antigravity as antigravity_cli
from zerg.cli._common import ManagedLocalLaunchResponse
from zerg.cli.main import app


def test_antigravity_command_launches_managed_session_and_passes_extra_args(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []
    run_calls: list[dict] = []

    monkeypatch.setattr(
        antigravity_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        antigravity_cli,
        "_resolve_antigravity_binary",
        lambda explicit=None: "/Users/test/.local/bin/agy",
    )
    monkeypatch.setattr(antigravity_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(antigravity_cli, "get_machine_name_label", lambda: "work-laptop")

    def fake_launch(**kwargs):
        launch_calls.append(kwargs)
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="antigravity_process",
        )

    monkeypatch.setattr(antigravity_cli, "_launch_managed_local_from_api", fake_launch)
    monkeypatch.setattr(antigravity_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(antigravity_cli, "_run_native_antigravity", lambda **kwargs: run_calls.append(kwargs) or 0)

    result = runner.invoke(
        app,
        [
            "antigravity",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
            "--",
            "--sandbox",
            "read-only",
        ],
    )

    assert result.exit_code == 0, result.output
    assert launch_calls[0]["cwd"] == tmp_path
    assert launch_calls[0]["machine_name"] == "work-laptop"
    assert run_calls == [
        {
            "session_id": "session-123",
            "machine_name": "work-laptop",
            "antigravity_bin": "/Users/test/.local/bin/agy",
            "cwd": tmp_path,
            "antigravity_args": ("--sandbox", "read-only"),
            "url": "https://longhouse.test",
            "token": "zdt_test_token",
            "config_dir": None,
        }
    ]


def test_agy_command_alias_launches_managed_session(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []
    run_calls: list[dict] = []

    monkeypatch.setattr(
        antigravity_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        antigravity_cli,
        "_resolve_antigravity_binary",
        lambda explicit=None: "/Users/test/.local/bin/agy",
    )
    monkeypatch.setattr(antigravity_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(antigravity_cli, "get_machine_name_label", lambda: "work-laptop")

    def fake_launch(**kwargs):
        launch_calls.append(kwargs)
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="antigravity_process",
        )

    monkeypatch.setattr(antigravity_cli, "_launch_managed_local_from_api", fake_launch)
    monkeypatch.setattr(antigravity_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(antigravity_cli, "_run_native_antigravity", lambda **kwargs: run_calls.append(kwargs) or 0)

    result = runner.invoke(app, ["agy", "--cwd", str(tmp_path), "--", "--version"])

    assert result.exit_code == 0, result.output
    assert launch_calls[0]["cwd"] == tmp_path
    assert run_calls[0]["antigravity_bin"] == "/Users/test/.local/bin/agy"
    assert run_calls[0]["antigravity_args"] == ("--version",)


def test_antigravity_no_attach_prints_tokenless_launch_script_command(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_script = tmp_path / "session-123.launch.sh"
    launch_script_calls: list[dict] = []

    monkeypatch.setattr(
        antigravity_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        antigravity_cli,
        "_resolve_antigravity_binary",
        lambda explicit=None: "/Users/test/.local/bin/agy",
    )
    monkeypatch.setattr(antigravity_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(antigravity_cli, "get_machine_name_label", lambda: "work-laptop")

    def fake_launch_script(**kwargs):
        launch_script_calls.append(kwargs)
        return launch_script

    monkeypatch.setattr(antigravity_cli, "_write_antigravity_launch_script", fake_launch_script)
    monkeypatch.setattr(
        antigravity_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="antigravity_process",
        ),
    )
    monkeypatch.setattr(antigravity_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(antigravity_cli, "_run_native_antigravity", lambda **_kwargs: pytest.fail("should not attach"))

    result = runner.invoke(
        app,
        [
            "antigravity",
            "--no-attach",
            "--cwd",
            str(tmp_path),
            "--",
            "--model",
            "gemini-3.5-pro",
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(launch_script) in result.output
    assert "zdt_test_token" not in result.output
    assert launch_script_calls[0]["runtime_events_url"] == "https://longhouse.test/api/agents/runtime/events/batch"
    assert launch_script_calls[0]["token"] == "zdt_test_token"


def test_antigravity_launch_api_wrapper_sets_provider(monkeypatch, tmp_path):
    calls: list[dict] = []

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="",
            source_runner_name="work-laptop",
            managed_transport="antigravity_process",
        )

    monkeypatch.setattr(antigravity_cli.managed_local_cli, "_launch_managed_local_from_api", fake_launch)

    antigravity_cli._launch_managed_local_from_api(
        url="https://longhouse.test",
        token="zdt_test_token",
        cwd=tmp_path,
        project="demo",
        loop_mode=antigravity_cli.SessionLoopMode.ASSIST,
        name=None,
        machine_name="work-laptop",
    )

    assert calls[0]["provider"] == "antigravity"


def test_run_native_antigravity_exports_managed_session_env(monkeypatch, tmp_path):
    calls: list[dict] = []
    runtime_events: list[dict] = []

    def fake_run(cmd, *, check, cwd, env):
        calls.append({"cmd": cmd, "check": check, "cwd": cwd, "env": env})

        class Completed:
            returncode = 0

        return Completed()

    monkeypatch.setattr(antigravity_cli, "_ensure_antigravity_runtime_plugin", lambda **_kwargs: tmp_path / "plugin")
    monkeypatch.setattr(
        antigravity_cli,
        "_post_antigravity_runtime_event",
        lambda **kwargs: runtime_events.append(kwargs),
    )
    monkeypatch.setattr(antigravity_cli.subprocess, "run", fake_run)

    exit_code = antigravity_cli._run_native_antigravity(
        session_id="session-123",
        machine_name="work-laptop",
        antigravity_bin="/Users/test/.local/bin/agy",
        cwd=tmp_path,
        antigravity_args=("--sandbox", "read-only"),
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / "config",
    )

    assert exit_code == 0
    assert calls[0]["cmd"] == ["/Users/test/.local/bin/agy", "--sandbox", "read-only"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["env"]["LONGHOUSE_MANAGED_SESSION_ID"] == "session-123"
    assert calls[0]["env"]["LONGHOUSE_DEVICE_ID"] == "work-laptop"
    assert calls[0]["env"]["LONGHOUSE_RUNTIME_EVENTS_URL"] == "https://longhouse.test/api/agents/runtime/events/batch"
    assert calls[0]["env"]["LONGHOUSE_RUNTIME_TOKEN"] == "zdt_test_token"
    assert runtime_events[0]["url"] == "https://longhouse.test"
    assert runtime_events[0]["token"] == "zdt_test_token"
    assert runtime_events[0]["event"]["kind"] == "terminal_signal"
    assert runtime_events[0]["event"]["phase"] == "finished"
    assert runtime_events[0]["event"]["source"] == "antigravity_event"
    assert runtime_events[0]["event"]["payload"] == {"terminal_state": "session_ended", "exit_code": 0}


def test_antigravity_runtime_plugin_writes_hooks_and_script(tmp_path):
    plugin_root = antigravity_cli._ensure_antigravity_runtime_plugin(
        config_dir=tmp_path / ".claude",
        antigravity_cli_root=tmp_path / ".gemini" / "antigravity-cli",
        engine_path="/usr/local/bin/longhouse-engine",
        global_hooks_path=tmp_path / ".gemini" / "config" / "hooks.json",
    )

    assert plugin_root.name == "longhouse-runtime"
    assert plugin_root.parent == tmp_path / ".gemini" / "antigravity-cli" / "plugins"
    assert json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8")) == {"name": "longhouse-runtime"}
    hooks = json.loads((plugin_root / "hooks.json").read_text(encoding="utf-8"))
    config = hooks["longhouse-runtime"]
    assert set(config) == {"PreInvocation", "PreToolUse", "PostToolUse", "PostInvocation", "Stop"}
    global_hooks = json.loads((tmp_path / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))
    assert global_hooks["longhouse-runtime"] == config
    script = plugin_root / "longhouse-antigravity-hook.sh"
    assert oct(script.stat().st_mode & 0o777) == "0o755"
    script_text = script.read_text(encoding="utf-8")
    assert '"--provider"' in script_text
    assert '"antigravity"' in script_text
    assert "zdt_test_token" not in script_text


def test_antigravity_hook_script_writes_outbox_without_jq(tmp_path):
    plugin_root = antigravity_cli._ensure_antigravity_runtime_plugin(
        config_dir=tmp_path / ".claude",
        antigravity_cli_root=tmp_path / ".gemini" / "antigravity-cli",
        engine_path="/bin/true",
        global_hooks_path=tmp_path / ".gemini" / "config" / "hooks.json",
    )
    script = plugin_root / "longhouse-antigravity-hook.sh"

    result = subprocess.run(
        [str(script), "PreToolUse"],
        input=json.dumps(
            {
                "conversationId": "ag-provider-session",
                "toolCall": {"name": "shell"},
                "workspacePaths": [str(tmp_path)],
                "transcriptPath": str(tmp_path / "transcript.jsonl"),
                "stepIdx": 7,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={
            "LONGHOUSE_HOOK_PYTHON": sys.executable,
            "LONGHOUSE_ENGINE": "/bin/true",
            "LONGHOUSE_MANAGED_SESSION_ID": "session-123",
            "PATH": "/nonexistent",
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "allow", "reason": ""}
    outbox_files = list((tmp_path / ".longhouse" / "agent" / "outbox").glob("prs.*.json"))
    assert len(outbox_files) == 1
    payload = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert payload["session_id"] == "session-123"
    assert payload["state"] == "running"
    assert payload["tool_name"] == "shell"
    assert payload["provider"] == "antigravity"
    assert payload["step_index"] == "7"


def test_antigravity_hook_binds_transcript_in_same_longhouse_home(tmp_path):
    record_path = tmp_path / "bind-args.json"
    fake_engine = tmp_path / "fake-engine.py"
    fake_engine.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, sys",
                "payload = {'argv': sys.argv[1:], 'home': os.environ.get('LONGHOUSE_HOME')}",
                f"open({str(record_path)!r}, 'w').write(json.dumps(payload))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_engine.chmod(0o755)
    plugin_root = antigravity_cli._ensure_antigravity_runtime_plugin(
        config_dir=tmp_path / ".claude",
        antigravity_cli_root=tmp_path / ".gemini" / "antigravity-cli",
        engine_path=str(fake_engine),
        global_hooks_path=tmp_path / ".gemini" / "config" / "hooks.json",
    )
    script = plugin_root / "longhouse-antigravity-hook.sh"
    transcript_path = tmp_path / "transcript.jsonl"

    result = subprocess.run(
        [str(script), "PreInvocation"],
        input=json.dumps(
            {
                "conversationId": "ag-provider-session",
                "workspacePaths": [str(tmp_path)],
                "transcriptPath": str(transcript_path),
                "stepIdx": 3,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={
            "LONGHOUSE_HOOK_PYTHON": sys.executable,
            "LONGHOUSE_MANAGED_SESSION_ID": "session-123",
            "PATH": os.defpath,
        },
    )

    assert result.returncode == 0, result.stderr
    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    longhouse_home = tmp_path / ".longhouse"
    assert recorded["home"] == str(longhouse_home)
    assert recorded["argv"] == [
        "bind",
        "--path",
        str(transcript_path),
        "--session-id",
        "session-123",
        "--provider",
        "antigravity",
        "--db",
        str(longhouse_home / "agent" / "longhouse-shipper.db"),
    ]


def test_antigravity_runtime_plugin_installs_with_agy(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, *, check, stdout, stderr, text, timeout):
        calls.append(list(cmd))

        class Completed:
            returncode = 0
            stderr = ""

        return Completed()

    monkeypatch.setattr(antigravity_cli.subprocess, "run", fake_run)

    plugin_root = antigravity_cli._ensure_antigravity_runtime_plugin(
        config_dir=tmp_path / ".claude",
        antigravity_cli_root=tmp_path / ".gemini" / "antigravity-cli",
        engine_path="/bin/true",
        antigravity_bin="/usr/local/bin/agy",
        global_hooks_path=tmp_path / ".gemini" / "config" / "hooks.json",
    )

    source_root = tmp_path / ".claude" / "managed-local" / "antigravity" / "plugins" / "longhouse-runtime"
    assert plugin_root == tmp_path / ".gemini" / "antigravity-cli" / "plugins" / "longhouse-runtime"
    assert calls == [["/usr/local/bin/agy", "plugin", "install", str(source_root)]]
    assert (source_root / "plugin.json").exists()
    assert (source_root / "hooks.json").exists()
    assert (source_root / "longhouse-antigravity-hook.sh").exists()


def test_launch_script_closes_session_without_printing_token(monkeypatch, tmp_path):
    monkeypatch.setattr(antigravity_cli, "_ensure_antigravity_runtime_plugin", lambda **_kwargs: tmp_path / "plugin")

    launch_script = antigravity_cli._write_antigravity_launch_script(
        config_dir=tmp_path / "config",
        session_id="session-123",
        device_id="work-laptop",
        antigravity_bin="/Users/test/.local/bin/agy",
        cwd=tmp_path,
        runtime_events_url="https://longhouse.test/api/agents/runtime/events/batch",
        token="zdt_test_token",
    )

    command = antigravity_cli._build_antigravity_command(
        session_id="session-123",
        machine_name="work-laptop",
        antigravity_bin="/Users/test/.local/bin/agy",
        cwd=tmp_path,
        antigravity_args=("--sandbox", "read-only"),
        launch_script_path=launch_script,
    )

    assert oct(launch_script.stat().st_mode & 0o777) == "0o700"
    assert "terminal_signal" in launch_script.read_text(encoding="utf-8")
    assert str(launch_script) in command
    assert "zdt_test_token" not in command


def test_run_native_antigravity_marks_launch_failure_terminal_state(monkeypatch, tmp_path):
    runtime_events: list[dict] = []

    def raise_file_not_found(cmd, *, check, cwd, env):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(antigravity_cli, "_ensure_antigravity_runtime_plugin", lambda **_kwargs: tmp_path / "plugin")
    monkeypatch.setattr(
        antigravity_cli,
        "_post_antigravity_runtime_event",
        lambda **kwargs: runtime_events.append(kwargs),
    )
    monkeypatch.setattr(antigravity_cli.subprocess, "run", raise_file_not_found)

    with pytest.raises(FileNotFoundError):
        antigravity_cli._run_native_antigravity(
            session_id="session-123",
            machine_name="work-laptop",
            antigravity_bin="/does/not/exist/agy",
            cwd=tmp_path,
            antigravity_args=(),
            url="https://longhouse.test",
            token="zdt_test_token",
            config_dir=tmp_path / "config",
        )

    assert len(runtime_events) == 1
    event = runtime_events[0]["event"]
    assert event["kind"] == "terminal_signal"
    assert event["phase"] == "finished"
    assert event["payload"]["terminal_state"] == "launch_failed"
    assert event["payload"]["exit_code"] == 1
    assert "launch_failed" in event["dedupe_key"]


def test_run_native_antigravity_records_session_ended_when_subprocess_returns_nonzero(monkeypatch, tmp_path):
    runtime_events: list[dict] = []

    class Completed:
        returncode = 17

    monkeypatch.setattr(antigravity_cli, "_ensure_antigravity_runtime_plugin", lambda **_kwargs: tmp_path / "plugin")
    monkeypatch.setattr(
        antigravity_cli,
        "_post_antigravity_runtime_event",
        lambda **kwargs: runtime_events.append(kwargs),
    )
    monkeypatch.setattr(antigravity_cli.subprocess, "run", lambda cmd, *, check, cwd, env: Completed())

    exit_code = antigravity_cli._run_native_antigravity(
        session_id="session-123",
        machine_name="work-laptop",
        antigravity_bin="/Users/test/.local/bin/agy",
        cwd=tmp_path,
        antigravity_args=(),
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / "config",
    )

    assert exit_code == 17
    event = runtime_events[0]["event"]
    assert event["payload"] == {"terminal_state": "session_ended", "exit_code": 17}
    assert "session_ended" in event["dedupe_key"]


def test_run_native_antigravity_swallows_runtime_event_failure(monkeypatch, tmp_path):
    class Completed:
        returncode = 0

    def raising_post(**_kwargs):
        raise antigravity_cli._AntigravityLaunchError("network down")

    monkeypatch.setattr(antigravity_cli, "_ensure_antigravity_runtime_plugin", lambda **_kwargs: tmp_path / "plugin")
    monkeypatch.setattr(antigravity_cli, "_post_antigravity_runtime_event", raising_post)
    monkeypatch.setattr(antigravity_cli.subprocess, "run", lambda cmd, *, check, cwd, env: Completed())

    exit_code = antigravity_cli._run_native_antigravity(
        session_id="session-123",
        machine_name="work-laptop",
        antigravity_bin="/Users/test/.local/bin/agy",
        cwd=tmp_path,
        antigravity_args=(),
        url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / "config",
    )

    assert exit_code == 0


def test_antigravity_hook_script_quotes_paths_with_special_chars(tmp_path):
    weird_engine = tmp_path / "engines" / "longhouse engine $with' quirks"
    weird_engine.parent.mkdir(parents=True, exist_ok=True)
    weird_engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    weird_engine.chmod(0o755)
    weird_config = tmp_path / "weird config $dir"
    weird_cli_root = tmp_path / "weird gemini" / "antigravity-cli"

    plugin_root = antigravity_cli._ensure_antigravity_runtime_plugin(
        config_dir=weird_config,
        antigravity_cli_root=weird_cli_root,
        engine_path=str(weird_engine),
        global_hooks_path=tmp_path / "gemini config" / "hooks.json",
    )
    script = plugin_root / "longhouse-antigravity-hook.sh"

    syntax_check = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax_check.returncode == 0, syntax_check.stderr

    result = subprocess.run(
        [str(script), "PreToolUse"],
        input=json.dumps(
            {
                "conversationId": "ag-1",
                "toolCall": {"name": "shell"},
                "workspacePaths": [str(tmp_path)],
                "stepIdx": 1,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env={
            "LONGHOUSE_HOOK_PYTHON": sys.executable,
            "LONGHOUSE_MANAGED_SESSION_ID": "session-weird",
            "PATH": os.defpath,
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "allow", "reason": ""}
