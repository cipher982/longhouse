from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from click.exceptions import Exit as ClickExit
from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.cli import claude as claude_cli
from zerg.cli.main import app
from zerg.services.claude_channel_bridge import build_claude_channel_state_file
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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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


def test_load_api_credentials_accepts_custom_exit_code(tmp_path):
    with pytest.raises(ClickExit) as exc_info:
        claude_cli._load_api_credentials(url=None, token=None, config_dir=tmp_path, exit_code=78)

    assert exc_info.value.exit_code == 78


def test_launch_managed_local_from_api_uses_this_device_endpoint(monkeypatch, tmp_path):
    fake_client = _FakeClient(
        response=_FakeResponse(
            status_code=200,
            json_data={
                "session_id": "session-123",
                "provider_session_id": "provider-123",
                "attach_command": "zsh -lc 'exec claude --resume provider-123'",
                "source_runner_name": "work-laptop",
                "managed_transport": "claude_channel_bridge",
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
        native_claude_channels_available=False,
        claude_launch_env={
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
            "AWS_REGION": "us-east-1",
            "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
        },
    )

    assert result.session_id == "session-123"
    assert result.provider_session_id == "provider-123"
    assert result.attach_command == "zsh -lc 'exec claude --resume provider-123'"
    assert result.managed_transport == "claude_channel_bridge"
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
                "native_claude_channels_available": False,
                "claude_launch_env": {
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                    "AWS_PROFILE": "zh-qa-engineer",
                    "AWS_REGION": "us-east-1",
                    "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
                },
            },
        }
    ]


def test_claude_command_fails_when_native_channels_unavailable(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

    monkeypatch.delenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, raising=False)
    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (False, "authMethod=third_party, apiProvider=bedrock"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_collect_claude_launch_env",
        lambda: {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
            "AWS_REGION": "us-east-1",
            "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
        },
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs),
    )

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
            "--open",
        ],
    )

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert "Native Claude channels unavailable (disabled by Claude launch env)." in result.output
    assert "Longhouse now requires the local Claude channel bridge." in result.output
    assert launch_calls == []


def test_claude_command_stops_before_api_launch_when_preflight_fails(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (True, "authMethod=claude.ai, apiProvider=firstParty"),
    )
    monkeypatch.setattr(claude_cli, "_collect_claude_launch_env", lambda: {})
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_ensure_managed_launch_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(ClickExit(claude_cli.EXIT_SETUP_FAILED)),
    )
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs),
    )

    result = runner.invoke(
        app,
        [
            "claude",
            "--cwd",
            str(tmp_path),
            "--project",
            "demo",
        ],
    )

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert launch_calls == []


def test_claude_command_stops_before_api_launch_when_native_bridge_setup_fails(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (True, "authMethod=claude.ai, apiProvider=firstParty"),
    )
    monkeypatch.setattr(claude_cli, "_collect_claude_launch_env", lambda: {})
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_cli,
        "_ensure_native_claude_prereqs",
        lambda **_kwargs: (_ for _ in ()).throw(claude_cli._NativeClaudeError("missing longhouse-channel")),
    )
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs),
    )

    result = runner.invoke(app, ["claude", "--cwd", str(tmp_path)])

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert "Claude bridge setup failed: missing longhouse-channel" in result.output
    assert launch_calls == []


