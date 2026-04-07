from __future__ import annotations

import os
from pathlib import Path
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
                "attach_command": "zsh -lc 'exec tmux attach -t lh-demo'",
                "source_runner_name": "work-laptop",
                "managed_transport": "tmux",
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
    assert result.attach_command == "zsh -lc 'exec tmux attach -t lh-demo'"
    assert result.managed_transport == "tmux"
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


def test_claude_command_prints_attach_command_and_auto_attaches(monkeypatch, tmp_path):
    runner = CliRunner()
    attach_calls: list[str] = []
    open_calls: list[str] = []
    launch_calls: list[dict] = []

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
        lambda **kwargs: launch_calls.append(kwargs)
        or claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec tmux attach -t lh-demo'",
            source_runner_name="work-laptop",
        ),
    )
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: True)
    monkeypatch.setattr(claude_cli, "_run_attach_command", lambda command: attach_calls.append(command) or 0)
    monkeypatch.setattr(claude_cli, "_open_session_url", lambda url: open_calls.append(url) or True)

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

    assert result.exit_code == 0, result.output
    assert "Longhouse: https://longhouse.test" in result.output
    assert "Native Claude channels unavailable (disabled by Claude launch env); using tmux fallback." in result.output
    assert "Longhouse Claude session launched on this machine." in result.output
    assert "Session ID: session-123" in result.output
    assert "Provider session ID: provider-123" in result.output
    assert "Session URL: https://longhouse.test/timeline/session-123" in result.output
    assert "Attach: zsh -lc 'exec tmux attach -t lh-demo'" in result.output
    assert "Opening session in browser..." in result.output
    assert "Attaching..." in result.output
    assert open_calls == ["https://longhouse.test/timeline/session-123"]
    assert attach_calls == ["zsh -lc 'exec tmux attach -t lh-demo'"]
    assert launch_calls[0]["claude_launch_env"] == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_PROFILE": "zh-qa-engineer",
        "AWS_REGION": "us-east-1",
        "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-4-6",
    }


def test_claude_command_starts_native_channel_bridge_when_api_returns_native_transport(monkeypatch, tmp_path):
    runner = CliRunner()
    open_calls: list[str] = []
    prepare_calls: list[tuple[str, str, str, str | None]] = []
    native_launch_calls: list[tuple[str, str, str, str, str]] = []

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
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **_kwargs: claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command=(
                "zsh -lc 'exec claude --dangerously-skip-permissions --session-id provider-123 "
                "--dangerously-load-development-channels server:longhouse-channel'"
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
        "--dangerously-load-development-channels server:longhouse-channel'" in result.output
    )
    assert "Preparing native Claude bridge..." in result.output
    assert "Opening session in browser..." in result.output
    assert "If launch seems stuck, press Enter on 'I am using this for local development'." in result.output
    assert "Launching native Claude..." in result.output
    assert prepare_calls == [("https://longhouse.test", "zdt_test_token", str(tmp_path), None)]
    assert native_launch_calls == [
        ("session-123", "provider-123", str(tmp_path), "https://longhouse.test", "zdt_test_token")
    ]
    assert open_calls == ["https://longhouse.test/timeline/session-123"]


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


def test_assert_private_forced_native_claude_ready_allows_patched_binary(monkeypatch):
    monkeypatch.setattr(
        claude_cli,
        "_inspect_private_native_claude_patch",
        lambda: ("patched", None, "known private patch present (2 matches)"),
    )

    claude_cli._assert_private_forced_native_claude_ready()


def test_assert_private_forced_native_claude_ready_rejects_unpatched_binary(monkeypatch):
    monkeypatch.setattr(
        claude_cli,
        "_inspect_private_native_claude_patch",
        lambda: ("unknown", None, "known private patch bytes not found in active binary"),
    )

    with pytest.raises(ClickExit) as exc_info:
        claude_cli._assert_private_forced_native_claude_ready()

    assert exc_info.value.exit_code == claude_cli.EXIT_SETUP_FAILED


