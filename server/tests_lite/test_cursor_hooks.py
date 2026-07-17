from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from zerg.services.cursor_hooks import install_cursor_hooks


class _PermissionServer:
    def __init__(self, decision: str):
        self.decision = decision
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or "0")
                outer.requests.append(json.loads(self.rfile.read(size)))
                self.reply({"pause_request_id": "pause-1", "request_key": "key-1", "status": "pending"})

            def do_GET(self):
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
    assert "afterAgentResponse" in config["hooks"]


def test_cursor_permission_timeout_returns_to_local_prompt(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    env = dict(os.environ)
    env.update(
        {
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_HOME": str(tmp_path / "longhouse"),
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

    assert json.loads(result.stdout) == {
        "permission": "ask",
        "user_message": "Longhouse unavailable; decide in Cursor",
    }
    presence = list((tmp_path / "longhouse" / "agent" / "outbox").glob("prs.*.json"))
    assert len(presence) == 1
    assert json.loads(presence[0].read_text())["state"] == "thinking"


def test_cursor_permission_hook_returns_exact_remote_allow_and_deny(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    for decision in ("allow", "deny"):
        with _PermissionServer(decision) as server:
            env = dict(os.environ)
            env.update(
                {
                    "LONGHOUSE_SESSION_ID": "managed-session",
                    "LONGHOUSE_HOME": str(tmp_path / f"longhouse-{decision}"),
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
