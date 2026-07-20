from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from zerg.services.cursor_hooks import install_cursor_hooks


def _seed_launch(home: Path, *, session_id: str = "managed-session", conversation_id: str = "cursor-id") -> str:
    launch_id = "launch-1"
    root = home / "managed-local" / "cursor-helm"
    claims = root / "binding-probes"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "cursor",
                "status": "pending",
                "session_id": session_id,
                "conversation_uuid": conversation_id,
                "launch_id": launch_id,
                "permission_policy": "remote_human",
            }
        )
    )
    (root / f"{session_id}.json").write_text(json.dumps({"session_id": session_id, "registration": "registered"}))
    return launch_id


class _PermissionServer:
    def __init__(self, decision: str | None):
        self.decision = decision
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.user_agents: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                outer.paths.append(self.path)
                size = int(self.headers.get("Content-Length") or "0")
                outer.user_agents.append(self.headers.get("User-Agent") or "")
                outer.requests.append(json.loads(self.rfile.read(size)))
                if self.path.endswith("/expire"):
                    self.reply({"resolved": True, "decision": "deny"})
                else:
                    self.reply({"pause_request_id": "pause-1", "request_key": "key-1", "status": "pending"})

            def do_GET(self):
                outer.paths.append(self.path)
                if outer.decision is None:
                    self.reply({"resolved": False, "decision": None})
                else:
                    self.reply({"resolved": True, "decision": outer.decision, "reason": "integration test"})

            def reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()


def test_cursor_hook_install_preserves_user_hooks_and_is_idempotent(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    user = {"command": "./hooks/user.py", "timeout": 3}
    (cursor / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"beforeShellExecution": [user]}}))

    install_cursor_hooks(cursor)
    first = (cursor / "hooks.json").read_text()
    install_cursor_hooks(cursor)
    config = json.loads((cursor / "hooks.json").read_text())

    assert (cursor / "hooks.json").read_text() == first
    assert config["hooks"]["beforeShellExecution"][0] == user
    assert sum("longhouse-cursor-hook.py" in item["command"] for item in config["hooks"]["beforeShellExecution"]) == 1
    assert sum(
        "longhouse-cursor-permission-hook.py" in item["command"]
        for item in config["hooks"]["beforeShellExecution"]
    ) == 1
    longhouse_shell = next(
        item for item in config["hooks"]["beforeShellExecution"] if "longhouse-cursor-hook.py" in item["command"]
    )
    longhouse_lifecycle = next(
        item for item in config["hooks"]["afterAgentResponse"] if "longhouse-cursor-hook.py" in item["command"]
    )
    permission_shell = next(
        item
        for item in config["hooks"]["beforeShellExecution"]
        if "longhouse-cursor-permission-hook.py" in item["command"]
    )
    assert longhouse_shell["failClosed"] is False
    assert permission_shell["failClosed"] is True
    assert longhouse_lifecycle["failClosed"] is False
    assert "afterAgentResponse" in config["hooks"]


def test_cursor_hook_does_not_overwrite_mismatched_launch_reservation(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    claims = tmp_path / "longhouse" / "managed-local" / "cursor-helm" / "binding-probes"
    claims.mkdir(parents=True)
    target = claims / "managed-session.json"
    reserved = {
        "schema_version": 2,
        "provider": "cursor",
        "status": "pending",
        "session_id": "managed-session",
        "conversation_uuid": "reserved-cursor-id",
    }
    target.write_text(json.dumps(reserved))
    result = subprocess.run(
        [str(script), "sessionStart"],
        input=json.dumps({"conversation_id": "different-cursor-id"}),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_HOME": str(tmp_path / "longhouse"),
        },
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout) == {}
    assert json.loads(target.read_text()) == reserved


def test_cursor_hook_does_not_promote_binding_before_registration(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    home = tmp_path / "longhouse"
    launch_id = _seed_launch(home)
    state_path = home / "managed-local" / "cursor-helm" / "managed-session.json"
    state_path.write_text(json.dumps({"session_id": "managed-session", "registration": "degraded"}))
    claim_path = home / "managed-local" / "cursor-helm" / "binding-probes" / "managed-session.json"

    subprocess.run(
        [str(script), "sessionStart"],
        input=json.dumps({"conversation_id": "cursor-id"}),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
            "LONGHOUSE_HOME": str(home),
        },
        timeout=5,
        check=True,
    )

    assert json.loads(claim_path.read_text())["status"] == "pending"


def test_cursor_hook_promotes_claim_without_overwriting_phase_and_removes_backup(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    home = tmp_path / "longhouse"
    launch_id = _seed_launch(home)
    root = home / "managed-local" / "cursor-helm"
    (root / "managed-session.json").write_text(
        json.dumps({"session_id": "managed-session", "registration": "registered"})
    )
    backup = root / "binding-probes" / "managed-session.observed-backup.json"
    backup.write_text(json.dumps({"status": "observed"}))

    subprocess.run(
        [str(script), "sessionStart"],
        input=json.dumps({"conversation_id": "cursor-id"}),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
            "LONGHOUSE_HOME": str(home),
        },
        timeout=5,
        check=True,
    )

    assert json.loads((root / "managed-session.phase.json").read_text())["phase"] == "idle"
    assert json.loads((root / "binding-probes" / "managed-session.json").read_text())["status"] == "observed"
    assert not backup.exists()