def test_launch_env_requires_flag_capable_claude_path_when_explicit_launch_env_requests_it():
    assert claude_cli._launch_env_requires_flag_capable_claude_path({}) is False
    assert claude_cli._launch_env_requires_flag_capable_claude_path({"AWS_PROFILE": "zh-qa-engineer"}) is False
    assert claude_cli._launch_env_requires_flag_capable_claude_path({"CLAUDE_CODE_USE_BEDROCK": ""}) is False
    assert (
        claude_cli._launch_env_requires_flag_capable_claude_path({"CLAUDE_CODE_USE_BEDROCK": "1"}) is True
    )


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
    tmux = claude_cli.ManagedLocalLaunchResponse(
        session_id="session-123",
        provider_session_id="provider-123",
        attach_command="attach",
        source_runner_name="work-laptop",
        managed_transport="tmux",
    )

    assert claude_cli._result_uses_native_claude_bridge(native) is True
    assert claude_cli._result_uses_native_claude_bridge(tmux) is False


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


def test_detect_native_claude_channels_available_false_for_bedrock(monkeypatch):
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

    assert available is False
    assert detail == "authMethod=third_party, apiProvider=bedrock"


def test_claude_command_forces_tmux_path_when_launch_env_requires_flag_capable_path(monkeypatch, tmp_path):
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
        lambda **kwargs: launch_calls.append(kwargs)
        or claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec tmux attach -t lh-demo'",
            source_runner_name="work-laptop",
            managed_transport="tmux",
        ),
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

    assert result.exit_code == 0, result.output
    assert "Native Claude channels unavailable (disabled by Claude launch env); using tmux fallback." in result.output
    assert launch_calls[0]["native_claude_channels_available"] is False


def test_claude_command_rejects_native_bridge_when_launch_env_requires_flag_capable_path(monkeypatch, tmp_path):
    runner = CliRunner()
    native_finalize_calls: list[dict] = []

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
    assert "requires the permissive Claude path" in result.output


def test_claude_command_force_native_channels_bypasses_bedrock_tmux_gate(monkeypatch, tmp_path):
    runner = CliRunner()
    launch_calls: list[dict] = []

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
    monkeypatch.setattr(
        claude_cli,
        "_inspect_private_native_claude_patch",
        lambda: ("patched", None, "known private patch present (2 matches)"),
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
    monkeypatch.setattr(
        claude_cli,
        "_launch_managed_local_from_api",
        lambda **kwargs: launch_calls.append(kwargs)
        or claude_cli.ManagedLocalLaunchResponse(
            session_id="session-123",
            provider_session_id="provider-123",
            attach_command="zsh -lc 'exec tmux attach -t lh-demo'",
            source_runner_name="work-laptop",
            managed_transport="tmux",
        ),
    )
    monkeypatch.setattr(claude_cli, "_interactive_stdio", lambda: False)

    result = runner.invoke(app, ["claude", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (
        f"Forcing native Claude channels via {claude_cli._FORCE_NATIVE_CLAUDE_CHANNELS_ENV}=1."
        in result.output
    )
    assert "disabled by Claude launch env" not in result.output
    assert launch_calls[0]["native_claude_channels_available"] is True


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
    monkeypatch.setattr(
        claude_cli,
        "_inspect_private_native_claude_patch",
        lambda: ("patched", None, "known private patch present (2 matches)"),
    )
    monkeypatch.setattr(claude_cli, "get_machine_name_label", lambda: "work-laptop")
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


def test_claude_command_force_native_channels_rejects_unpatched_private_bedrock_path(monkeypatch, tmp_path):
    runner = CliRunner()

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
    monkeypatch.setattr(
        claude_cli,
        "_inspect_private_native_claude_patch",
        lambda: ("unknown", Path("/Users/davidrose/.local/share/claude/versions/2.1.94"), "known private patch bytes not found in active binary"),
    )

    result = runner.invoke(app, ["claude", "--cwd", str(tmp_path)])

    assert result.exit_code == claude_cli.EXIT_SETUP_FAILED
    assert "does not contain the known private" in result.output
    assert "repointed ~/.local/bin/claude to a fresh unpatched version" in result.output