def test_claude_command_starts_native_channel_bridge_when_api_returns_native_transport(monkeypatch, tmp_path):
    runner = CliRunner()
    open_calls: list[str] = []
    prepare_calls: list[tuple[str, str, str, str | None]] = []
    native_launch_calls: list[tuple[str, str, str, str, str]] = []
    provider_home = tmp_path / ".claude"

    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (True, "authMethod=claude.ai, apiProvider=firstParty"),
    )
    monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command=(
                "zsh -lc 'exec claude --dangerously-skip-permissions --session-id provider-123 "
                "--channels server:longhouse-channel'"
            ),
            source_runner_name="work-laptop",
            managed_transport="claude_channel_bridge",
        ),
    )
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: True)
    # Simulate a non-Bedrock environment so force_flag_capable_path stays False.
    monkeypatch.setattr(claude_cli, "_collect_claude_launch_env", lambda: {})
    monkeypatch.setattr(
        claude_cli,
        "_ensure_native_claude_prereqs",
        lambda *, base_url, token, workspace_path, config_dir: prepare_calls.append(
            (base_url, token, str(workspace_path), str(config_dir) if config_dir else None)
        ),
    )
    monkeypatch.setattr(
        claude_cli,
        "_run_native_claude_tui",
        lambda *, session_id, provider_session_id, cwd, base_url, token: native_launch_calls.append(
            (session_id, provider_session_id, str(cwd), base_url, token)
        )
        or 0,
    )
    monkeypatch.setattr(claude_cli, "_open_session_url", lambda url: open_calls.append(url) or True)

    result = runner.invoke(
        app,
        [
            "claude",
            "--cwd",
            str(tmp_path),
            "--config-dir",
            str(provider_home),
            "--project",
            "demo",
            "--loop-mode",
            "assist",
            "--name",
            "Demo session",
            "--open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Longhouse Claude session launched on this machine." in result.output
    assert (
        "Attach: zsh -lc 'exec claude --dangerously-skip-permissions --session-id provider-123 "
        "--channels server:longhouse-channel'" in result.output
    )
    assert "Preparing native Claude bridge..." in result.output
    assert "Opening session in browser..." in result.output
    assert "Launching native Claude..." in result.output
    assert prepare_calls == [("https://longhouse.test", "zdt_test_token", str(tmp_path), str(provider_home))]
    assert native_launch_calls == [
        ("session-123", "provider-123", str(tmp_path), "https://longhouse.test", "zdt_test_token")
    ]
    assert open_calls == ["https://longhouse.test/timeline/session-123"]
    assert list_managed_session_contracts(tmp_path / ".longhouse") == []
    assert not (provider_home / "managed-local" / "contracts").exists()


def test_claude_no_attach_does_not_record_unlaunched_provider_contract(monkeypatch, tmp_path):
    runner = CliRunner()
    provider_home = tmp_path / ".claude"

    monkeypatch.setattr(
        claude_cli, "_load_api_credentials", lambda **_kwargs: ("https://longhouse.test", "zdt_test_token")
    )
    monkeypatch.setattr(claude_cli, "_detect_native_claude_channels_available", lambda: (True, "ok"))
    monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(claude_cli, "_collect_claude_launch_env", lambda: {})
    monkeypatch.setattr(claude_cli, "_ensure_native_claude_prereqs", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="attach",
            source_runner_name="work-laptop",
            managed_transport="claude_channel_bridge",
        ),
    )

    result = runner.invoke(
        app,
        ["claude", "--cwd", str(tmp_path), "--config-dir", str(provider_home), "--no-attach"],
    )

    assert result.exit_code == 0, result.output
    assert list_managed_session_contracts(tmp_path / ".longhouse") == []
    assert not (provider_home / "managed-local" / "contracts").exists()


def test_launch_detached_native_claude_channel_waits_for_channel_state(monkeypatch, tmp_path):
    prereq_calls: list[dict] = []
    popen_calls: list[dict] = []

    class _FakeProcess:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": list(cmd), **kwargs})
        return _FakeProcess()

    monkeypatch.setattr(
        claude_cli,
        "_ensure_native_claude_prereqs",
        lambda **kwargs: prereq_calls.append(kwargs),
    )
    monkeypatch.setattr(claude_cli.shutil, "which", lambda command: "/usr/bin/script" if command == "script" else None)
    monkeypatch.setattr(claude_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        claude_cli,
        "wait_for_claude_channel_state",
        lambda **_kwargs: {"ready": True, "port": 49200},
    )

    result = claude_cli._launch_detached_native_claude_channel(
        session_id="11111111-1111-4111-8111-111111111111",
        provider_session_id="22222222-2222-4222-8222-222222222222",
        cwd=tmp_path,
        base_url="https://longhouse.test",
        token="zdt_test_token",
        config_dir=tmp_path / ".claude",
    )

    assert result["provider"] == "claude"
    assert result["transport"] == "claude_channel_bridge"
    assert result["pid"] == 12345
    assert result["channel_state"] == {"ready": True, "port": 49200}
    assert prereq_calls[0]["base_url"] == "https://longhouse.test"
    assert popen_calls[0]["cwd"] == str(tmp_path)
    assert popen_calls[0]["cmd"][0] == "script"
    assert popen_calls[0]["stdout"] == claude_cli.subprocess.DEVNULL
    assert popen_calls[0]["stderr"] == claude_cli.subprocess.DEVNULL
    assert popen_calls[0]["env"]["LONGHOUSE_HOOK_TOKEN"] == "zdt_test_token"
    command_text = " ".join(popen_calls[0]["cmd"])
    assert "claude" in command_text
    assert "11111111-1111-4111-8111-111111111111" in command_text
    assert "zdt_test_token" not in command_text


def test_claude_channel_state_file_rejects_non_uuid_session_id(tmp_path):
    with pytest.raises(ValueError, match="valid UUID"):
        build_claude_channel_state_file(session_id="../../escape", state_root=tmp_path)


def test_run_claude_auth_status_uses_bare_claude(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true, "authMethod": "third_party", "apiProvider": "bedrock"}',
            stderr="",
        )

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    completed = claude_cli._run_claude_auth_status()

    assert completed.returncode == 0
    assert calls == [["claude", "auth", "status", "--json"]]


