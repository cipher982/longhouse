"""Subprocess tests for the real PreToolUse permission_gate.py hook.

The hook is safety-critical: it can gate a real tool execution, so it MUST fail
open (emit no decision, exit 0) on timeout or any error, and only emit allow/deny
when Longhouse explicitly resolves the request.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

HOOK_SCRIPT = (
    Path(__file__).resolve().parents[2] / "config" / "claude-hooks" / "scripts" / "permission_gate.py"
)


class _StubLonghouse:
    """Minimal stand-in for the permission-request/decision endpoints."""

    def __init__(self, *, decision: str | None, resolved: bool):
        self._decision = decision
        self._resolved = resolved
        self.requests_seen: list[dict] = []
        self.decision_polls = 0
        handler = self._build_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _build_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
                if self.path == "/api/agents/permission-requests":
                    outer.requests_seen.append(body)
                    self._reply(200, {"pause_request_id": "p1", "request_key": "k1", "status": "pending"})
                else:
                    self._reply(404, {})

            def do_GET(self):
                if self.path.startswith("/api/agents/permission-decision"):
                    outer.decision_polls += 1
                    self._reply(
                        200,
                        {
                            "decision": outer._decision,
                            "reason": "stub",
                            "resolved": outer._resolved,
                        },
                    )
                else:
                    self._reply(404, {})

            def _reply(self, code: int, payload: dict):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


def _run_hook(*, base_url: str | None, timeout_env: str = "3", extra_env: dict | None = None):
    env = {
        "LONGHOUSE_HOOK_TOKEN": "zht_test",
        "LONGHOUSE_MANAGED_SESSION_ID": "11111111-1111-1111-1111-111111111111",
        "LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S": timeout_env,
        "PATH": "/usr/bin:/bin",
    }
    if base_url is not None:
        env["LONGHOUSE_HOOK_URL"] = base_url
    if extra_env:
        env.update(extra_env)
    hook_input = json.dumps(
        {
            "session_id": "11111111-1111-1111-1111-111111111111",
            "tool_use_id": "toolu_test",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=hook_input,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _parse_decision(stdout: str) -> str | None:
    out = stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"]


def test_hook_emits_allow_when_resolved_allow():
    with _StubLonghouse(decision="allow", resolved=True) as stub:
        result = _run_hook(base_url=stub.base_url)
    assert result.returncode == 0, result.stderr
    assert _parse_decision(result.stdout) == "allow"
    assert stub.requests_seen and stub.requests_seen[0]["tool_use_id"] == "toolu_test"


def test_hook_emits_deny_when_resolved_deny():
    with _StubLonghouse(decision="deny", resolved=True) as stub:
        result = _run_hook(base_url=stub.base_url)
    assert result.returncode == 0, result.stderr
    assert _parse_decision(result.stdout) == "deny"


def test_hook_fails_open_on_timeout():
    # Server always answers "pending" → hook should give up and emit nothing.
    with _StubLonghouse(decision=None, resolved=False) as stub:
        result = _run_hook(base_url=stub.base_url, timeout_env="1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert stub.decision_polls >= 1  # it really polled before giving up


def test_hook_fails_open_when_server_unreachable():
    # Point at a closed port; registration fails → fail open, no decision.
    result = _run_hook(base_url="http://127.0.0.1:1", timeout_env="2")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_fails_open_when_unconfigured():
    result = _run_hook(base_url=None)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_disabled_via_env():
    with _StubLonghouse(decision="allow", resolved=True) as stub:
        result = _run_hook(base_url=stub.base_url, extra_env={"LONGHOUSE_PERMISSION_HOOK_ENABLED": "0"})
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not stub.requests_seen  # never even registered