def test_cursor_permission_transport_failure_blocks_instead_of_failing_open(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-permission-hook.py"
    home = tmp_path / "longhouse"
    launch_id = _seed_launch(home)
    env = dict(os.environ)
    env.update(
        {
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_HOME": str(home),
            "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
            "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
            "LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S": "0",
            "LONGHOUSE_HOOK_URL": "http://127.0.0.1:1",
            "LONGHOUSE_HOOK_TOKEN": "test-token",
        }
    )
    result = subprocess.run(
        [str(script), "beforeShellExecution"],
        input=json.dumps(
            {
                "conversation_id": "cursor-id",
                "generation_id": "generation-id",
                "command": "pwd",
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout)["permission"] == "deny"
    assert "could not be reached" in json.loads(result.stdout)["user_message"]
    assert "registration_unreachable" in result.stderr


def test_cursor_permission_hook_returns_exact_remote_allow_and_deny(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-permission-hook.py"
    for decision in ("allow", "deny"):
        with _PermissionServer(decision) as server:
            home = tmp_path / f"longhouse-{decision}"
            launch_id = _seed_launch(home)
            env = dict(os.environ)
            env.update(
                {
                    "LONGHOUSE_SESSION_ID": "managed-session",
                    "LONGHOUSE_HOME": str(home),
                    "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
                    "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
                    "LONGHOUSE_HOOK_URL": server.url,
                    "LONGHOUSE_HOOK_TOKEN": "session-token",
                }
            )
            result = subprocess.run(
                [str(script), "beforeShellExecution"],
                input=json.dumps({"conversation_id": "cursor-id", "generation_id": "gen-1", "command": "pwd"}),
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
                check=True,
            )
        assert json.loads(result.stdout)["permission"] == decision
        assert server.requests[0]["provider"] == "cursor"
        assert server.user_agents == ["Longhouse-Cursor-Permission-Hook/1"]


def test_identical_cursor_tool_calls_get_distinct_permission_requests(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-permission-hook.py"
    home = tmp_path / "longhouse"
    launch_id = _seed_launch(home)
    env = {
        **os.environ,
        "LONGHOUSE_SESSION_ID": "managed-session",
        "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
        "LONGHOUSE_HOME": str(home),
        "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
        "LONGHOUSE_HOOK_TOKEN": "session-token",
    }
    with _PermissionServer("allow") as server:
        env["LONGHOUSE_HOOK_URL"] = server.url
        for _ in range(2):
            subprocess.run(
                [str(script), "beforeShellExecution"],
                input=json.dumps({"conversation_id": "cursor-id", "generation_id": "gen-1", "command": "pwd"}),
                text=True,
                capture_output=True,
                env=env,
                timeout=5,
                check=True,
            )

    assert len(server.requests) == 2
    assert server.requests[0]["tool_use_id"] != server.requests[1]["tool_use_id"]


def test_cursor_permission_deadline_expires_exact_remote_prompt(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-permission-hook.py"
    home = tmp_path / "longhouse"
    launch_id = _seed_launch(home)
    env = {
        **os.environ,
        "LONGHOUSE_SESSION_ID": "managed-session",
        "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
        "LONGHOUSE_HOME": str(home),
        "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
        "LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S": "1",
        "LONGHOUSE_HOOK_TOKEN": "session-token",
    }
    with _PermissionServer(None) as server:
        env["LONGHOUSE_HOOK_URL"] = server.url
        result = subprocess.run(
            [str(script), "beforeShellExecution"],
            input=json.dumps({"conversation_id": "cursor-id", "generation_id": "gen-1", "command": "pwd"}),
            text=True,
            capture_output=True,
            env=env,
            timeout=5,
            check=True,
        )

    output = json.loads(result.stdout)
    assert output["permission"] == "deny"
    assert "No human approval" in output["user_message"]
    assert "timeout_no_decision" in result.stderr
    assert "/api/agents/permission-requests/pause-1/expire" in server.paths


def test_cursor_permission_hook_is_inert_without_remote_policy(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-permission-hook.py"
    home = tmp_path / "must-not-be-created"
    env = {
        **os.environ,
        "LONGHOUSE_HOME": str(home),
    }
    result = subprocess.run(
        [str(script), "beforeShellExecution"],
        input="not-json-and-must-not-be-read",
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout) == {}
    assert not home.exists()


def test_cursor_stop_wakes_engine_with_exact_managed_store(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    conversation_id = "cursor-id"
    store = cursor / "chats" / "workspace" / conversation_id / "store.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"cursor-store")
    longhouse_home = Path("/tmp") / f"lh-cursor-wake-{uuid.uuid4().hex[:8]}"
    wake_socket = longhouse_home / "agent" / "transcript-wake.sock"
    wake_socket.parent.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(wake_socket))
    listener.listen(1)
    received: list[dict] = []

    def accept_wake() -> None:
        conn, _ = listener.accept()
        with conn:
            data = bytearray()
            while chunk := conn.recv(4096):
                data.extend(chunk)
        received.append(json.loads(data))

    thread = threading.Thread(target=accept_wake, daemon=True)
    thread.start()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    env = dict(os.environ)
    launch_id = _seed_launch(longhouse_home, conversation_id=conversation_id)
    env.update(
        {
            "CURSOR_HOME": str(cursor),
            "LONGHOUSE_HOME": str(longhouse_home),
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_CURSOR_LAUNCH_ID": launch_id,
        }
    )
    result = subprocess.run(
        [str(script), "stop"],
        input=json.dumps({"conversation_id": conversation_id, "generation_id": "generation-1"}),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=True,
    )
    thread.join(timeout=2)
    listener.close()

    assert json.loads(result.stdout) == {}
    assert received == [
        {
            "provider": "cursor",
            "path": str(store),
            "phase": "idle",
            "session_id": "managed-session",
            "turn_id": "generation-1",
            "wake_reason": "turn_completed",
            "observed_at_ms": received[0]["observed_at_ms"],
            "file_len_hint": len(b"cursor-store"),
        }
    ]
    shutil.rmtree(longhouse_home)