def test_verify_claude_channel_mcp_server_uses_effective_workspace_config(monkeypatch, tmp_path):
    calls: list[tuple[list[str], str]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs["cwd"]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    claude_cli._verify_claude_channel_mcp_server(workspace_path=tmp_path)

    assert calls == [(["claude", "mcp", "get", "longhouse-channel"], str(tmp_path))]


def test_verify_claude_channel_mcp_server_raises_actionable_setup_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout='No MCP server found with name: "longhouse-channel"',
            stderr="",
        ),
    )

    with pytest.raises(claude_cli._NativeClaudeError, match="Claude cannot resolve MCP server longhouse-channel"):
        claude_cli._verify_claude_channel_mcp_server(workspace_path=tmp_path)


def test_post_claude_terminal_signal_posts_runtime_event(monkeypatch, tmp_path):
    fake_client = _FakeClient(response=_FakeResponse(status_code=200, json_data={"accepted": 1}))
    monkeypatch.setattr(claude_cli.httpx, "Client", lambda timeout: fake_client)
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    outbox_dir = tmp_path / "runtime-events-outbox"
    monkeypatch.setattr(claude_cli, "get_agent_runtime_events_outbox_dir", lambda: outbox_dir)

    ok = claude_cli._post_claude_terminal_signal(
        base_url="https://longhouse.test",
        token="zdt_test_token",
        session_id="11111111-1111-4111-8111-111111111111",
        provider_session_id="provider-123",
        exit_code=0,
    )

    assert ok is True
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["url"] == "https://longhouse.test/api/agents/runtime/events/batch"
    assert call["headers"] == {"X-Agents-Token": "zdt_test_token"}
    event = call["json"]["events"][0]
    assert event["runtime_key"] == "claude:provider-123"
    assert event["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert event["provider"] == "claude"
    assert event["device_id"] == "work-laptop"
    assert event["source"] == "claude_channel_wrapper"
    assert event["kind"] == "terminal_signal"
    assert event["payload"]["terminal_state"] == "session_ended"
    assert event["payload"]["terminal_reason"] == "provider_exit"
    assert event["payload"]["terminal_source"] == "claude_channel_wrapper"
    assert event["payload"]["exit_code"] == 0
    assert list(outbox_dir.glob("*.json")) == []


def test_post_claude_terminal_signal_timeout_leaves_queued_event(monkeypatch, tmp_path, capsys):
    class _TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, *args, **kwargs):
            raise claude_cli.httpx.ReadTimeout("timed out")

    monkeypatch.setattr(claude_cli.httpx, "Client", lambda timeout: _TimeoutClient())
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    outbox_dir = tmp_path / "runtime-events-outbox"
    monkeypatch.setattr(claude_cli, "get_agent_runtime_events_outbox_dir", lambda: outbox_dir)

    ok = claude_cli._post_claude_terminal_signal(
        base_url="https://longhouse.test",
        token="zdt_test_token",
        session_id="11111111-1111-4111-8111-111111111111",
        provider_session_id="provider-123",
        exit_code=0,
    )

    assert ok is False
    assert "Queued for Machine Agent retry" in capsys.readouterr().out
    queued = list(outbox_dir.glob("*.json"))
    assert len(queued) == 1
    event = json.loads(queued[0].read_text(encoding="utf-8"))
    assert event["source"] == "claude_channel_wrapper"
    assert event["kind"] == "terminal_signal"
    assert event["payload"]["terminal_state"] == "session_ended"


