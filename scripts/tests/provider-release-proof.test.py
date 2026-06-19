#!/usr/bin/env python3
"""Tests for the provider release-proof artifact wrapper."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts" / "qa" / "provider-release-proof.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_fake_provider_bin(root: Path, version: str = "provider 9.9.9") -> None:
    _write_exe(
        root / "fake-provider",
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

print("unexpected args", sys.argv[1:], file=sys.stderr)
raise SystemExit(2)
""",
    )


def _write_fake_claude_provider_live_bin(root: Path) -> None:
    _write_exe(
        root / "fake-provider",
        r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.9.9-fake (Claude Code)")
    raise SystemExit(0)

if args == ["--help"]:
    if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1":
        print("--resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    if os.environ.get("FAKE_CLAUDE_CHANNELS_MISSING") == "1":
        print("unknown option --dangerously-load-development-channels", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("FAKE_CLAUDE_CHANNELS_UNCONFIRMED") == "1":
        print("--session-id --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
        raise SystemExit(0)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _write_fake_codex_real_tool_bin(root: Path) -> None:
    _write_exe(
        root / "fake-provider",
        r"""#!/usr/bin/env python3
import json
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 9.9.9")
    raise SystemExit(0)

if args[:2] == ["exec", "--json"]:
    prompt = args[-1] if args else ""
    match = re.search(r"LONGHOUSE_CODEX_REAL_TOOL_[A-Za-z0-9_]+", prompt)
    marker = match.group(0) if match else "LONGHOUSE_CODEX_REAL_TOOL_missing"
    command = f"printf '{marker}\\n'"
    print(json.dumps({"item": {
        "id": "call_fake_tool",
        "type": "command_execution",
        "status": "completed",
        "exit_code": 0,
        "command": command,
        "aggregated_output": marker + "\n",
    }}))
    print(json.dumps({"item": {"type": "agent_message", "text": "DONE"}}))
    raise SystemExit(0)

print("unexpected fake codex args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _write_fake_opencode_server_bin(root: Path) -> None:
    _write_exe(
        root / "fake-provider",
        r"""#!/usr/bin/env python3
import base64
import http.server
import json
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

args = sys.argv[1:]
if args == ["--version"]:
    print("opencode 9.9.9-release-e2e-fake")
    raise SystemExit(0)

if args == ["attach", "--help"]:
    print("opencode attach <url>")
    print("-s, --session session id")
    print("-p, --password defaults to OPENCODE_SERVER_PASSWORD")
    print("-u, --username defaults to OPENCODE_SERVER_USERNAME")
    raise SystemExit(0)

if not args or args[0] != "serve":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
provider_session_id = "ses_fake_release_universal_e2e"
state_path = Path.cwd() / ".fake-opencode-state.json"

def load_messages():
    try:
        payload = json.loads(state_path.read_text())
    except Exception:
        return []
    messages = payload.get("messages")
    return messages if isinstance(messages, list) else []

messages = load_messages()

def save_state():
    state_path.write_text(json.dumps({"messages": messages}))

def make_doc():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/global/health": {"get": {"operationId": "global.health"}},
            "/session": {"post": {"operationId": "session.create"}},
            "/session/{sessionID}": {"get": {"operationId": "session.get"}},
            "/session/{sessionID}/message": {
                "get": {"operationId": "session.messages"},
                "post": {"operationId": "session.prompt"},
            },
            "/session/{sessionID}/prompt_async": {
                "post": {
                    "operationId": "session.prompt_async",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "noReply": {"type": "boolean"},
                                        "parts": {"type": "array"},
                                    },
                                }
                            }
                        }
                    },
                }
            },
            "/session/{sessionID}/abort": {"post": {"operationId": "session.abort"}},
        },
    }

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if self.path == "/global/health":
            self._json({"healthy": True})
            return
        if self.path == "/doc":
            self._json(make_doc())
            return
        if self.path == f"/session/{provider_session_id}":
            self._json({"id": provider_session_id})
            return
        if parsed.path == f"/session/{provider_session_id}/message":
            self._json(messages)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if parsed.path == "/session":
            self._json({
                "id": provider_session_id,
                "cost": 0,
                "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            })
            return
        if parsed.path == f"/session/{provider_session_id}/prompt_async":
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            messages.append({
                "info": {"id": "msg_fake_user", "sessionID": provider_session_id, "role": "user"},
                "parts": payload.get("parts") or [],
            })
            save_state()
            self._empty()
            return
        if parsed.path == f"/session/{provider_session_id}/abort":
            self._json(True)
            return
        self._json({"error": "not found"}, 404)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
""",
    )


@contextlib.contextmanager
def _fake_claude_machine_live_server(*, mode: str = "success"):
    requests: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return

        def _write(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "token": self.headers.get("X-Agents-Token"),
                    "body": body,
                }
            )
            if mode == "legacy_extra_forbidden" and body.get("run_live_token_contract"):
                self._write(
                    422,
                    {
                        "detail": [
                            {
                                "input": True,
                                "loc": ["body", "run_live_token_contract"],
                                "msg": "Extra inputs are not permitted",
                                "type": "extra_forbidden",
                            },
                            {
                                "input": body.get("live_token_timeout_secs"),
                                "loc": ["body", "live_token_timeout_secs"],
                                "msg": "Extra inputs are not permitted",
                                "type": "extra_forbidden",
                            },
                        ]
                    },
                )
                return
            self._write(
                202,
                {
                    "operation_id": "op_claude_machine_live",
                    "status_url": "/api/agents/operations/op_claude_machine_live",
                },
            )

        def do_GET(self) -> None:
            requests.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "token": self.headers.get("X-Agents-Token"),
                }
            )
            if mode == "failed":
                self._write(
                    200,
                    {
                        "status": "failed",
                        "error": {
                            "code": "provider_live_operation_failed",
                            "message": "Machine Agent control channel is offline",
                        },
                    },
                )
                return
            operation_evidence = {
                "launch_local": {
                    "status": "pass",
                    "level": "live_no_token",
                    "source": "fake machine proof launch",
                },
                "send_input": {
                    "status": "pass",
                    "level": "manual_live_token",
                    "source": "fake machine proof send",
                },
                "transcript_binding": {
                    "status": "pass",
                    "level": "manual_live_token",
                    "source": "fake machine proof transcript",
                },
                "steer_active_turn": {
                    "status": "pass",
                    "level": "manual_live_token",
                    "source": "fake machine proof steer",
                },
            }
            if mode == "legacy_extra_forbidden":
                operation_evidence = {
                    "launch_local": {
                        "status": "pass",
                        "level": "live_no_token",
                        "source": "fake legacy machine proof launch",
                    },
                }
            self._write(
                200,
                {
                    "status": "succeeded",
                    "command_id": "cmd_claude_machine_live",
                    "result": {
                        "provider": "claude",
                        "transport": "provider_live_proof",
                        "publish": True,
                        "exit_code": 0,
                        "artifact": {
                            "artifact_kind": "provider_live_canary",
                            "provider": "claude",
                            "provider_version": "Claude Code 2.9.9",
                            "verdict": "green",
                            "failure_code": None,
                            "recommendation": "upgrade_allowed",
                            "canaries": {"live_contract": {"status": "pass"}},
                            "operation_evidence": operation_evidence,
                        },
                    },
                },
            )

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_fake_repo(root: Path) -> None:
    manifest = {
        "schema_version": 1,
        "providers": [
            {
                "provider": "opencode",
                "operation_evidence": {
                    "launch_local": {
                        "level": "live_no_token",
                        "source": "fake provider-live canary",
                    },
                    "send_input": {
                        "level": "live_no_token",
                        "source": "fake provider-live canary",
                    },
                },
            }
        ],
    }
    manifest_path = root / "server" / "zerg" / "config" / "managed_provider_contracts.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _write_exe(
        root / "scripts" / "qa" / "provider-live-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

if os.environ.get("FAKE_TIMEOUT") == "1":
    time.sleep(10)

if os.environ.get("FAKE_SKIP_ARTIFACT") == "1":
    raise SystemExit(0)

provider = value("--provider")
if provider == "claude":
    missing = ["--session-id"] if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1" else []
    channels_unconfirmed = os.environ.get("FAKE_CLAUDE_CHANNELS_UNCONFIRMED") == "1"
    channels_missing = os.environ.get("FAKE_CLAUDE_CHANNELS_MISSING") == "1"
    pty_missing = os.environ.get("FAKE_CLAUDE_PTY_MISSING") == "1"
    verdict = "red" if (missing or channels_missing or pty_missing) else "yellow" if channels_unconfirmed else "green"
    failure_code = (
        "claude_command_contract_missing"
        if missing
        else "claude_development_channels_contract_missing"
        if channels_missing
        else "claude_detached_pty_unavailable"
        if pty_missing
        else None
    )
    artifact = {
        "artifact_kind": "provider_live_canary",
        "provider": "claude",
        "provider_version": "Claude Code 2.9.9",
        "verdict": verdict,
        "failure_code": failure_code,
        "recommendation": "block_upgrade_recommendation"
        if failure_code
        else "investigate_before_upgrade"
        if channels_unconfirmed
        else "upgrade_allowed",
        "canaries": {
            "binary_identity": {"status": "pass", "version": "Claude Code 2.9.9"},
            "command_shape": {
                "status": "fail" if missing else "pass",
                "failure_code": "claude_command_contract_missing" if missing else None,
                "missing": missing,
            },
            "channels_shape": {
                "status": "fail" if channels_missing else "warn" if channels_unconfirmed else "pass",
                "failure_code": "claude_development_channels_contract_missing"
                if channels_missing
                else None,
                "reason": "claude_development_channels_contract_unconfirmed"
                if channels_unconfirmed
                else None,
                "missing": ["--dangerously-load-development-channels"]
                if channels_missing
                else ["--resume"]
                if channels_unconfirmed
                else [],
            },
            "detached_pty_shape": {
                "status": "fail" if pty_missing else "pass",
                "failure_code": "claude_detached_pty_unavailable" if pty_missing else None,
                "platform": "darwin",
            },
        },
        "operation_evidence": {
            "launch_local": {
                "status": "fail" if failure_code else "pass",
                "level": "none" if failure_code else "live_no_token",
                "canary": "claude_launch_local_no_token",
                "failure_code": failure_code,
            }
        },
    }
    Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
    Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
    raise SystemExit(1 if verdict == "red" else 0)
if provider == "antigravity":
    artifact = {
        "artifact_kind": "provider_live_canary",
        "provider": "antigravity",
        "provider_version": "agy 1.0.3",
        "verdict": "green",
        "failure_code": None,
        "recommendation": "upgrade_allowed",
        "canaries": {
            "binary_identity": {"status": "pass", "version": "agy 1.0.3"},
            "command_shape": {"status": "pass"},
            "plugin_contract": {"status": "pass"},
            "global_hooks_contract": {"status": "pass"},
        },
        "operation_evidence": {
            "launch_local": {
                "status": "pass",
                "level": "live_no_token",
                "canary": "antigravity_launch_local_no_token",
            }
        },
    }
    Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
    Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
    raise SystemExit(0)

verdict = os.environ.get("FAKE_VERDICT", "green")
artifact = {
    "artifact_kind": "provider_live_canary",
    "provider": provider,
    "provider_version": "opencode 1.2.3",
    "verdict": verdict,
    "failure_code": None if verdict == "green" else "fake_provider_break",
    "recommendation": "upgrade_allowed" if verdict == "green" else "block_upgrade_recommendation",
    "canaries": {
        "server_contract": {
            "status": "pass" if verdict == "green" else "fail",
            "failure_code": None if verdict == "green" else "fake_provider_break",
        }
    },
    "operation_evidence": {
        "launch_local": {"status": "pass", "level": "live_no_token", "canary": "server_contract"},
        "send_input": {
            "status": "pass" if verdict == "green" else "fail",
            "level": "live_no_token",
            "canary": "server_contract",
            "failure_code": None if verdict == "green" else "fake_provider_break",
        },
    },
    "session_projection": {
        "artifact_kind": "provider_live_session_projection",
        "provider": provider,
        "status": "captured" if verdict == "green" else "partial",
        "provider_session_id": "ses_fake_release_proof",
        "operation_statuses": {
            "send_input": {
                "status": "pass" if verdict == "green" else "fail",
                "level": "live_no_token" if verdict == "green" else "none",
                "canary": "server_contract",
            }
        },
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(1 if os.environ.get("FAKE_EXIT_ONE") == "1" else 0 if verdict != "red" else 1)
""",
    )

    _write_exe(
        root / "scripts" / "qa" / "provider-control-e2e-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag):
    return args[args.index(flag) + 1]

args_path = os.environ.get("FAKE_CONTROL_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps(args), encoding="utf-8")

provider = value("--provider")
if provider == "opencode":
    status = "fail" if os.environ.get("FAKE_OPENCODE_CONTROL_FAIL") == "1" else "pass"
    failure_code = "fake_opencode_tool_failed" if status == "fail" else None
    canary = "opencode_real_tool_result_shape"
    operation = "transcript_binding"
    level = "none" if status == "fail" else "live_token"
elif provider == "claude":
    status = "fail" if os.environ.get("FAKE_CLAUDE_REAL_PRINT_FAIL") == "1" else "pass"
    failure_code = "fake_claude_real_print_failed" if status == "fail" else None
    canary = "claude_real_print"
    operation = "live_token_behavior"
    level = "none" if status == "fail" else "live_token"
else:
    status = "fail" if os.environ.get("FAKE_ANTIGRAVITY_CONTROL_FAIL") == "1" else "pass"
    failure_code = "fake_antigravity_send_failed" if status == "fail" else None
    canary = "antigravity_real_agy_send"
    operation = "send_input"
    level = "none" if status == "fail" else "live_token"
artifact = {
    "schema_version": 1,
    "provider": provider,
    "verdict": "red" if status == "fail" else "green",
    "failure_code": failure_code,
    "canaries": {
        provider: {
            "status": status,
            "failure_code": failure_code,
            "operation_evidence": {
                operation: {
                    "status": status,
                    "level": level,
                    "source": "fake provider control live canary",
                    "canary": canary,
                    "failure_code": failure_code,
                }
            },
        }
    },
}
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(1 if status == "fail" else 0)
""",
    )

    _write_exe(
        root / "scripts" / "qa" / "codex-provider-release-canary.py",
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]

def value(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default

args_path = os.environ.get("FAKE_CODEX_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps(args), encoding="utf-8")
env_path = os.environ.get("FAKE_CODEX_ENV_PATH")
if env_path:
    Path(env_path).write_text(
        json.dumps(
            {
                "CODEX_AGENTS_TOKEN": os.environ.get("CODEX_AGENTS_TOKEN"),
                "CODEX_API_URL": os.environ.get("CODEX_API_URL"),
            }
        ),
        encoding="utf-8",
    )
if os.environ.get("FAKE_CODEX_SKIP_ARTIFACT") == "1":
    print(os.environ.get("CODEX_AGENTS_TOKEN", ""))
    raise SystemExit(0)

source_review_status = value("--source-review-status", "missing")
run_real_tool = "--run-real-tool" in args
run_live_interrupt = "--run-managed-live-interrupt" in args
real_tool_status = "fail" if os.environ.get("FAKE_CODEX_REAL_TOOL_FAIL") == "1" else "pass"
real_tool_failure_code = "fake_codex_real_tool_failed" if real_tool_status == "fail" else None
live_interrupt_status = "fail" if os.environ.get("FAKE_CODEX_INTERRUPT_FAIL") == "1" else "pass"
live_interrupt_failure_code = "fake_codex_interrupt_failed" if live_interrupt_status == "fail" else None
verdict = "yellow" if source_review_status == "not_run" else "green"
failure_code = "insufficient_coverage" if source_review_status == "not_run" else None
recommendation = "investigate_before_upgrade" if source_review_status == "not_run" else "upgrade_allowed"
if run_real_tool and real_tool_status == "fail":
    verdict = "red"
    failure_code = real_tool_failure_code
    recommendation = "block_upgrade_recommendation"
if run_live_interrupt and live_interrupt_status == "fail":
    verdict = "red"
    failure_code = live_interrupt_failure_code
    recommendation = "block_upgrade_recommendation"
artifact = {
    "artifact_kind": "codex_provider_release_canary",
    "provider": "codex",
    "codex_version": value("--provider-version", "codex 9.9.9"),
    "codex_bin": value("--codex-bin"),
    "longhouse_commit": "abc123",
    "verdict": verdict,
    "failure_code": failure_code,
    "recommendation": recommendation,
    "source_review": {"status": source_review_status, "note": value("--source-review-note", "")},
    "canaries": {
        "binary_identity": {
            "status": "pass",
            "version": value("--provider-version", "codex 9.9.9"),
            "path": value("--codex-bin"),
        },
        "raw_fresh_remote": {
            "status": "pass",
            "protocol_fingerprints": {
                "status": "ok",
                "path": "/tmp/noisy/codex.jsonl",
                "responses": {"initialize": {"platformFamily": "str"}},
                "notifications": {"thread/started": {"threadId": "str"}},
                "server_requests": {},
                "response_errors": {},
            },
        }
    },
    "operation_evidence": {
        "launch_local": {
            "status": "not_run" if source_review_status == "not_run" else "pass",
            "level": "none" if source_review_status == "not_run" else "live_no_token",
            "canary": "fake_codex_release_canary",
        }
    },
}
if run_real_tool:
    artifact["canaries"]["codex_real_tool_result_shape"] = {
        "status": real_tool_status,
        "failure_code": real_tool_failure_code,
        "command_status": "completed" if real_tool_status == "pass" else None,
        "command_exit_code": 0 if real_tool_status == "pass" else None,
        "command_exact_match": real_tool_status == "pass",
        "output_exact_match": real_tool_status == "pass",
    }
    artifact["operation_evidence"]["run_once"] = {
        "status": real_tool_status,
        "level": "none" if real_tool_status == "fail" else "live_token",
        "canary": "codex_real_tool_result_shape",
        "failure_code": real_tool_failure_code,
    }
    artifact["operation_evidence"]["transcript_binding"] = {
        "status": real_tool_status,
        "level": "none" if real_tool_status == "fail" else "live_token",
        "canary": "codex_real_tool_result_shape",
        "failure_code": real_tool_failure_code,
    }
if run_live_interrupt:
    artifact["canaries"]["managed_live_interrupt"] = {
        "status": live_interrupt_status,
        "failure_code": live_interrupt_failure_code,
        "last_turn_status": "interrupted" if live_interrupt_status == "pass" else "completed",
    }
    artifact["operation_evidence"]["interrupt"] = {
        "status": live_interrupt_status,
        "level": "none" if live_interrupt_status == "fail" else "live_token",
        "canary": "managed_live_interrupt",
        "failure_code": live_interrupt_failure_code,
    }
Path(value("--artifact")).parent.mkdir(parents=True, exist_ok=True)
Path(value("--artifact")).write_text(json.dumps(artifact), encoding="utf-8")
raise SystemExit(1 if verdict == "red" else 0)
""",
    )


def _write_fake_claude_real_print_binary(path: Path, *, mode: str = "success") -> Path:
    return _write_exe(
        path,
        f"""#!/usr/bin/env python3
import json
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.181-fake (Claude Code)")
    raise SystemExit(0)
if args == ["auth", "status", "--json"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "fake-auth", "apiProvider": "fake-provider"}}))
    raise SystemExit(0)
if "--print" not in args:
    print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)
prompt = sys.stdin.read()
match = re.search(r"exactly ([A-Za-z0-9_]+)", prompt)
marker = match.group(1) if match else "MISSING_MARKER"
session_id = "fake-claude-print-session"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": session_id}}))
if {mode!r} == "api_error":
    print(json.dumps({{
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error_status": 401,
        "error": "authentication_failed",
        "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
        "session_id": session_id,
    }}))
    raise SystemExit(0)
print(json.dumps({{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": marker,
    "stop_reason": "end_turn",
    "session_id": session_id,
}}))
""",
    )


def _run_proof(
    root: Path,
    provider: str,
    *,
    env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(root / "repo"),
            "--provider",
            provider,
            "--provider-bin",
            str(root / "fake-provider"),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
            *(extra_args or []),
        ],
        cwd=REPO_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, json.loads(artifact.read_text(encoding="utf-8"))


def test_opencode_release_proof_normalizes_source_canary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(root, "opencode")

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["artifact_kind"] == "provider_release_proof"
        assert payload["provider"] == "opencode"
        assert payload["provider_version"] == "opencode 1.2.3"
        assert payload["scenario_id"] == "opencode-release-proof-v1"
        assert payload["scenario_profile"] == "default"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["canaries"]["source_canary"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["normalized"]["canaries"]["server_contract"]["status"] == "pass"
        assert Path(payload["artifacts"]["normalized_contract"]).exists()
        provider_contract = _read_json(Path(payload["artifacts"]["provider_contract"]))
        operation_evidence = _read_json(Path(payload["artifacts"]["operation_evidence"]))
        session_projection = _read_json(Path(payload["artifacts"]["session_projection"]))
        assert provider_contract["contract_operations"]["send_input"]["level"] == "live_no_token"
        assert operation_evidence["operation_evidence"]["send_input"]["status"] == "pass"
        assert session_projection["status"] == "captured"
        assert session_projection["projection"]["provider_session_id"] == "ses_fake_release_proof"


def test_release_proof_can_attach_universal_harness_for_all_providers() -> None:
    for provider in ("claude", "codex", "opencode", "antigravity"):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_fake_repo(root / "repo")
            _write_fake_provider_bin(root, f"{provider} 9.9.9")
            fixture = root / "fixture.jsonl"
            fixture.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "text": "hello"}),
                        json.dumps({"type": "assistant", "text": "world"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result, payload = _run_proof(
                root,
                provider,
                extra_args=[
                    "--run-universal-harness",
                    "--universal-fixture-path",
                    str(fixture),
                ],
            )

            assert result.returncode == 0, result.stderr + result.stdout
            assert Path(payload["artifacts"]["universal_harness_artifact"]).exists()
            assert Path(payload["artifacts"]["universal_harness_evidence_root"]).is_dir()
            assert Path(payload["artifacts"]["action_matrix"]).exists()
            assert Path(payload["artifacts"]["control_surface"]).exists()
            universal = payload["normalized"]["universal_harness"]
            assert universal["result_count"] == 8
            assert "action_matrix" in universal["scenarios"]
            assert "control_surface" in universal["scenarios"]
            assert "parse_ingest_project" in universal["scenarios"]
            assert payload["normalized"]["canaries"]["universal_probe_identity"]["status"] == "pass"
            assert payload["normalized"]["canaries"]["universal_collect_raw_evidence"]["status"] == "pass"
            assert payload["normalized"]["canaries"]["universal_action_matrix"]["status"] == "warn"
            assert payload["normalized"]["canaries"]["universal_control_surface"]["status"] == "warn"
            assert payload["normalized"]["canaries"]["universal_parse_ingest_project"]["status"] == "pass"
            assert payload["normalized"]["canaries"]["universal_run_prompt_once"]["status"] in {
                "pass",
                "warn",
            }
            action_matrix = payload["action_matrix"]
            assert action_matrix["action_count"] > 10
            assert action_matrix["action_ids"]
            action_statuses = {row["action_id"]: row for row in action_matrix["actions"]}
            assert action_statuses["old_new_release_diff"]["status"] == "pass"
            assert action_statuses["old_new_release_diff"]["evidence_level"] == "artifact_diff"
            control_surface = payload["control_surface"]
            assert 8 < control_surface["action_count"] < action_matrix["action_count"]
            assert "send_message" in control_surface["action_ids"]
            assert "old_new_release_diff" not in control_surface["action_ids"]
            control_counts = payload["normalized"]["control_surface"]["status_counts"]
            assert control_counts.get("blocked", 0) + control_counts.get("unsupported_gap", 0) >= 1


def test_release_proof_exposes_universal_session_evidence_for_codex_and_opencode() -> None:
    for provider in ("codex", "opencode"):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_fake_repo(root / "repo")
            _write_fake_provider_bin(root, f"{provider} 9.9.9")

            result, payload = _run_proof(
                root,
                provider,
                extra_args=["--run-universal-harness"],
            )

            assert result.returncode == 0, result.stderr + result.stdout
            assert payload["normalized"]["canaries"]["universal_launch_managed_session"]["status"] == "pass"
            assert payload["normalized"]["canaries"]["universal_send_receive"]["status"] == "pass"
            assert payload["operation_evidence"]["universal_launch_local"]["status"] == "pass"
            assert payload["operation_evidence"]["universal_send_input"]["status"] == "pass"
            assert payload["operation_evidence"]["universal_transcript_binding"]["status"] == "pass"
            operation_evidence = _read_json(Path(payload["artifacts"]["operation_evidence"]))
            assert operation_evidence["operation_evidence"]["universal_send_input"]["level"] == "hermetic"
            universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
            send_receive = [
                item
                for item in universal_artifact["results"]
                if item["provider"] == provider and item["scenario"] == "send_receive"
            ][0]
            session_path = Path(send_receive["data"]["session_projection_path"])
            session = _read_json(session_path)
            assert session["has_user"] is True
            assert session["has_assistant"] is True


def test_release_proof_can_attach_universal_db_ingest_project() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_provider_bin(root, "codex 9.9.9")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--run-universal-harness",
                "--universal-scenario",
                "db_ingest_project",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["normalized"]["canaries"]["universal_db_ingest_project"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_db_ingest"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_db_ingest"]["level"] == "hermetic"
        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        db_result = universal_artifact["results"][0]
        assert db_result["scenario"] == "db_ingest_project"
        assert db_result["status"] == "pass"
        db_snapshot = _read_json(Path(db_result["data"]["db_snapshot_path"]))
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["timeline"]["matched"] is True
        assert "universal db ingest hello" in db_snapshot["export_jsonl"]


def test_codex_release_proof_can_attach_universal_interrupt_credentials_gap() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_provider_bin(root, "codex-cli 9.9.9")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--run-universal-harness",
                "--universal-scenario",
                "interrupt_cancel",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["normalized"]["canaries"]["universal_interrupt_cancel"]["status"] == "warn"
        assert payload["operation_evidence"]["universal_interrupt"]["status"] == "unsupported_gap"
        assert payload["operation_evidence"]["universal_interrupt"]["level"] == "live_token_required"

        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        result_row = universal_artifact["results"][0]
        assert result_row["scenario"] == "interrupt_cancel"
        assert result_row["status"] == "unsupported_gap"
        assert result_row["failure_code"] == "codex_managed_bridge_credentials_missing"
        assert result_row["data"]["missing"] == ["--agents-token", "--api-url"]


def test_codex_release_proof_can_attach_universal_tool_call_result_e2e() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_codex_real_tool_bin(root)

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--source-review-status",
                "pass",
                "--run-universal-harness",
                "--universal-scenario",
                "tool_call_result",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["normalized"]["canaries"]["universal_tool_call_result"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_tool_call_result"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_tool_call_result"]["level"] == "live_token"
        assert payload["operation_evidence"]["universal_db_ingest"]["status"] == "pass"

        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        result_row = universal_artifact["results"][0]
        assert result_row["scenario"] == "tool_call_result"
        assert result_row["status"] == "pass"
        assert result_row["data"]["source_artifact_kind"] == "provider_release_canary"
        assert result_row["data"]["synthetic"] is False

        evidence_root = Path(result_row["evidence_root"])
        raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
        assert "call_fake_tool" in raw_events
        assert "codex_real_tool_result_shape" in raw_events
        db_snapshot = _read_json(evidence_root / "longhouse" / "db-ingest-result.json")
        assert db_snapshot["ingest_result"]["events_inserted"] == 3
        assert db_snapshot["session_counts"]["tool_calls"] == 1
        assert db_snapshot["timeline"]["matched"] is True


def test_opencode_release_proof_can_attach_real_universal_managed_session_e2e() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_opencode_server_bin(root)

        result, payload = _run_proof(
            root,
            "opencode",
            extra_args=[
                "--run-universal-harness",
                "--universal-scenario",
                "managed_session_e2e",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["normalized"]["canaries"]["universal_managed_session_e2e"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_send_input"]["level"] == "live_no_token"
        assert (
            payload["operation_evidence"]["universal_send_input"]["canary"] == "opencode_prompt_async_no_reply_delivery"
        )
        assert payload["operation_evidence"]["universal_transcript_binding"]["level"] == "live_no_token"

        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        e2e_result = universal_artifact["results"][0]
        assert e2e_result["scenario"] == "managed_session_e2e"
        assert e2e_result["status"] == "pass"
        assert e2e_result["data"]["source_artifact_kind"] == "provider_live_canary"
        assert e2e_result["data"]["synthetic"] is False
        assert e2e_result["data"]["longhouse_ingest"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_db_ingest"]["status"] == "pass"

        evidence_root = Path(e2e_result["evidence_root"])
        assert (evidence_root / "raw" / "provider-live-canary.json").is_file()
        assert (evidence_root / "raw" / "provider-live-evidence" / "opencode-server.log").is_file()
        db_snapshot = _read_json(evidence_root / "longhouse" / "db-ingest-result.json")
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["timeline"]["matched"] is True
        raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
        assert "provider_live_canary" in raw_events
        assert '"synthetic": true' not in raw_events


def test_claude_release_proof_can_attach_universal_provider_live_contract_e2e() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_claude_provider_live_bin(root)

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--run-universal-harness",
                "--universal-scenario",
                "managed_session_e2e",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["normalized"]["canaries"]["universal_managed_session_e2e"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_launch_local"]["level"] == "live_no_token"
        assert payload["operation_evidence"]["universal_external_event_channel"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_runtime_phase"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_send_input"]["status"] == "blocked"
        assert payload["operation_evidence"]["universal_send_input"]["level"] == "live_token_required"
        assert payload["operation_evidence"]["universal_steer_active_turn"]["status"] == "blocked"
        assert payload["operation_evidence"]["universal_db_ingest"]["status"] == "pass"

        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        e2e_result = universal_artifact["results"][0]
        assert e2e_result["scenario"] == "managed_session_e2e"
        assert e2e_result["status"] == "pass"
        assert e2e_result["data"]["source_artifact_kind"] == "provider_live_canary"
        assert e2e_result["data"]["synthetic"] is False
        assert e2e_result["data"]["longhouse_ingest"]["status"] == "pass"

        evidence_root = Path(e2e_result["evidence_root"])
        assert (evidence_root / "raw" / "provider-live-canary.json").is_file()
        db_snapshot = _read_json(evidence_root / "longhouse" / "db-ingest-result.json")
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["timeline"]["matched"] is True
        raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
        assert "provider_live_canary" in raw_events
        assert "channels_shape" in raw_events


def test_antigravity_release_proof_can_attach_universal_hook_inbox_e2e() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        _write_fake_provider_bin(root, "agy 9.9.9")

        result, payload = _run_proof(
            root,
            "antigravity",
            extra_args=[
                "--run-universal-harness",
                "--universal-scenario",
                "managed_session_e2e",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["normalized"]["canaries"]["universal_managed_session_e2e"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_external_event_channel"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_send_input"]["level"] == "hermetic"
        assert payload["operation_evidence"]["universal_runtime_phase"]["status"] == "pass"
        assert payload["operation_evidence"]["universal_db_ingest"]["status"] == "pass"

        universal_artifact = _read_json(Path(payload["artifacts"]["universal_harness_artifact"]))
        e2e_result = universal_artifact["results"][0]
        assert e2e_result["scenario"] == "managed_session_e2e"
        assert e2e_result["status"] == "pass"
        assert e2e_result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
        assert e2e_result["data"]["synthetic"] is False
        assert e2e_result["data"]["longhouse_ingest"]["status"] == "pass"

        evidence_root = Path(e2e_result["evidence_root"])
        assert (evidence_root / "raw" / "provider-control-e2e.json").is_file()
        db_snapshot = _read_json(evidence_root / "longhouse" / "db-ingest-result.json")
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["timeline"]["matched"] is True
        raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
        assert "provider_control_e2e_canary" in raw_events
        assert "force_continue" in raw_events


def test_opencode_release_proof_blocks_on_source_canary_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_VERDICT": "red"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_provider_break"
        assert payload["canaries"]["source_canary"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"


def test_opencode_release_proof_blocks_green_artifact_from_failed_source_canary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_EXIT_ONE": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "source_canary_returncode_mismatch"
        assert payload["source_canary_returncode"] == 1


def test_opencode_release_proof_blocks_when_source_artifact_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_SKIP_ARTIFACT": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_source_missing"


def test_opencode_release_proof_blocks_when_source_canary_times_out() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_TIMEOUT": "1"},
            extra_args=["--timeout-secs", "1"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_timeout"


def test_codex_release_proof_maps_provider_binary_and_keeps_source_review_honest() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        env_path = root / "codex-env.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={
                "FAKE_CODEX_ARGS_PATH": str(args_path),
                "FAKE_CODEX_ENV_PATH": str(env_path),
            },
            extra_args=[
                "--provider-version",
                "codex 2.0.0",
                "--codex-run-raw-fresh-remote",
                "--codex-run-managed-tui-attach",
                "--codex-run-detached-ui",
                "--codex-run-managed-live-send",
                "--codex-api-url",
                "http://longhouse.test",
                "--codex-agents-token",
                "secret-token",
            ],
        )

        assert result.returncode == 0
        codex_args = json.loads(args_path.read_text(encoding="utf-8"))
        codex_env = json.loads(env_path.read_text(encoding="utf-8"))
        assert codex_args[codex_args.index("--codex-bin") + 1] == str(root / "fake-provider")
        assert codex_args[codex_args.index("--source-review-status") + 1] == "not_run"
        assert "--run-raw-fresh-remote" in codex_args
        assert "--run-managed-tui-attach" in codex_args
        assert "--run-detached-ui" in codex_args
        assert "--run-managed-live-send" in codex_args
        assert codex_args[codex_args.index("--api-url") + 1] == "http://longhouse.test"
        assert "--agents-token" not in codex_args
        assert codex_env["CODEX_AGENTS_TOKEN"] == "secret-token"
        assert payload["provider"] == "codex"
        assert payload["provider_version"] == "codex 2.0.0"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["normalized"]["provider_version"] == "codex 2.0.0"
        assert payload["normalized"]["source_review"]["status"] == "not_run"
        assert payload["normalized"]["codex"] == {
            "binary_present": True,
            "longhouse_commit_present": True,
        }
        assert payload["normalized"]["canaries"]["binary_identity"]["version"] == "codex 2.0.0"
        fingerprints = payload["normalized"]["canaries"]["raw_fresh_remote"]["protocol_fingerprints"]
        assert "path" not in fingerprints
        assert fingerprints["responses"]["initialize"]["platformFamily"] == "str"
        assert Path(payload["artifacts"]["provider_contract"]).exists()
        assert Path(payload["artifacts"]["operation_evidence"]).exists()
        assert Path(payload["artifacts"]["session_projection"]).exists()


def test_codex_release_proof_redacts_token_from_command_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_SKIP_ARTIFACT": "1"},
            extra_args=["--codex-agents-token", "secret-token"],
        )

        assert result.returncode == 1
        source = _read_json(Path(payload["artifacts"]["source_artifact"]))
        command = source["canaries"]["release_proof"]["command"]
        assert "secret-token" not in json.dumps(command)
        assert "<redacted>" in json.dumps(command)


def test_codex_managed_live_send_uses_distinct_scenario() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path)},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-send",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-managed-live-send-release-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"
        source_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--run-managed-live-send" in source_args


def test_codex_release_proof_can_attach_real_tool_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path)},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-real-tool",
                "--codex-real-tool-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-real-tool-release-proof-v1"
        assert payload["scenario_profile"] == "real-tool"
        assert payload["verdict"] == "green"
        source_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--run-real-tool" in source_args
        assert source_args[source_args.index("--real-tool-timeout-secs") + 1] == "5"
        assert payload["operation_evidence"]["run_once"]["level"] == "live_token"
        assert payload["operation_evidence"]["transcript_binding"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["run_once"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["codex_real_tool_result_shape"]["status"] == "pass"
        assert payload["normalized"]["canaries"]["codex_real_tool_result_shape"]["command_status"] == "completed"


def test_codex_release_proof_blocks_failed_real_tool_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_REAL_TOOL_FAIL": "1"},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-real-tool",
            ],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_codex_real_tool_failed"
        assert payload["operation_evidence"]["run_once"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["codex_real_tool_result_shape"]["failure_code"] == (
            "fake_codex_real_tool_failed"
        )


def test_codex_managed_live_interrupt_uses_distinct_scenario() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "codex-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_ARGS_PATH": str(args_path)},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-interrupt",
                "--codex-live-interrupt-timeout-secs",
                "7",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-managed-live-interrupt-release-proof-v1"
        assert payload["scenario_profile"] == "managed-live-interrupt"
        assert payload["verdict"] == "green"
        source_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--run-managed-live-interrupt" in source_args
        assert source_args[source_args.index("--live-interrupt-timeout-secs") + 1] == "7"
        assert payload["operation_evidence"]["interrupt"]["status"] == "pass"
        assert payload["operation_evidence"]["interrupt"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["interrupt"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["managed_live_interrupt"]["status"] == "pass"
        assert payload["normalized"]["canaries"]["managed_live_interrupt"]["last_turn_status"] == "interrupted"


def test_codex_release_proof_blocks_failed_managed_live_interrupt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"FAKE_CODEX_INTERRUPT_FAIL": "1"},
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-interrupt",
            ],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_codex_interrupt_failed"
        assert payload["operation_evidence"]["interrupt"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["managed_live_interrupt"]["failure_code"] == (
            "fake_codex_interrupt_failed"
        )


def test_codex_managed_live_send_preflight_reports_missing_credentials() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            env={"CODEX_API_URL": "", "CODEX_AGENTS_TOKEN": ""},
            extra_args=[
                "--preflight-only",
                "--codex-run-managed-live-send",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["artifact_kind"] == "provider_release_proof_preflight"
        assert payload["scenario_id"] == "codex-managed-live-send-release-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "provider_release_proof_prerequisites_missing"
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["provider_binary"]["status"] == "pass"
        assert "message" not in checks["provider_binary"]
        assert checks["codex_api_url"]["failure_code"] == "codex_runtime_host_api_url_missing"
        assert checks["codex_agents_token"]["failure_code"] == "codex_runtime_host_agents_token_missing"


def test_codex_managed_live_send_preflight_redacts_credentials() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--preflight-only",
                "--codex-run-managed-live-send",
                "--codex-api-url",
                "http://longhouse.test",
                "--codex-agents-token",
                "secret-token",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert "secret-token" not in json.dumps(payload)
        assert {check["status"] for check in payload["checks"]} == {"pass"}


def test_preflight_reports_missing_provider_binary_as_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")

        result, payload = _run_proof(
            root,
            "opencode",
            extra_args=["--preflight-only"],
        )

        assert result.returncode == 1
        assert payload["artifact_kind"] == "provider_release_proof_preflight"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_binary_not_found"


def test_explicit_scenario_id_overrides_profile_default() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "codex",
            extra_args=[
                "--source-review-status",
                "pass",
                "--codex-run-managed-live-send",
                "--scenario-id",
                "codex-custom-live-proof-v1",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "codex-custom-live-proof-v1"
        assert payload["scenario_profile"] == "managed-live-send"


def test_antigravity_release_proof_can_attach_real_agy_send_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "control-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "antigravity",
            env={"FAKE_CONTROL_ARGS_PATH": str(args_path)},
            extra_args=[
                "--antigravity-run-real-agy-send",
                "--antigravity-print-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        control_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--antigravity-real-agy-send" in control_args
        assert payload["provider"] == "antigravity"
        assert payload["scenario_id"] == "antigravity-real-agy-send-release-proof-v1"
        assert payload["scenario_profile"] == "real-agy-send"
        assert payload["verdict"] == "green"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["send_input"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["antigravity_real_agy_send"]["status"] == "pass"
        assert Path(payload["artifacts"]["antigravity_control_artifact"]).exists()


def test_antigravity_release_proof_blocks_failed_real_agy_send_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "antigravity",
            env={"FAKE_ANTIGRAVITY_CONTROL_FAIL": "1"},
            extra_args=["--antigravity-run-real-agy-send"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_antigravity_send_failed"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["antigravity_real_agy_send"]["failure_code"] == (
            "fake_antigravity_send_failed"
        )


def test_opencode_release_proof_can_attach_real_tool_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "control-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_CONTROL_ARGS_PATH": str(args_path)},
            extra_args=[
                "--opencode-run-real-tool",
                "--opencode-run-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        control_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--opencode-run-real-tool" in control_args
        assert payload["provider"] == "opencode"
        assert payload["scenario_id"] == "opencode-real-tool-release-proof-v1"
        assert payload["scenario_profile"] == "real-tool"
        assert payload["verdict"] == "green"
        assert payload["operation_evidence"]["transcript_binding"]["status"] == "pass"
        assert payload["operation_evidence"]["transcript_binding"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["transcript_binding"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["opencode_real_tool_result_shape"]["status"] == "pass"
        assert Path(payload["artifacts"]["opencode_control_artifact"]).exists()


def test_opencode_release_proof_blocks_failed_real_tool_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "opencode",
            env={"FAKE_OPENCODE_CONTROL_FAIL": "1"},
            extra_args=["--opencode-run-real-tool"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_opencode_tool_failed"
        assert payload["operation_evidence"]["transcript_binding"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["opencode_real_tool_result_shape"]["failure_code"] == (
            "fake_opencode_tool_failed"
        )


def test_claude_release_proof_can_attach_real_print_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        args_path = root / "control-args.json"
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CONTROL_ARGS_PATH": str(args_path)},
            extra_args=[
                "--claude-run-real-print",
                "--claude-print-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        control_args = json.loads(args_path.read_text(encoding="utf-8"))
        assert "--claude-run-real-print" in control_args
        assert payload["provider"] == "claude"
        assert payload["scenario_id"] == "claude-real-print-release-proof-v1"
        assert payload["scenario_profile"] == "real-print"
        assert payload["verdict"] == "green"
        assert payload["operation_evidence"]["live_token_behavior"]["status"] == "pass"
        assert payload["operation_evidence"]["live_token_behavior"]["level"] == "live_token"
        assert payload["normalized"]["operation_evidence"]["live_token_behavior"]["level"] == "live_token"
        assert payload["normalized"]["canaries"]["claude_real_print"]["status"] == "pass"
        assert Path(payload["artifacts"]["claude_control_artifact"]).exists()


def test_claude_release_proof_blocks_failed_real_print_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_REAL_PRINT_FAIL": "1"},
            extra_args=["--claude-run-real-print"],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_claude_real_print_failed"
        assert payload["operation_evidence"]["live_token_behavior"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["claude_real_print"]["failure_code"] == (
            "fake_claude_real_print_failed"
        )


def test_claude_real_print_wrapper_catches_real_control_api_error() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        repo = root / "repo"
        _write_fake_repo(repo)
        _write_fake_claude_real_print_binary(root / "fake-provider", mode="api_error")
        control_path = repo / "scripts" / "qa" / "provider-control-e2e-canary.py"
        control_path.write_text(
            (REPO_ROOT / "scripts" / "qa" / "provider-control-e2e-canary.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        control_path.chmod(0o755)

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--claude-run-real-print",
                "--claude-print-timeout-secs",
                "5",
            ],
        )

        assert result.returncode == 1
        assert payload["scenario_id"] == "claude-real-print-release-proof-v1"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_real_print_api_error"
        assert payload["operation_evidence"]["live_token_behavior"]["status"] == "fail"
        assert payload["normalized"]["canaries"]["claude_real_print"]["failure_code"] == ("claude_real_print_api_error")


def test_claude_machine_live_proof_uses_distinct_scenario_and_operation_evidence() -> None:
    with (
        tempfile.TemporaryDirectory() as temp_dir,
        _fake_claude_machine_live_server() as (
            server,
            requests,
        ),
    ):
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")
        api_url = f"http://127.0.0.1:{server.server_port}"

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--provider-version",
                "Claude Code 2.9.9",
                "--timeout-secs",
                "120",
                "--claude-run-machine-live-proof",
                "--claude-api-url",
                api_url,
                "--claude-device-id",
                "cinder",
            ],
            env={"CLAUDE_AGENTS_TOKEN": "secret-token"},
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "claude-machine-live-release-proof-v1"
        assert payload["scenario_profile"] == "machine-live"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["operation_evidence"]["send_input"]["level"] == "manual_live_token"
        assert payload["operation_evidence"]["transcript_binding"]["level"] == "manual_live_token"
        assert payload["operation_evidence"]["steer_active_turn"]["level"] == "manual_live_token"
        canary = payload["normalized"]["canaries"]["claude_machine_live_proof"]
        assert canary["status"] == "pass"
        assert canary["device_id"] == "cinder"
        assert canary["command_id"] == "cmd_claude_machine_live"
        assert canary["operation_id"] == "op_claude_machine_live"
        assert Path(payload["artifacts"]["claude_machine_live_artifact"]).exists()
        assert "secret-token" not in json.dumps(payload)
        assert [request["method"] for request in requests] == ["POST", "GET"]
        assert requests[0]["path"] == "/api/agents/machines/cinder/provider-live-proof"
        assert requests[0]["token"] == "secret-token"
        assert requests[0]["body"]["run_live_token_contract"] is True
        assert requests[0]["body"]["expected_provider_version"] == "Claude Code 2.9.9"


def test_claude_machine_live_proof_retries_legacy_runtime_host_without_live_token_fields() -> None:
    with (
        tempfile.TemporaryDirectory() as temp_dir,
        _fake_claude_machine_live_server(mode="legacy_extra_forbidden") as (
            server,
            requests,
        ),
    ):
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")
        api_url = f"http://127.0.0.1:{server.server_port}"

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--provider-version",
                "Claude Code 2.9.9",
                "--timeout-secs",
                "120",
                "--claude-run-machine-live-proof",
                "--claude-api-url",
                api_url,
                "--claude-device-id",
                "cinder",
            ],
            env={"CLAUDE_AGENTS_TOKEN": "secret-token"},
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["scenario_id"] == "claude-machine-live-release-proof-v1"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "claude_machine_live_insufficient_coverage"
        assert [request["method"] for request in requests] == ["POST", "POST", "GET"]
        first = requests[0]["body"]
        second = requests[1]["body"]
        assert first["run_live_token_contract"] is True
        assert first["live_token_timeout_secs"] == 120
        assert "run_live_token_contract" not in second
        assert "live_token_timeout_secs" not in second
        assert second["expected_provider_version"] == "Claude Code 2.9.9"
        canary = payload["normalized"]["canaries"]["claude_machine_live_proof"]
        assert canary["status"] == "warn"
        assert canary["failure_code"] == "claude_machine_live_insufficient_coverage"
        assert "send_input" not in payload["operation_evidence"]


def test_claude_machine_live_proof_preflight_reports_missing_credentials() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={
                "CLAUDE_API_URL": "",
                "CLAUDE_AGENTS_TOKEN": "",
                "CLAUDE_DEVICE_ID": "",
            },
            extra_args=[
                "--preflight-only",
                "--claude-run-machine-live-proof",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["artifact_kind"] == "provider_release_proof_preflight"
        assert payload["scenario_id"] == "claude-machine-live-release-proof-v1"
        assert payload["scenario_profile"] == "machine-live"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "provider_release_proof_prerequisites_missing"
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["provider_binary"]["status"] == "pass"
        assert "message" not in checks["provider_binary"]
        assert checks["claude_api_url"]["failure_code"] == "claude_runtime_host_api_url_missing"
        assert checks["claude_agents_token"]["failure_code"] == ("claude_runtime_host_agents_token_missing")
        assert checks["claude_device_id"]["failure_code"] == "claude_runtime_host_device_id_missing"


def test_claude_machine_live_proof_blocks_failed_operation() -> None:
    with (
        tempfile.TemporaryDirectory() as temp_dir,
        _fake_claude_machine_live_server(mode="failed") as (server, requests),
    ):
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")
        api_url = f"http://127.0.0.1:{server.server_port}"

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--provider-version",
                "Claude Code 2.9.9",
                "--claude-run-machine-live-proof",
                "--claude-api-url",
                api_url,
                "--claude-device-id",
                "cinder",
            ],
            env={"CLAUDE_AGENTS_TOKEN": "secret-token"},
        )

        assert result.returncode == 1
        assert payload["scenario_id"] == "claude-machine-live-release-proof-v1"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_machine_live_proof_failed"
        canary = payload["normalized"]["canaries"]["claude_machine_live_proof"]
        assert canary["status"] == "fail"
        assert canary["failure_code"] == "claude_machine_live_proof_failed"
        assert "Machine Agent control channel is offline" in canary["message"]
        assert "secret-token" not in json.dumps(payload)
        assert [request["method"] for request in requests] == ["POST", "GET"]


def test_claude_machine_live_proof_does_not_mask_red_source_canary() -> None:
    with (
        tempfile.TemporaryDirectory() as temp_dir,
        _fake_claude_machine_live_server() as (
            server,
            requests,
        ),
    ):
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")
        api_url = f"http://127.0.0.1:{server.server_port}"

        result, payload = _run_proof(
            root,
            "claude",
            extra_args=[
                "--provider-version",
                "Claude Code 2.9.9",
                "--claude-run-machine-live-proof",
                "--claude-api-url",
                api_url,
                "--claude-device-id",
                "cinder",
            ],
            env={
                "CLAUDE_AGENTS_TOKEN": "secret-token",
                "FAKE_CLAUDE_MISSING_SESSION_ID": "1",
            },
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_command_contract_missing"
        assert payload["normalized"]["canaries"]["claude_machine_live_proof"]["status"] == "pass"
        assert [request["method"] for request in requests] == ["POST", "GET"]


def test_claude_release_proof_normalizes_no_token_contract_shape() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_CHANNELS_UNCONFIRMED": "1"},
        )

        assert result.returncode == 0
        assert payload["provider"] == "claude"
        assert payload["provider_version"] == "Claude Code 2.9.9"
        assert payload["verdict"] == "yellow"
        assert payload["normalized"]["claude"] == {
            "launch_flags_missing": [],
            "launch_flags_failure_code": None,
            "development_channels_status": "warn",
            "development_channels_missing": ["--resume"],
            "development_channels_failure_code": None,
            "development_channels_reason": "claude_development_channels_contract_unconfirmed",
            "detached_pty_status": "pass",
            "detached_pty_failure_code": None,
            "detached_pty_reason": None,
            "detached_pty_platform": "darwin",
        }
        assert payload["normalized"]["canaries"]["channels_shape"]["reason"] == (
            "claude_development_channels_contract_unconfirmed"
        )


def test_claude_release_proof_red_when_session_flag_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_MISSING_SESSION_ID": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_command_contract_missing"
        assert payload["normalized"]["claude"]["launch_flags_missing"] == ["--session-id"]
        assert payload["normalized"]["claude"]["launch_flags_failure_code"] == ("claude_command_contract_missing")
        assert payload["operation_evidence"]["launch_local"]["failure_code"] == ("claude_command_contract_missing")


def test_claude_release_proof_preserves_development_channel_failure_code() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_CHANNELS_MISSING": "1"},
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "claude_development_channels_contract_missing"
        assert payload["normalized"]["claude"]["development_channels_status"] == "fail"
        assert payload["normalized"]["claude"]["development_channels_failure_code"] == (
            "claude_development_channels_contract_missing"
        )
        assert payload["normalized"]["claude"]["development_channels_missing"] == [
            "--dangerously-load-development-channels"
        ]


def test_claude_release_proof_preserves_detached_pty_failure_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_fake_repo(root / "repo")
        (root / "fake-provider").write_text("#!/bin/sh\n", encoding="utf-8")

        result, payload = _run_proof(
            root,
            "claude",
            env={"FAKE_CLAUDE_PTY_MISSING": "1"},
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "claude_detached_pty_unavailable"
        assert payload["normalized"]["claude"]["detached_pty_status"] == "fail"
        assert payload["normalized"]["claude"]["detached_pty_failure_code"] == ("claude_detached_pty_unavailable")
        assert payload["normalized"]["claude"]["detached_pty_platform"] == "darwin"


def main() -> int:
    tests = [
        test_opencode_release_proof_normalizes_source_canary,
        test_release_proof_can_attach_universal_harness_for_all_providers,
        test_release_proof_exposes_universal_session_evidence_for_codex_and_opencode,
        test_release_proof_can_attach_universal_db_ingest_project,
        test_codex_release_proof_can_attach_universal_interrupt_credentials_gap,
        test_codex_release_proof_can_attach_universal_tool_call_result_e2e,
        test_opencode_release_proof_can_attach_real_universal_managed_session_e2e,
        test_claude_release_proof_can_attach_universal_provider_live_contract_e2e,
        test_antigravity_release_proof_can_attach_universal_hook_inbox_e2e,
        test_opencode_release_proof_blocks_on_source_canary_red,
        test_opencode_release_proof_blocks_green_artifact_from_failed_source_canary,
        test_opencode_release_proof_blocks_when_source_artifact_missing,
        test_opencode_release_proof_blocks_when_source_canary_times_out,
        test_codex_release_proof_maps_provider_binary_and_keeps_source_review_honest,
        test_codex_release_proof_redacts_token_from_command_evidence,
        test_codex_managed_live_send_uses_distinct_scenario,
        test_codex_release_proof_can_attach_real_tool_evidence,
        test_codex_release_proof_blocks_failed_real_tool_evidence,
        test_codex_managed_live_interrupt_uses_distinct_scenario,
        test_codex_release_proof_blocks_failed_managed_live_interrupt,
        test_codex_managed_live_send_preflight_reports_missing_credentials,
        test_codex_managed_live_send_preflight_redacts_credentials,
        test_preflight_reports_missing_provider_binary_as_red,
        test_explicit_scenario_id_overrides_profile_default,
        test_antigravity_release_proof_can_attach_real_agy_send_evidence,
        test_antigravity_release_proof_blocks_failed_real_agy_send_evidence,
        test_opencode_release_proof_can_attach_real_tool_evidence,
        test_opencode_release_proof_blocks_failed_real_tool_evidence,
        test_claude_release_proof_can_attach_real_print_evidence,
        test_claude_release_proof_blocks_failed_real_print_evidence,
        test_claude_real_print_wrapper_catches_real_control_api_error,
        test_claude_machine_live_proof_uses_distinct_scenario_and_operation_evidence,
        test_claude_machine_live_proof_retries_legacy_runtime_host_without_live_token_fields,
        test_claude_machine_live_proof_preflight_reports_missing_credentials,
        test_claude_machine_live_proof_blocks_failed_operation,
        test_claude_machine_live_proof_does_not_mask_red_source_canary,
        test_claude_release_proof_normalizes_no_token_contract_shape,
        test_claude_release_proof_red_when_session_flag_missing,
        test_claude_release_proof_preserves_development_channel_failure_code,
        test_claude_release_proof_preserves_detached_pty_failure_context,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
