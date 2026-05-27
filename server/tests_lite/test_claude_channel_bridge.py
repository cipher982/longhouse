from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

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
        "command": "longhouse",
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
        cwd="/tmp/demo",
        resume=False,
        claude_command="claude",
    )

    assert "--dangerously-load-development-channels server:longhouse-channel" in command
    assert "--channels server:longhouse-channel" not in command


def test_claude_channel_bridge_emits_channel_notification_after_init(tmp_path):
    session_id = "11111111-1111-1111-1111-111111111111"
    provider_session_id = "provider-123"
    state_root = tmp_path / "bridge-state"
    server_cwd = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zerg.cli.main",
            "claude-channel",
            "serve",
            "--session-id",
            session_id,
            "--provider-session-id",
            provider_session_id,
            "--state-root",
            str(state_root),
            "--auth-token",
            "bridge-test-token",
            "--claude-pid",
            str(os.getpid()),
        ],
        cwd=str(server_cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    stdout_lines: "queue.Queue[str]" = queue.Queue()

    def _pump_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            stdout_lines.put(line)

    reader = threading.Thread(target=_pump_stdout, daemon=True)
    reader.start()

    try:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "0.1.0"},
                    },
                }
            )
            + "\n"
        )
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.flush()

        init_response = json.loads(stdout_lines.get(timeout=5.0))
        assert init_response["id"] == 1
        assert init_response["result"]["capabilities"]["experimental"] == {"claude/channel": {}}

        state = wait_for_claude_channel_state(session_id=session_id, state_root=state_root, timeout_secs=5.0)
        assert state["session_id"] == session_id
        assert state["provider_session_id"] == provider_session_id
        assert state["auth_token"] == "bridge-test-token"
        assert state["ready"] is True
        assert build_claude_channel_state_file(session_id=session_id, state_root=state_root).exists()

        send = subprocess.run(
            [
                sys.executable,
                "-m",
                "zerg.cli.main",
                "claude-channel",
                "send",
                "--session-id",
                session_id,
                "--state-root",
                str(state_root),
                "--text",
                "hello from pytest",
                "--meta",
                "user=pm",
            ],
            cwd=str(server_cwd),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert send.returncode == 0, send.stderr or send.stdout

        notification = json.loads(stdout_lines.get(timeout=5.0))
        assert notification["method"] == "notifications/claude/channel"
        assert notification["params"]["content"] == "hello from pytest"
        assert notification["params"]["meta"] == {
            "injected_by": "longhouse",
            "longhouse_session_id": session_id,
            "user": "pm",
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


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