def test_collect_claude_launch_env_filters_empty_values(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_PROFILE", "zh-qa-engineer")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ANTHROPIC_MODEL", "us.anthropic.claude-sonnet-4-6")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    env = claude_cli._collect_claude_launch_env()

    assert env == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_PROFILE": "zh-qa-engineer",
        "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
    }


def test_launch_env_requires_flag_capable_claude_path_when_explicit_launch_env_requests_it():
    assert claude_cli._launch_env_requires_flag_capable_claude_path({}) is False
    assert claude_cli._launch_env_requires_flag_capable_claude_path({"AWS_PROFILE": "zh-qa-engineer"}) is False
    assert claude_cli._launch_env_requires_flag_capable_claude_path({"CLAUDE_CODE_USE_BEDROCK": ""}) is False
    assert claude_cli._launch_env_requires_flag_capable_claude_path({"CLAUDE_CODE_USE_BEDROCK": "1"}) is True


def test_force_native_claude_channels_enabled_reads_hidden_override(monkeypatch):
    monkeypatch.delenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, raising=False)
    assert claude_cli._force_native_claude_channels_enabled() is False

    monkeypatch.setenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, "1")
    assert claude_cli._force_native_claude_channels_enabled() is True

    monkeypatch.setenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, "true")
    assert claude_cli._force_native_claude_channels_enabled() is True

    monkeypatch.setenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, "0")
    assert claude_cli._force_native_claude_channels_enabled() is False


def test_result_uses_native_claude_bridge_only_for_native_transport():
    native = claude_cli.ManagedLocalLaunchResponse(
        session_id="session-123",
        provider_session_id="provider-123",
        attach_command="attach",
        source_runner_name="work-laptop",
        managed_transport="claude_channel_bridge",
    )
    codex = claude_cli.ManagedLocalLaunchResponse(
        session_id="session-123",
        provider_session_id="provider-123",
        attach_command="attach",
        source_runner_name="work-laptop",
        managed_transport="codex_app_server",
    )

    assert claude_cli._result_uses_native_claude_bridge(native) is True
    assert claude_cli._result_uses_native_claude_bridge(codex) is False


def test_detect_native_claude_channels_available_true_for_first_party_auth(monkeypatch):
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty"}',
            stderr="",
        ),
    )

    available, detail = claude_cli._detect_native_claude_channels_available()

    assert available is True
    assert detail == "authMethod=claude.ai, apiProvider=firstParty"


