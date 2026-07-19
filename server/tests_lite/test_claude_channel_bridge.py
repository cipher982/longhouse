from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

import zerg.cli.claude_channel as claude_channel_cli
from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.claude_channel_bridge import build_claude_channel_state_file
from zerg.services.claude_channel_bridge import install_claude_channel_mcp_server
from zerg.services.claude_channel_bridge import resolve_claude_user_config_path
from zerg.services.claude_channel_bridge import wait_for_claude_channel_state


def test_install_claude_channel_mcp_server_writes_user_scope_entry(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    workspace = tmp_path / "real-workspace"
    workspace.mkdir()
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(workspace, target_is_directory=True)
    config_path = resolve_claude_user_config_path(claude_dir=claude_dir)
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {"existing": {"type": "stdio", "command": "demo", "args": ["serve"]}},
                "projects": {
                    "/tmp/other-project": {
                        "allowedTools": ["Read"],
                        "mcpContextUris": ["ctx://demo"],
                        "mcpServers": {
                            "other": {"type": "stdio", "command": "other", "args": ["serve"]},
                            CLAUDE_CHANNEL_SERVER_NAME: {
                                "type": "stdio",
                                "command": "stale-longhouse",
                                "args": ["claude-channel", "serve"],
                            },
                        },
                        "enabledMcpjsonServers": ["shared"],
                        "disabledMcpjsonServers": [],
                        "hasTrustDialogAccepted": True,
                        "projectOnboardingSeenCount": 2,
                        "hasClaudeMdExternalIncludesApproved": True,
                        "hasClaudeMdExternalIncludesWarningShown": False,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    actions = install_claude_channel_mcp_server(workspace_path=workspace_link, claude_dir=claude_dir)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert actions == [
        f"Updated {config_path} with user MCP server {CLAUDE_CHANNEL_SERVER_NAME}",
        f"Removed project-local MCP server {CLAUDE_CHANNEL_SERVER_NAME} from 1 Claude project(s)",
    ]
    assert data["mcpServers"]["existing"] == {"type": "stdio", "command": "demo", "args": ["serve"]}
    assert data["mcpServers"][CLAUDE_CHANNEL_SERVER_NAME] == {
        "type": "stdio",
        "command": "longhouse-engine",
        "args": ["claude-channel", "serve"],
        "env": {},
    }
    assert data["projects"]["/tmp/other-project"]["mcpServers"]["other"] == {
        "type": "stdio",
        "command": "other",
        "args": ["serve"],
    }
    assert CLAUDE_CHANNEL_SERVER_NAME not in data["projects"]["/tmp/other-project"]["mcpServers"]
    assert str(workspace.resolve()) not in data["projects"]


def test_build_claude_channel_exec_command_uses_development_channel_flag():
    command = build_claude_channel_exec_command(
        provider_session_id="provider-123",
        longhouse_session_id="11111111-1111-1111-1111-111111111111",
        longhouse_run_id="22222222-2222-4222-8222-222222222222",
        cwd="/tmp/demo",
        resume=False,
        claude_command="claude",
    )

    assert "--dangerously-load-development-channels server:longhouse-channel" in command
    assert "--channels server:longhouse-channel" not in command
    assert "LONGHOUSE_CHANNEL_CWD=/tmp/demo" in command
    assert "LONGHOUSE_RUN_ID=22222222-2222-4222-8222-222222222222" in command


def test_build_claude_channel_exec_command_defaults_to_bypass():
    # Default (bypass) must keep --dangerously-skip-permissions and NOT engage the
    # permission gate — this is the historical autonomous behavior.
    command = build_claude_channel_exec_command(
        provider_session_id="provider-123",
        longhouse_session_id="11111111-1111-1111-1111-111111111111",
        cwd="/tmp/demo",
        resume=False,
        claude_command="claude",
    )
    assert "--dangerously-skip-permissions" in command
    # Bypass must explicitly force the gate OFF so an inherited env var from the
    # parent shell cannot engage it.
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=0" in command
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=1" not in command


def test_build_claude_channel_exec_command_remote_approve_drops_bypass_and_engages_gate():
    from zerg.services.claude_channel_bridge import CLAUDE_PERMISSION_MODE_REMOTE_APPROVE

    command = build_claude_channel_exec_command(
        provider_session_id="provider-123",
        longhouse_session_id="11111111-1111-1111-1111-111111111111",
        cwd="/tmp/demo",
        resume=False,
        claude_command="claude",
        permission_mode=CLAUDE_PERMISSION_MODE_REMOTE_APPROVE,
    )
    # Remote-approve must NOT bypass permissions, and must engage the gate.
    assert "--dangerously-skip-permissions" not in command
    assert "LONGHOUSE_PERMISSION_HOOK_ENABLED=1" in command


def test_build_claude_channel_exec_command_fresh_uses_session_id_flag():
    command = build_claude_channel_exec_command(
        provider_session_id="11111111-1111-1111-1111-111111111111",
        longhouse_session_id="11111111-1111-1111-1111-111111111111",
        cwd="/tmp/demo",
        resume=False,
        claude_command="claude",
    )

    # Fresh launch pins the session id; it must NOT resume.
    assert "--session-id 11111111-1111-1111-1111-111111111111" in command
    assert "--resume" not in command


def test_build_claude_channel_exec_command_resume_uses_resume_flag():
    command = build_claude_channel_exec_command(
        provider_session_id="11111111-1111-1111-1111-111111111111",
        longhouse_session_id="11111111-1111-1111-1111-111111111111",
        cwd="/tmp/demo",
        resume=True,
        claude_command="claude",
    )

    # Resume re-opens the existing provider session by id.
    assert "--resume 11111111-1111-1111-1111-111111111111" in command
    assert "--session-id 11111111-1111-1111-1111-111111111111" not in command


def test_claude_channel_send_shim_dispatches_to_engine(monkeypatch, tmp_path):
    session_id = "11111111-1111-1111-1111-111111111111"
    state_root = tmp_path / "bridge-state"
    calls: list[list[str]] = []

    def fake_run_engine(argv: list[str]) -> None:
        calls.append(argv)

    monkeypatch.setenv("LONGHOUSE_ENGINE_BIN", "/tmp/longhouse-engine")
    monkeypatch.setattr(claude_channel_cli, "_run_engine", fake_run_engine)

    result = CliRunner().invoke(
        claude_channel_cli.app,
        [
            "send",
            "--session-id",
            session_id,
            "--state-root",
            str(state_root),
            "--text",
            "hello from pytest",
            "--meta",
            "user=pm",
            "--wait-secs",
            "1.5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "/tmp/longhouse-engine",
            "claude-channel",
            "send",
            "--session-id",
            session_id,
            "--text",
            "hello from pytest",
            "--wait-secs",
            "1.5",
            "--meta",
            "user=pm",
            "--state-root",
            str(state_root),
        ]
    ]


def test_claude_channel_serve_shim_execs_engine_with_env_token_only(monkeypatch, tmp_path):
    session_id = "11111111-1111-1111-1111-111111111111"
    state_root = tmp_path / "bridge-state"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_exec_engine(argv: list[str], env: dict[str, str]) -> None:
        calls.append((argv, env))

    monkeypatch.setenv("LONGHOUSE_ENGINE_BIN", "/tmp/longhouse-engine")
    monkeypatch.setenv("LONGHOUSE_CHANNEL_AUTH_TOKEN", "bridge-test-token")
    monkeypatch.setattr(claude_channel_cli, "_exec_engine", fake_exec_engine)

    result = CliRunner().invoke(
        claude_channel_cli.app,
        [
            "serve",
            "--session-id",
            session_id,
            "--state-root",
            str(state_root),
            "--port",
            "4242",
        ],
    )

    assert result.exit_code == 0, result.output
    argv, env = calls[0]
    assert argv == [
        "/tmp/longhouse-engine",
        "claude-channel",
        "serve",
        "--session-id",
        session_id,
        "--state-root",
        str(state_root),
        "--port",
        "4242",
    ]
    assert "bridge-test-token" not in " ".join(argv)
    assert env["LONGHOUSE_CHANNEL_AUTH_TOKEN"] == "bridge-test-token"


def test_claude_channel_serve_shim_preserves_stdio_through_exec(tmp_path):
    fake_engine = tmp_path / "longhouse-engine"
    argv_path = tmp_path / "argv.json"
    env_path = tmp_path / "env.json"
    fake_engine.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, sys",
                f"open({str(argv_path)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv))",
                f"open({str(env_path)!r}, 'w', encoding='utf-8').write(json.dumps({{'token': os.environ.get('LONGHOUSE_CHANNEL_AUTH_TOKEN')}}))",
                "line = sys.stdin.readline()",
                "sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'stdin': line.strip()}}) + '\\n')",
                "sys.stdout.flush()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_engine.chmod(0o755)

    env = os.environ.copy()
    env["LONGHOUSE_ENGINE_BIN"] = str(fake_engine)
    env["LONGHOUSE_CHANNEL_AUTH_TOKEN"] = "bridge-test-token"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zerg.cli.main",
            "claude-channel",
            "serve",
            "--session-id",
            "11111111-1111-1111-1111-111111111111",
            "--state-root",
            str(tmp_path / "state"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["result"]["stdin"] == '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr else ""
        assert process.wait(timeout=5.0) == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert argv[:5] == [
        str(fake_engine),
        "claude-channel",
        "serve",
        "--session-id",
        "11111111-1111-1111-1111-111111111111",
    ]
    assert "bridge-test-token" not in " ".join(argv)
    assert json.loads(env_path.read_text(encoding="utf-8")) == {"token": "bridge-test-token"}


def test_wait_for_claude_channel_state_waits_for_ready_transition(tmp_path):
    session_id = "22222222-2222-2222-2222-222222222222"
    state_root = tmp_path / "bridge-state"
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"ready": False}) + "\n", encoding="utf-8")

    def _mark_ready() -> None:
        time.sleep(0.2)
        state_path.write_text(json.dumps({"ready": True, "port": 1234}) + "\n", encoding="utf-8")

    writer = threading.Thread(target=_mark_ready, daemon=True)
    writer.start()

    state = wait_for_claude_channel_state(session_id=session_id, state_root=state_root, timeout_secs=2.0)

    assert state["ready"] is True
    assert state["port"] == 1234


def test_wait_for_claude_channel_state_ignores_partial_json_writes(tmp_path):
    session_id = "33333333-3333-3333-3333-333333333333"
    state_root = tmp_path / "bridge-state"
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("", encoding="utf-8")

    def _mark_ready() -> None:
        time.sleep(0.2)
        state_path.write_text(json.dumps({"ready": True, "port": 3210}) + "\n", encoding="utf-8")

    writer = threading.Thread(target=_mark_ready, daemon=True)
    writer.start()

    state = wait_for_claude_channel_state(session_id=session_id, state_root=state_root, timeout_secs=2.0)

    assert state["ready"] is True
    assert state["port"] == 3210