def test_detect_native_claude_channels_available_true_for_any_logged_in_auth(monkeypatch):
    monkeypatch.setattr(
        claude_cli,
        "_run_claude_auth_status",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true, "authMethod": "third_party", "apiProvider": "bedrock"}',
            stderr="",
        ),
    )

    available, detail = claude_cli._detect_native_claude_channels_available()

    assert available is True
    assert detail == "authMethod=third_party, apiProvider=bedrock"


def test_claude_command_rejects_launch_envs_that_disable_native_channels(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

    monkeypatch.delenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, raising=False)
    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (True, "authMethod=claude.ai, apiProvider=firstParty"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_collect_claude_launch_env",
        lambda: {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
            "AWS_REGION": "us-east-1",
            "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
        },
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs),
    )
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: False)

    result = runner.invoke(
        app,
        [
            "claude",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert "Native Claude channels unavailable (disabled by Claude launch env)." in result.output
    assert launch_calls == []


def test_claude_command_rejects_native_bridge_when_launch_env_requires_flag_capable_path(monkeypatch, tmp_path):
    runner = CliRunner()
    native_finalize_calls: list[dict] = []

    monkeypatch.delenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, raising=False)
    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (True, "authMethod=claude.ai, apiProvider=firstParty"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_collect_claude_launch_env",
        lambda: {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
        },
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("API launch should not be attempted")),
    )
    monkeypatch.setattr(
        claude_cli,
        "_finalize_native_claude_launch",
        lambda **kwargs: native_finalize_calls.append(kwargs),
    )

    result = runner.invoke(
        app,
        [
            "claude",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert native_finalize_calls == []
    assert "Native Claude channels unavailable (disabled by Claude launch env)." in result.output


def test_claude_command_force_native_channels_bypasses_bedrock_gate(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []
    native_finalize_calls: list[dict] = []

    monkeypatch.setenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, "1")
    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (False, "authMethod=third_party, apiProvider=bedrock"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_collect_claude_launch_env",
        lambda: {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
        },
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(claude_cli, "_ensure_native_claude_prereqs", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs)
        or claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec claude --resume provider-123'",
            source_runner_name="work-laptop",
            managed_transport="claude_channel_bridge",
        ),
    )
    monkeypatch.setattr(
        claude_cli,
        "_finalize_native_claude_launch",
        lambda **kwargs: native_finalize_calls.append(kwargs),
    )

    result = runner.invoke(app, ["claude", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert f"Forcing native Claude channels via {claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV}=1." in result.output
    assert "disabled by Claude launch env" not in result.output
    assert launch_calls[0]["native_claude_channels_available"] is True
    assert native_finalize_calls


def test_claude_command_force_native_channels_allows_bedrock_native_transport(monkeypatch, tmp_path):
    runner = CliRunner()
    native_finalize_calls: list[dict] = []

    monkeypatch.setenv(claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV, "1")
    monkeypatch.setattr(
        claude_cli,
        "_load_api_credentials",
        lambda **_kwargs: ("https://longhouse.test", "zdt_test_token"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_detect_native_claude_channels_available",
        lambda: (False, "authMethod=third_party, apiProvider=bedrock"),
    )
    monkeypatch.setattr(
        claude_cli,
        "_collect_claude_launch_env",
        lambda: {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_PROFILE": "zh-qa-engineer",
        },
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(claude_cli, "_ensure_managed_launch_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(claude_cli, "_ensure_native_claude_prereqs", lambda **_kwargs: None)
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec claude --resume provider-123'",
            source_runner_name="work-laptop",
            managed_transport="claude_channel_bridge",
        ),
    )
    monkeypatch.setattr(
        claude_cli,
        "_finalize_native_claude_launch",
        lambda **kwargs: native_finalize_calls.append(kwargs),
    )

    result = runner.invoke(app, ["claude", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert native_finalize_calls
    assert "requires the permissive Claude path" not in result.output
