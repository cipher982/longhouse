from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from zerg.qa import universal_agent_harness as uah

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_universal_smoke_module():
    module_path = REPO_ROOT / "scripts" / "qa" / "provider-release-proof-universal-smoke.py"
    spec = importlib.util.spec_from_file_location("provider_release_proof_universal_smoke", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_exe(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys

if sys.argv[1:] == ["--version"]:
    print({version!r})
    raise SystemExit(0)

print("unexpected args", sys.argv[1:], file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_bins(tmp_path: Path) -> dict[str, Path]:
    return {
        "claude": _fake_claude_provider_live(tmp_path / "bin" / "claude"),
        "codex": _write_exe(tmp_path / "bin" / "codex", "codex-cli 9.9.9"),
        "opencode": _write_exe(tmp_path / "bin" / "opencode", "opencode 9.9.9"),
        "antigravity": _fake_antigravity_provider_live(tmp_path / "bin" / "agy"),
    }


def _fake_longhouse_engine_claude_channel(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import http.server
import json
import os
import signal
import socketserver
import sys
import threading
import urllib.request
import uuid
from pathlib import Path


def arg_value(args, name, default=None):
    if name not in args:
        return default
    index = args.index(name)
    return args[index + 1]


def meta_entries(args):
    values = {}
    index = 0
    while index < len(args):
        if args[index] == "--meta" and index + 1 < len(args):
            raw = args[index + 1]
            if "=" in raw:
                key, value = raw.split("=", 1)
                values[key] = value
            index += 2
            continue
        index += 1
    return values


def state_path(args):
    session_id = arg_value(args, "--session-id")
    state_root = Path(arg_value(args, "--state-root"))
    return state_root / "sessions" / f"{uuid.UUID(session_id)}.json"


def read_state(args):
    return json.loads(state_path(args).read_text(encoding="utf-8"))


def serve(args):
    session_id = arg_value(args, "--session-id")
    provider_session_id = arg_value(args, "--provider-session-id", "provider-session")
    claude_pid = int(arg_value(args, "--claude-pid", "0"))
    token = os.environ.get("LONGHOUSE_CHANNEL_AUTH_TOKEN") or "fake-token"
    stdout_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            expected = f"Bearer {token}"
            got = self.headers.get("Authorization")
            if got != expected and self.headers.get("X-Longhouse-Channel-Token") != token:
                self.send_response(401)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {
                    "content": payload.get("text") or payload.get("content"),
                    "meta": payload.get("meta") or {},
                },
            }
            with stdout_lock:
                sys.stdout.write(json.dumps(notification) + "\n")
                sys.stdout.flush()
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *values):
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = state_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ready": True,
                "session_id": session_id,
                "provider_session_id": provider_session_id,
                "port": server.server_address[1],
                "auth_token": token,
                "claude_pid": claude_pid,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request = json.loads(line)
            if request.get("method") == "initialize":
                with stdout_lock:
                    sys.stdout.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request.get("id"),
                                "result": {
                                    "protocolVersion": "2025-11-25",
                                    "capabilities": {},
                                    "serverInfo": {"name": "fake-longhouse-channel", "version": "0"},
                                },
                            }
                        )
                        + "\n"
                    )
                    sys.stdout.flush()
    finally:
        server.shutdown()
        server.server_close()


def send(args):
    state = read_state(args)
    meta = {"injected_by": "longhouse", "longhouse_session_id": arg_value(args, "--session-id")}
    meta.update(meta_entries(args))
    payload = json.dumps({"text": arg_value(args, "--text"), "meta": meta}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/message",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Longhouse-Channel-Token": state["auth_token"],
        },
    )
    with urllib.request.urlopen(request, timeout=5):
        pass


def interrupt(args):
    state = read_state(args)
    os.kill(int(state["claude_pid"]), signal.SIGINT)


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "claude-channel":
        print("unexpected fake engine args: " + json.dumps(args), file=sys.stderr)
        raise SystemExit(2)
    command = args[1]
    rest = args[2:]
    if command == "serve":
        serve(rest)
    elif command == "send":
        send(rest)
    elif command == "interrupt":
        interrupt(rest)
    else:
        print("unsupported fake engine command: " + command, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_codex_permission_canary(args: dict[str, object]) -> dict[str, object]:
    artifact_path = Path(str(args["artifact"]))
    evidence_root = Path(str(args["evidence_root"]))
    payload: dict[str, object] = {
        "artifact_kind": "provider_release_canary",
        "provider": "codex",
        "provider_version": "codex 9.9.9",
        "verdict": "green",
        "canaries": {
            "fake_app_server": {
                "status": "pass",
                "operation_evidence": {
                    "permission_prompt": {
                        "status": "pass",
                        "level": "hermetic",
                        "source": "fake codex permission prompt canary",
                        "canary": "codex_fake_app_server_permission_approval",
                        "next": "Promote with a live held-permission Codex provider canary.",
                    }
                },
            }
        },
        "operation_evidence": {
            "permission_prompt": {
                "status": "pass",
                "level": "hermetic",
                "source": "fake codex permission prompt canary",
                "canary": "codex_fake_app_server_permission_approval",
                "next": "Promote with a live held-permission Codex provider canary.",
            }
        },
        "evidence_root": str(evidence_root),
        "artifact_path": str(artifact_path),
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _fake_codex_permission_canary_only(original):
    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        if args.get("run_fake_app_server"):
            return _fake_codex_permission_canary(args)
        return original(args)

    return fake_canary


def _proof_verdict_for_status(status: str) -> str:
    if status == "pass":
        return "green"
    if status == "warn":
        return "yellow"
    return "red"


def _proof_failure_for_status(status: str) -> str | None:
    if status == "pass":
        return None
    if status == "warn":
        return "fake_warning"
    return "fake_drift"


def _write_release_proof(
    root: Path,
    name: str,
    *,
    status: str = "pass",
    version: str = "1.2.3",
    provider: str = "opencode",
    scenario_id: str = "opencode-release-proof-v1",
) -> Path:
    proof_dir = root / name
    artifact_dir = proof_dir / "evidence"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_artifact = artifact_dir / "source.json"
    stdout = artifact_dir / "stdout.log"
    stderr = artifact_dir / "stderr.log"
    normalized_contract = artifact_dir / "normalized" / "contract.json"
    provider_contract = artifact_dir / "normalized" / "provider_contract.json"
    operation_evidence_artifact = artifact_dir / "normalized" / "operation_evidence.json"
    session_projection = artifact_dir / "normalized" / "session_projection.json"
    action_matrix = artifact_dir / "normalized" / "action_matrix.json"
    control_surface = artifact_dir / "normalized" / "control_surface.json"
    provider_execution_coverage_matrix = artifact_dir / "normalized" / "provider_execution_coverage_matrix.json"
    action_rows = [
        {
            "action_id": "send_message",
            "category": "control",
            "status": status,
            "support": True,
            "support_reason": "contract.send_input",
            "required_evidence": "hermetic",
            "evidence_level": "live_no_token",
            "proof_scope": "managed_provider_contract",
            "contract_operation": "send_input",
            "canary": "server_contract",
            "failure_code": _proof_failure_for_status(status),
            "raw_artifacts": [f"/tmp/{name}/volatile-action-path.json"],
        },
        {
            "action_id": "old_new_release_diff",
            "category": "release_diff",
            "status": "blocked",
            "support": True,
            "support_reason": "provider_release_proof",
            "required_evidence": "live_no_token",
            "proof_scope": "release_diff_runner",
            "failure_code": "old_new_release_runner_missing",
        },
    ]
    normalized = {
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "verdict": _proof_verdict_for_status(status),
        "failure_code": _proof_failure_for_status(status),
        "canaries": {"server_contract": {"status": status}},
        "operation_evidence": {
            "send_input": {
                "status": status,
                "level": "live_no_token",
                "canary": "server_contract",
            }
        },
    }
    provider_contract_payload = {
        "artifact_kind": "provider_release_proof_provider_contract",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "contract_operations": {
            "send_input": {
                "level": "live_no_token",
                "source": "fake server_contract",
            }
        },
    }
    operation_evidence_payload = {
        "artifact_kind": "provider_release_proof_operation_evidence",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "operation_evidence": normalized["operation_evidence"],
    }
    session_projection_payload = {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "projection": {
            "artifact_kind": "provider_live_session_projection",
            "provider": provider,
            "status": "captured",
            "provider_session_id": f"volatile-{name}-{version}",
            "classification_sidecar_path": f"/tmp/{name}/sidecar.json",
            "checks": {
                "session_create": {
                    "status": "pass",
                    "provider_session_id": f"volatile-{name}-{version}",
                    "elapsed_ms": 7,
                },
                "prompt_async_no_reply_delivery": {
                    "status": status,
                    "message_marker_sha256": f"volatile-{name}",
                    "elapsed_ms": 11,
                },
            },
            "operation_statuses": {
                "send_input": {
                    "status": status,
                    "level": "live_no_token",
                    "canary": "server_contract",
                }
            },
        },
    }
    action_matrix_payload = {
        "artifact_kind": "provider_release_proof_action_matrix",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "action_matrix": {
            "artifact_kind": "provider_release_proof_action_matrix",
            "provider": provider,
            "action_count": len(action_rows),
            "action_ids": [row["action_id"] for row in action_rows],
            "status_counts": {"blocked": 1, status: 1},
            "action_matrix_path": f"/tmp/{name}/volatile-action-matrix.json",
            "raw_inputs_path": f"/tmp/{name}/volatile-action-inputs.json",
            "actions": action_rows,
        },
    }
    control_surface_payload = {
        "artifact_kind": "provider_release_proof_control_surface",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "control_surface": {
            "artifact_kind": "provider_release_proof_control_surface",
            "provider": provider,
            "action_count": 1,
            "action_ids": ["send_message"],
            "status_counts": {status: 1},
            "control_surface_path": f"/tmp/{name}/volatile-control-surface.json",
            "raw_inputs_path": f"/tmp/{name}/volatile-control-inputs.json",
            "actions": [action_rows[0]],
        },
    }
    execution_coverage_rows = [
        {
            "action_id": "send_message",
            "category": "control",
            "contract_operation": "send_input",
            "required_evidence": "hermetic",
            "coverage_kind": "executable_scenario",
            "coverage_status": status,
            "coverage_gap_kind": "passed" if status == "pass" else "unexpected_failure",
            "failure_code": _proof_failure_for_status(status),
            "matrix_status": status,
            "matrix_support": True,
            "matrix_support_reason": "contract.send_input",
            "scenario_ids": ["managed_session_e2e"],
            "scenario_statuses": {"managed_session_e2e": status},
            "coverage_policy": "scenario_or_matrix",
        },
        {
            "action_id": "old_new_release_diff",
            "category": "release_diff",
            "required_evidence": "live_no_token",
            "coverage_kind": "matrix_contract",
            "coverage_status": "blocked",
            "coverage_gap_kind": "missing_coverage",
            "failure_code": "old_new_release_runner_missing",
            "matrix_status": "blocked",
            "matrix_failure_code": "old_new_release_runner_missing",
            "matrix_support": True,
            "matrix_support_reason": "provider_release_proof",
            "coverage_policy": "matrix_only",
        },
    ]
    provider_execution_coverage_matrix_payload = {
        "artifact_kind": "provider_release_proof_provider_execution_coverage_matrix",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "provider_execution_coverage_matrix": {
            "artifact_kind": "provider_release_proof_provider_execution_coverage_matrix",
            "provider": provider,
            "action_count": len(execution_coverage_rows),
            "coverage_status_counts": {"blocked": 1, status: 1},
            "coverage_kind_counts": {
                "executable_scenario": 1,
                "matrix_contract": 1,
            },
            "coverage_gap_kind_counts": {
                "missing_coverage": 1,
                "passed" if status == "pass" else "unexpected_failure": 1,
            },
            "required_evidence_rollup": {
                "hermetic": {
                    "cell_count": 1,
                    "pass_count": 1 if status == "pass" else 0,
                    "pass_percent": 100.0 if status == "pass" else 0.0,
                    "coverage_status_counts": {status: 1},
                    "coverage_kind_counts": {"executable_scenario": 1},
                    "coverage_gap_kind_counts": {"passed" if status == "pass" else "unexpected_failure": 1},
                },
                "live_no_token": {
                    "cell_count": 1,
                    "pass_count": 0,
                    "pass_percent": 0.0,
                    "coverage_status_counts": {"blocked": 1},
                    "coverage_kind_counts": {"matrix_contract": 1},
                    "coverage_gap_kind_counts": {"missing_coverage": 1},
                },
            },
            "execution_coverage_matrix_path": f"/tmp/{name}/volatile-execution-coverage.json",
            "actions": execution_coverage_rows,
        },
    }
    source_artifact.write_text(json.dumps({"raw": True}), encoding="utf-8")
    stdout.write_text("stdout\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    normalized_contract.parent.mkdir(parents=True, exist_ok=True)
    normalized_contract.write_text(json.dumps(normalized), encoding="utf-8")
    provider_contract.write_text(json.dumps(provider_contract_payload), encoding="utf-8")
    operation_evidence_artifact.write_text(json.dumps(operation_evidence_payload), encoding="utf-8")
    session_projection.write_text(json.dumps(session_projection_payload), encoding="utf-8")
    action_matrix.write_text(json.dumps(action_matrix_payload), encoding="utf-8")
    control_surface.write_text(json.dumps(control_surface_payload), encoding="utf-8")
    provider_execution_coverage_matrix.write_text(
        json.dumps(provider_execution_coverage_matrix_payload), encoding="utf-8"
    )
    proof = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "scenario_id": scenario_id,
        "scenario_version": 1,
        "verdict": _proof_verdict_for_status(status),
        "failure_code": _proof_failure_for_status(status),
        "normalized": normalized,
        "artifacts": {
            "source_artifact": str(source_artifact),
            "stdout": str(stdout),
            "stderr": str(stderr),
            "normalized_contract": str(normalized_contract),
            "provider_contract": str(provider_contract),
            "operation_evidence": str(operation_evidence_artifact),
            "session_projection": str(session_projection),
            "action_matrix": str(action_matrix),
            "control_surface": str(control_surface),
            "provider_execution_coverage_matrix": str(provider_execution_coverage_matrix),
        },
    }
    proof_path = proof_dir / "proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    return proof_path


EXPECTED_ADAPTER_CLASS_BY_PROVIDER = {
    "claude": "ClaudeCodeHarnessAdapter",
    "codex": "CodexOpenAIHarnessAdapter",
    "opencode": "OpenCodeHarnessAdapter",
    "antigravity": "AntigravityHarnessAdapter",
}


def _fake_claude_provider_live(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_antigravity_provider_live(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("agy 9.9.9-fake")
    raise SystemExit(0)

if args == ["--help"]:
    print("--print --prompt-interactive --conversation plugin")
    raise SystemExit(0)

if args == ["plugin", "--help"]:
    print("install <target>")
    print("list")
    print("validate")
    raise SystemExit(0)

if len(args) == 3 and args[:2] == ["plugin", "validate"]:
    plugin_root = Path(args[2])
    plugin_json = plugin_root / "plugin.json"
    if not plugin_json.is_file():
        print("plugin.json missing", file=sys.stderr)
        raise SystemExit(1)
    payload = json.loads(plugin_json.read_text())
    if payload.get("name") != "longhouse-runtime":
        print("unexpected plugin name", file=sys.stderr)
        raise SystemExit(1)
    print("valid longhouse-runtime")
    raise SystemExit(0)

if len(args) == 3 and args[:2] == ["plugin", "install"]:
    plugin_root = Path(args[2])
    if not (plugin_root / "plugin.json").is_file():
        print("plugin.json missing", file=sys.stderr)
        raise SystemExit(1)
    print("installed longhouse-runtime")
    raise SystemExit(0)

if args == ["plugin", "list"]:
    print("longhouse-runtime")
    raise SystemExit(0)

print("unexpected fake agy args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_opencode_server(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
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
    print("opencode 9.9.9-e2e-fake")
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
provider_session_id = "ses_fake_universal_e2e"
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
            if os.environ.get("FAKE_OPENCODE_DROP_PROMPT_ASYNC") != "1":
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
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_adapter_registry_loads_all_four_provider_mvp_adapters(tmp_path: Path) -> None:
    registry = uah.adapter_registry(_fake_bins(tmp_path))

    assert tuple(registry) == uah.SUPPORTED_PROVIDERS
    for provider, adapter in registry.items():
        assert adapter.config.provider == provider
        assert set(uah.MVP_METHODS).issubset(set(adapter.config.methods))
        assert set(uah.MVP_CAPABILITIES).issubset(set(adapter.config.capabilities))


def test_probe_identity_runs_for_all_providers_through_shared_scenario(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    assert all(result["scenario"] == "probe_identity" for result in payload["results"])
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        probe = json.loads((Path(result["evidence_root"]) / "assertions" / "probe.json").read_text(encoding="utf-8"))
        assert probe["declared_capabilities"]
        assert probe["mvp_methods"] == list(uah.MVP_METHODS)
        assert probe["version"]


def test_adapter_registry_uses_concrete_provider_adapters(tmp_path: Path) -> None:
    registry = uah.adapter_registry(_fake_bins(tmp_path))

    assert set(registry) == set(uah.SUPPORTED_PROVIDERS)
    for provider, expected_class in EXPECTED_ADAPTER_CLASS_BY_PROVIDER.items():
        adapter = registry[provider]
        assert type(adapter).__name__ == expected_class
        assert adapter.config.provider == provider
        assert "action_result" in adapter.config.methods


def test_adapter_conformance_runs_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("adapter_conformance",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "adapter_conformance"
        assert result["status"] == "pass"
        assert result["data"]["action_ids"] == list(uah.ACTIONS)
        assert result["data"]["scenario_ids"] == list(uah.SCENARIOS)
        assert result["data"]["method_count"] == len(uah.MVP_METHODS)
        assert result["data"]["failures"] == {
            "missing_declared_methods": [],
            "extra_declared_methods": [],
            "missing_callable_methods": [],
            "wrong_adapter_class": False,
            "unmapped_actions": [],
            "extra_action_mappings": [],
            "mapped_unknown_scenarios": [],
            "missing_scenario_runners": [],
            "extra_scenario_runners": [],
        }
        assert all(row["declared"] and row["callable"] for row in result["data"]["methods"])
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "assertions" / "adapter-conformance.json").is_file()


def test_action_matrix_emits_same_longhouse_actions_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("action_matrix",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "action_matrix"
        assert result["status"] == "blocked"
        assert result["data"]["action_ids"] == list(uah.ACTIONS)
        assert result["data"]["action_count"] == len(uah.ACTIONS)
        actions = {row["action_id"]: row for row in result["data"]["actions"]}
        assert set(actions) == set(uah.ACTIONS)
        expected_class = EXPECTED_ADAPTER_CLASS_BY_PROVIDER[result["provider"]]
        assert {row["adapter_class"] for row in actions.values()} == {expected_class}
        assert {row["adapter_method"] for row in actions.values()} == {"action_result"}
        assert all(row["implementation_kind"] for row in actions.values())
        assert actions["send_message"]["category"] == "control"
        assert actions["steer_active_turn"]["category"] == "control"
        assert actions["pause_request_detect"]["category"] == "observe"
        assert actions["answer_pause_request"]["category"] == "control"
        assert actions["multi_turn_continuity"]["category"] == "control"
        if result["provider"] == "codex":
            assert actions["permission_prompt"]["status"] == "pass"
            assert actions["permission_prompt"]["canary"] == "codex_fake_app_server_permission_approval"
        elif result["provider"] == "opencode":
            assert actions["permission_prompt"]["status"] == "pass"
            assert actions["permission_prompt"]["canary"] == "opencode_bridge_permission_reply"
        elif result["provider"] == "antigravity":
            assert actions["permission_prompt"]["status"] == "unsupported_gap"
            assert actions["permission_prompt"]["failure_code"] == "permission_prompt_unsupported"
        else:
            assert actions["permission_prompt"]["status"] == "pass"
            assert actions["permission_prompt"]["canary"] == "claude_permission_gate_reply"
        assert actions["crash_timeout_cleanup"]["category"] == "resilience"
        assert actions["crash_timeout_cleanup"]["status"] == "pass"
        assert actions["interrupt_cancel"]["contract_operation"] == "interrupt"
        assert actions["raw_evidence_capture"]["status"] == "pass"
        assert actions["parse_normalize"]["status"] == "pass"
        assert actions["db_ingest"]["status"] == "pass"
        assert actions["db_ingest"]["canary"] == "universal_db_ingest_project"
        assert actions["baseline_compare"]["status"] == "pass"
        assert actions["baseline_compare"]["canary"] == "provider_release_proof_baseline_diff"
        assert actions["old_new_release_diff"]["status"] == "pass"
        assert actions["old_new_release_diff"]["evidence_level"] == "artifact_diff"
        assert actions["old_new_release_diff"]["canary"] == "provider_release_proof_old_new_diff"
        assert Path(result["data"]["action_matrix_path"]).is_file()


def test_action_matrix_marks_provider_specific_unsupported_actions(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "opencode", "antigravity"),
            scenarios=("action_matrix",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": bins["claude"], "opencode": bins["opencode"], "antigravity": bins["antigravity"]},
        )
    )

    by_provider = {
        result["provider"]: {row["action_id"]: row for row in result["data"]["actions"]}
        for result in payload["results"]
    }
    assert by_provider["claude"]["external_event_channel"]["status"] == "pass"
    assert by_provider["claude"]["external_event_channel"]["canary"] == "claude_development_channels_contract"
    assert by_provider["opencode"]["steer_active_turn"]["status"] == "unsupported_gap"
    assert by_provider["opencode"]["answer_pause_request"]["status"] == "unsupported_gap"
    assert by_provider["opencode"]["external_event_channel"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["launch_remote"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["interrupt_cancel"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["external_event_channel"]["status"] == "pass"
    assert by_provider["antigravity"]["send_message"]["status"] == "pass"
    assert by_provider["antigravity"]["send_message"]["evidence_level"] == "live_token"


def test_old_new_release_diff_blocks_without_explicit_artifacts(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("old_new_release_diff",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "old_new_release_diff"
        assert result["status"] == "blocked"
        assert result["failure_code"] == "old_new_proof_artifacts_required"
        operation = result["data"]["operation_evidence"]["old_new_release_diff"]
        assert operation["status"] == "blocked"
        assert operation["level"] == "artifact_diff"
        assert operation["canary"] == "provider_release_proof_old_new_diff"


def test_baseline_compare_executes_release_proof_diff_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("baseline_compare",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
            baseline_root=tmp_path / "baselines",
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "baseline_compare"
        assert result["status"] == "pass"
        data = result["data"]
        assert data["provider_release_proof_diff_verdict"] == "green"
        assert data["diff"]["status"] == "match"
        assert Path(data["baseline_proof_path"]).is_file()
        assert Path(data["candidate_proof_path"]).is_file()
        assert Path(data["baseline_compare_artifact_path"]).is_file()
        assert Path(data["raw_command_path"]).is_file()
        operation = data["operation_evidence"]["baseline_compare"]
        assert operation["status"] == "pass"
        assert operation["level"] == "artifact_diff"
        assert operation["canary"] == "provider_release_proof_baseline_diff"


def test_baseline_compare_uses_resolvable_artifacts_with_relative_evidence_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("baseline_compare",),
            evidence_root=Path("relative-evidence"),
            provider_bins={"claude": bins["claude"]},
        )
    )

    result = payload["results"][0]
    data = result["data"]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    diff = json.loads(Path(data["baseline_compare_artifact_path"]).read_text(encoding="utf-8"))
    assert diff["verdict"] == "green"
    assert "artifact_errors" not in diff["diff"]


def test_old_new_release_diff_compares_explicit_proof_artifacts(tmp_path: Path) -> None:
    old = _write_release_proof(tmp_path, "old")
    new = _write_release_proof(tmp_path, "new", version="1.2.4")

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("old_new_release_diff",),
            evidence_root=tmp_path / "evidence",
            old_proof_path=old,
            new_proof_path=new,
            baseline_root=tmp_path / "baselines",
        )
    )

    result = payload["results"][0]
    data = result["data"]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert "failure_code" not in result
    assert data["provider_release_proof_old_new_verdict"] == "green"
    assert data["diff"]["status"] == "match"
    assert data["staging"]["status"] == "explicit_proof_artifacts"
    assert Path(data["old_new_diff_artifact_path"]).is_file()
    assert Path(data["raw_command_path"]).is_file()
    operation = data["operation_evidence"]["old_new_release_diff"]
    assert operation["status"] == "pass"
    assert operation["level"] == "artifact_diff"


@pytest.mark.timeout(30)
def test_full_action_suite_uses_provider_scoped_old_new_artifacts(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    monkeypatch.setattr(
        codex_provider_release_canary,
        "run_codex_provider_release_canary",
        _fake_codex_permission_canary_only(codex_provider_release_canary.run_codex_provider_release_canary),
    )
    monkeypatch.setenv(
        "LONGHOUSE_ENGINE_BIN",
        str(_fake_longhouse_engine_claude_channel(tmp_path / "bin" / "longhouse-engine")),
    )
    providers = ("claude", "codex")
    old_paths = {
        provider: _write_release_proof(
            tmp_path,
            f"{provider}-old",
            provider=provider,
            scenario_id=f"{provider}-release-proof-v1",
        )
        for provider in providers
    }
    new_paths = {
        provider: _write_release_proof(
            tmp_path,
            f"{provider}-new",
            version="1.2.4",
            provider=provider,
            scenario_id=f"{provider}-release-proof-v1",
        )
        for provider in providers
    }

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=providers,
            scenarios=("full_action_suite",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
            old_proof_paths=old_paths,
            new_proof_paths=new_paths,
            baseline_root=tmp_path / "baselines",
        )
    )

    assert payload["verdict"] == "yellow"
    execution_rows = {row["action_id"]: row for row in payload["provider_execution_coverage_matrix"]["actions"]}
    assert execution_rows["old_new_release_diff"]["coverage_status_counts"] == {"pass": len(providers)}
    for result in payload["results"]:
        suite_path = Path(result["data"]["full_action_suite_path"])
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
        old_new_result = next(row for row in suite["results"] if row["scenario"] == "old_new_release_diff")
        assert old_new_result["status"] == "pass"
        assert old_new_result["data"]["old_proof_uri"] == str(old_paths[result["provider"]].resolve())
        coverage = {row["action_id"]: row for row in suite["actions"]}
        assert coverage["old_new_release_diff"]["coverage_status"] == "pass"


def test_old_new_release_diff_fails_on_proof_artifact_drift(tmp_path: Path) -> None:
    old = _write_release_proof(tmp_path, "old")
    new = _write_release_proof(tmp_path, "new", version="1.2.4")
    new_payload = json.loads(new.read_text(encoding="utf-8"))
    action_matrix_path = Path(new_payload["artifacts"]["action_matrix"])
    action_matrix = json.loads(action_matrix_path.read_text(encoding="utf-8"))
    action_matrix["action_matrix"]["actions"][0]["status"] = "fail"
    action_matrix["action_matrix"]["actions"][0]["failure_code"] = "send_message_regressed"
    action_matrix_path.write_text(json.dumps(action_matrix), encoding="utf-8")

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("old_new_release_diff",),
            evidence_root=tmp_path / "evidence",
            old_proof_path=old,
            new_proof_path=new,
            baseline_root=tmp_path / "baselines",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "provider_release_proof_drift"
    assert result["data"]["provider_release_proof_old_new_verdict"] == "red"
    assert result["data"]["diff"]["status"] == "different"
    operation = result["data"]["operation_evidence"]["old_new_release_diff"]
    assert operation["status"] == "fail"
    assert operation["failure_code"] == "provider_release_proof_drift"


def test_launch_remote_projection_runs_for_supported_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("launch_remote_projection",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    by_provider = {result["provider"]: result for result in payload["results"]}
    for provider in ("claude", "codex", "opencode"):
        result = by_provider[provider]
        assert result["status"] == "pass"
        assert result["data"]["operation_evidence"]["launch_remote"]["status"] == "pass"
        assert result["data"]["projections"]["dispatched"]["state"] == "launching_unknown"
        assert result["data"]["projections"]["adopted"]["state"] == "live"
        assert result["data"]["projections"]["failed"]["error_code"] == "provider_launch_failed"
        assert Path(result["evidence_root"], "longhouse", "remote-launch-projection.json").is_file()
    antigravity = by_provider["antigravity"]
    assert antigravity["status"] == "unsupported_gap"
    assert antigravity["failure_code"] == "launch_remote_unsupported"
    assert antigravity["data"]["operation_evidence"]["launch_remote"]["status"] == "unsupported_gap"


def test_control_surface_emits_same_control_actions_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("control_surface",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "control_surface"
        assert result["status"] == "blocked"
        assert result["data"]["action_ids"] == list(uah.CONTROL_SURFACE_ACTION_IDS)
        assert result["data"]["action_count"] == len(uah.CONTROL_SURFACE_ACTION_IDS)
        assert Path(result["data"]["control_surface_path"]).is_file()
        actions = {row["action_id"]: row for row in result["data"]["actions"]}
        assert set(actions) == set(uah.CONTROL_SURFACE_ACTION_IDS)
        assert actions["send_message"]["category"] == "control"
        assert actions["tail_output"]["category"] == "observe"
        assert actions["tool_call_result"]["status"] == "pass"
        assert "baseline_compare" not in actions
        assert "db_ingest" not in actions


def test_control_surface_keeps_unsupported_and_live_token_rows_explicit(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode", "antigravity"),
            scenarios=("control_surface",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": bins["opencode"], "antigravity": bins["antigravity"]},
        )
    )

    by_provider = {
        result["provider"]: {row["action_id"]: row for row in result["data"]["actions"]}
        for result in payload["results"]
    }
    assert by_provider["opencode"]["steer_active_turn"]["status"] == "unsupported_gap"
    assert by_provider["opencode"]["resume_reattach"]["status"] == "pass"
    assert by_provider["opencode"]["resume_reattach"]["evidence_level"] == "live_no_token"
    assert by_provider["antigravity"]["interrupt_cancel"]["status"] == "unsupported_gap"
    assert by_provider["antigravity"]["send_message"]["status"] == "pass"
    assert by_provider["antigravity"]["send_message"]["required_evidence"] == "hermetic"
    assert by_provider["antigravity"]["send_message"]["evidence_level"] == "live_token"


@pytest.mark.timeout(60)
def test_full_action_suite_runs_same_abstract_surface_for_all_providers(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    monkeypatch.setattr(
        codex_provider_release_canary,
        "run_codex_provider_release_canary",
        _fake_codex_permission_canary_only(codex_provider_release_canary.run_codex_provider_release_canary),
    )
    monkeypatch.setenv(
        "LONGHOUSE_ENGINE_BIN",
        str(_fake_longhouse_engine_claude_channel(tmp_path / "bin" / "longhouse-engine")),
    )
    bins = _fake_bins(tmp_path)
    bins["opencode"] = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("full_action_suite",),
            evidence_root=tmp_path / "evidence",
            provider_bins=bins,
            prompt="ping",
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    assert Path(payload["provider_execution_coverage_matrix_path"]).is_file()
    execution_matrix = payload["provider_execution_coverage_matrix"]
    assert execution_matrix["artifact_kind"] == "universal_agent_harness_provider_execution_coverage_matrix"
    assert execution_matrix["providers"] == list(uah.SUPPORTED_PROVIDERS)
    assert execution_matrix["action_count"] == len(uah.ACTIONS)
    assert execution_matrix["captured_provider_count"] == len(uah.SUPPORTED_PROVIDERS)
    assert execution_matrix["missing_provider_actions"] == []
    execution_rows = {row["action_id"]: row for row in execution_matrix["actions"]}
    assert set(execution_rows) == set(uah.ACTIONS)
    assert execution_rows["send_message"]["providers"]["claude"]["coverage_kind"] == "executable_scenario"
    assert execution_rows["send_message"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["send_message"]["providers"]["claude"]["coverage_policy"] == "any_mapped_scenario"
    assert execution_rows["send_message"]["providers"]["claude"]["scenario_ids"] == [
        "send_receive",
        "interrupt_cancel",
        "managed_session_e2e",
    ]
    assert execution_rows["send_message"]["providers"]["claude"]["scenario_statuses"] == {
        "send_receive": "unsupported_gap",
        "interrupt_cancel": "pass",
        "managed_session_e2e": "pass",
    }
    assert execution_rows["send_message"]["providers"]["claude"]["scenario_failure_codes"] == {
        "send_receive": "send_receive_not_safe_no_token",
    }
    assert execution_rows["send_message"]["providers"]["codex"]["scenario_ids"] == [
        "send_receive",
        "interrupt_cancel",
        "managed_session_e2e",
    ]
    assert execution_rows["session_identity"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["session_identity"]["providers"]["claude"]["coverage_policy"] == "any_mapped_scenario"
    assert execution_rows["session_identity"]["providers"]["claude"]["scenario_ids"] == [
        "launch_managed_session",
        "resume_reattach",
        "managed_session_e2e",
    ]
    assert execution_rows["session_identity"]["providers"]["claude"]["scenario_statuses"] == {
        "launch_managed_session": "pass",
        "resume_reattach": "pass",
        "managed_session_e2e": "pass",
    }
    assert execution_rows["session_identity"]["providers"]["claude"]["scenario_failure_codes"] == {}
    assert execution_rows["session_identity"]["providers"]["codex"]["coverage_status"] == "pass"
    assert execution_rows["session_identity"]["providers"]["codex"]["scenario_statuses"] == {
        "launch_managed_session": "pass",
        "resume_reattach": "pass",
        "managed_session_e2e": "unsupported_gap",
    }
    assert execution_rows["session_identity"]["providers"]["antigravity"]["coverage_status"] == "pass"
    assert execution_rows["send_message"]["providers"]["antigravity"]["coverage_status"] == "pass"
    assert execution_rows["resume_reattach"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["resume_reattach"]["providers"]["codex"]["coverage_status"] == "pass"
    assert execution_rows["launch_remote"]["providers"]["claude"]["coverage_kind"] == "executable_scenario"
    assert execution_rows["launch_remote"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["launch_remote"]["providers"]["claude"]["scenario_ids"] == ["launch_remote_projection"]
    assert execution_rows["launch_remote"]["providers"]["antigravity"]["coverage_status"] == "unsupported_gap"
    assert (
        execution_rows["launch_remote"]["providers"]["antigravity"]["coverage_gap_kind"]
        == "provider_contract_unsupported"
    )
    assert execution_rows["run_once"]["coverage_gap_kind_counts"] == {
        "no_token_safety_gate": 3,
        "passed": 1,
    }
    assert execution_rows["baseline_compare"]["providers"]["opencode"]["coverage_status"] == "pass"
    assert execution_rows["tool_call_result"]["providers"]["antigravity"]["coverage_kind"] == "executable_scenario"
    assert execution_rows["tool_call_result"]["providers"]["antigravity"]["coverage_status"] == "pass"
    assert execution_rows["tool_call_result"]["providers"]["antigravity"]["scenario_ids"] == [
        "tool_call_result_projection"
    ]
    assert execution_rows["permission_prompt"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["permission_prompt"]["providers"]["claude"]["coverage_gap_kind"] == "passed"
    assert execution_rows["permission_prompt"]["providers"]["codex"]["coverage_status"] == "pass"
    assert execution_rows["permission_prompt"]["providers"]["codex"]["coverage_gap_kind"] == "passed"
    assert execution_rows["permission_prompt"]["providers"]["opencode"]["coverage_status"] == "pass"
    assert execution_rows["permission_prompt"]["providers"]["opencode"]["scenario_ids"] == ["permission_prompt"]
    assert execution_rows["answer_pause_request"]["providers"]["claude"]["coverage_status"] == "pass"
    assert execution_rows["answer_pause_request"]["providers"]["codex"]["coverage_status"] == "pass"
    assert execution_matrix["provider_coverage_kind_counts"]["claude"]["executable_scenario"] > execution_matrix[
        "provider_coverage_kind_counts"
    ]["claude"].get("matrix_contract", 0)
    assert execution_matrix["provider_coverage_gap_kind_counts"]["claude"] == {
        "missing_coverage": 1,
        "no_token_safety_gate": 1,
        "passed": len(uah.ACTIONS) - 2,
    }
    for result in payload["results"]:
        assert result["scenario"] == "full_action_suite"
        assert result["status"] == "blocked"
        assert result["failure_code"] == "full_action_suite_has_explicit_gaps"
        assert result["data"]["action_ids"] == list(uah.ACTIONS)
        assert result["data"]["action_count"] == len(uah.ACTIONS)
        assert result["data"]["missing_actions"] == []
        assert "action_matrix" in result["data"]["scenario_ids"]
        assert "adapter_conformance" in result["data"]["scenario_ids"]
        assert "launch_remote_projection" in result["data"]["scenario_ids"]
        assert "interrupt_cancel" in result["data"]["scenario_ids"]
        assert "tool_call_result_projection" in result["data"]["scenario_ids"]
        assert "answer_pause_request" in result["data"]["scenario_ids"]
        assert "baseline_compare" in result["data"]["scenario_ids"]
        assert "old_new_release_diff" in result["data"]["scenario_ids"]

        evidence_root = Path(result["evidence_root"])
        suite = json.loads((evidence_root / "assertions" / "full-action-suite.json").read_text(encoding="utf-8"))
        assert suite["action_ids"] == list(uah.ACTIONS)
        assert suite["missing_actions"] == []
        assert len(suite["results"]) == 1 + len(uah.FULL_ACTION_SUITE_SCENARIOS)
        coverage = {row["action_id"]: row for row in suite["actions"]}
        assert set(coverage) == set(uah.ACTIONS)
        assert coverage["send_message"]["coverage_kind"] == "executable_scenario"
        assert coverage["send_message"]["scenario_ids"] == ["send_receive", "interrupt_cancel", "managed_session_e2e"]
        assert coverage["session_identity"]["coverage_kind"] == "executable_scenario"
        assert coverage["session_identity"]["scenario_ids"] == [
            "launch_managed_session",
            "resume_reattach",
            "managed_session_e2e",
        ]
        if result["provider"] == "claude":
            assert coverage["send_message"]["coverage_status"] == "pass"
            assert coverage["send_message"]["scenario_statuses"] == {
                "send_receive": "unsupported_gap",
                "interrupt_cancel": "pass",
                "managed_session_e2e": "pass",
            }
            assert coverage["send_message"]["scenario_failure_codes"] == {
                "send_receive": "send_receive_not_safe_no_token",
            }
            assert coverage["session_identity"]["coverage_status"] == "pass"
            assert coverage["session_identity"]["scenario_failure_codes"] == {}
        if result["provider"] == "codex":
            assert coverage["session_identity"]["coverage_status"] == "pass"
            assert coverage["session_identity"]["scenario_failure_codes"] == {
                "managed_session_e2e": "codex_managed_bridge_credentials_missing",
            }
            assert coverage["interrupt_cancel"]["coverage_status"] == "pass"
            assert coverage["interrupt_cancel"]["scenario_failure_codes"] == {}
        if result["provider"] == "antigravity":
            assert coverage["send_message"]["coverage_status"] == "pass"
            assert coverage["session_identity"]["coverage_status"] == "pass"
        assert coverage["launch_remote"]["coverage_kind"] == "executable_scenario"
        assert coverage["launch_remote"]["scenario_ids"] == ["launch_remote_projection"]
        assert coverage["pause_request_detect"]["coverage_status"] == "pass"
        if result["provider"] in {"codex", "opencode", "claude"}:
            assert coverage["permission_prompt"]["coverage_status"] == "pass"
        else:
            assert coverage["permission_prompt"]["coverage_status"] == "unsupported_gap"
        assert coverage["tool_call_result"]["coverage_kind"] == "executable_scenario"
        assert coverage["tool_call_result"]["coverage_status"] == "pass"
        assert coverage["tool_call_result"]["scenario_ids"] == ["tool_call_result_projection"]
        assert coverage["baseline_compare"]["coverage_kind"] == "executable_scenario"
        assert coverage["baseline_compare"]["coverage_status"] == "pass"
        assert coverage["baseline_compare"]["scenario_ids"] == ["baseline_compare"]
        assert coverage["old_new_release_diff"]["coverage_status"] == "blocked"


def test_db_ingest_project_uses_real_longhouse_sqlite_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("db_ingest_project",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"
        evidence_root = Path(result["evidence_root"])
        db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
        assert Path(db_snapshot["db_path"]).is_file()
        assert db_snapshot["ingest_result"]["events_inserted"] == 4
        assert db_snapshot["session_counts"]["user_messages"] == 1
        assert db_snapshot["session_counts"]["assistant_messages"] == 1
        assert db_snapshot["session_counts"]["tool_calls"] == 1
        assert db_snapshot["timeline"]["matched"] is True
        assert "universal db ingest hello" in db_snapshot["export_jsonl"]


def test_opencode_lineage_projection_uses_real_longhouse_sqlite(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("opencode_lineage_projection",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": _fake_bins(tmp_path)["opencode"]},
        )
    )

    assert payload["verdict"] == "green"
    result = payload["results"][0]
    assert result["provider"] == "opencode"
    assert result["scenario"] == "opencode_lineage_projection"
    assert result["status"] == "pass"
    assert result["data"]["operation_evidence"]["opencode_lineage_projection"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"
    assert all(result["data"]["assertions"].values())

    evidence_root = Path(result["evidence_root"])
    projection = json.loads(
        (evidence_root / "longhouse" / "opencode-lineage-projection.json").read_text(encoding="utf-8")
    )
    branch_kinds = [row["branch_kind"] for row in projection["thread_rows"]]
    assert branch_kinds.count("subagent") == 2
    assert "fork" in branch_kinds
    edge_kinds = [row["edge_kind"] for row in projection["edge_rows"]]
    assert edge_kinds.count("task_child") == 2
    assert "fork" in edge_kinds
    assert "subagent_id" in {row["alias_kind"] for row in projection["alias_rows"]}
    assert "forked_from_provider_session_id" in {row["alias_kind"] for row in projection["alias_rows"]}


def test_opencode_orchestration_projection_uses_real_longhouse_sqlite(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("opencode_orchestration_projection",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": _fake_bins(tmp_path)["opencode"]},
        )
    )

    assert payload["verdict"] == "green"
    result = payload["results"][0]
    assert result["provider"] == "opencode"
    assert result["scenario"] == "opencode_orchestration_projection"
    assert result["status"] == "pass"
    assert all(result["data"]["assertions"].values())
    assert result["data"]["operation_evidence"]["opencode_nested_subagent_projection"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["opencode_task_id_resume_projection"]["status"] == "pass"
    assert result["data"]["capability_states"]["background_task_status"] == "unknown"
    assert result["data"]["capability_states"]["switch_actor"] == "unknown"
    assert result["data"]["capability_reason_codes"]["background_task_status"] == "provider_background_status_unproven"
    assert result["data"]["capability_reason_codes"]["switch_actor"] == "provider_actor_switch_unmapped"
    assert (
        result["data"]["operation_evidence"]["opencode_rich_gap_manifest"]["background_task_status_reason_code"]
        == "provider_background_status_unproven"
    )
    assert (
        result["data"]["operation_evidence"]["opencode_rich_gap_manifest"]["switch_actor_reason_code"]
        == "provider_actor_switch_unmapped"
    )

    evidence_root = Path(result["evidence_root"])
    projection = json.loads(
        (evidence_root / "longhouse" / "opencode-orchestration-projection.json").read_text(encoding="utf-8")
    )
    edge_rows = projection["edge_rows"]
    assert [row["edge_kind"] for row in edge_rows].count("task_child") == 2
    nested_edges = [row for row in edge_rows if row["provider_edge_id"] == "task_nested"]
    assert len(nested_edges) == 1
    assert nested_edges[0]["source_thread_id"] is not None
    assert nested_edges[0]["source_thread_id"] != nested_edges[0]["target_thread_id"]
    assert "ses_fork" in {row["alias_value"] for row in projection["alias_rows"]}


def test_orchestration_capability_matrix_emits_per_capability_evidence(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("orchestration_capability_matrix",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["scenario"] == "orchestration_capability_matrix"
        assert result["status"] == "pass"
        operation_evidence = result["data"]["operation_evidence"]
        assert "orchestration_observe_transcript" in operation_evidence
        assert "orchestration_background_task_status" in operation_evidence
        assert all("verdict" in item for item in operation_evidence.values())
        assert all("reason_code" in item for item in operation_evidence.values())
        assert all(item["canary"] == "provider_action_coverage" for item in operation_evidence.values())
        assert (
            operation_evidence["orchestration_background_task_status"]["reason_code"]
            == "provider_background_status_unproven"
        )
        background_rows = [
            row for row in result["data"]["capabilities"] if row["capability"] == "background_task_status"
        ]
        assert background_rows[0]["reason_code"] == "provider_background_status_unproven"
        summary = result["data"]["summary"]
        assert summary["green"] + summary["yellow"] + summary["red"] == len(operation_evidence)


def test_projection_scenarios_emit_comparable_artifacts_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("session_projection", "timeline_projection"),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS) * 2
    for result in payload["results"]:
        assert result["status"] == "pass"
        assert result["scenario"] in {"session_projection", "timeline_projection"}
        evidence_root = Path(result["evidence_root"])
        assert Path(result["data"]["session_projection_path"]).is_file()
        assert Path(result["data"]["timeline_projection_path"]).is_file()
        assert Path(result["data"]["canonical_events_path"]).is_file()
        session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
        timeline = json.loads((evidence_root / "longhouse" / "timeline-projection.json").read_text(encoding="utf-8"))
        assert session["provider"] == result["provider"]
        assert session["provider_session_id"].startswith(f"universal-{result['provider']}-{result['scenario']}")
        assert session["has_user"] is True
        assert session["has_assistant"] is True
        assert timeline["event_count"] == 3
        assert timeline["preview_text"] == "universal projection hello"
        if result["scenario"] == "session_projection":
            assert result["data"]["operation_evidence"]["session_projection"]["status"] == "pass"
        else:
            assert result["data"]["operation_evidence"]["timeline_projection"]["status"] == "pass"
        assert result["data"]["operation_evidence"]["transcript_binding"]["status"] == "pass"


def test_codex_run_prompt_once_writes_safe_projection(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert session["operation_statuses"]["run_once"]["status"] == "pass"


def test_unsafe_run_prompt_once_is_typed_unsupported_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("run_prompt_once",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": _fake_bins(tmp_path)["claude"]},
            prompt="hello",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "run_prompt_once_not_safe_no_token"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "input" / "prompt.txt").read_text(encoding="utf-8") == "hello"
    assert (evidence_root / "assertions" / "run_prompt.json").is_file()


def test_managed_session_scenarios_pass_for_codex_and_opencode(tmp_path: Path) -> None:
    bins = _fake_bins(tmp_path)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex", "opencode"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": bins["codex"], "opencode": bins["opencode"]},
            prompt="ping",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == 4
    assert all(result["status"] == "pass" for result in payload["results"])
    for result in payload["results"]:
        evidence_root = Path(result["evidence_root"])
        session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
        assert session["provider"] == result["provider"]
        assert session["provider_session_id"].startswith(f"universal-{result['provider']}-")
        if result["scenario"] == "send_receive":
            assert session["has_user"] is True
            assert session["has_assistant"] is True
            assert session["operation_statuses"]["send_input"]["status"] == "pass"
        else:
            assert session["operation_statuses"]["launch_local"]["level"] == "live_no_token"


def test_claude_launch_managed_session_uses_provider_live_contract_canary(tmp_path: Path) -> None:
    fake_claude = _fake_claude_provider_live(tmp_path / "bin" / "claude")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("launch_managed_session",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "launch_managed_session"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["operation_evidence"]["launch_local"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["launch_local"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "assertions" / "launch_managed_session.json").is_file()
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["canaries"]["command_shape"]["status"] == "pass"
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "command_shape" in raw_events


def test_managed_session_scenarios_keep_remaining_typed_gaps(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "antigravity"),
            scenarios=("launch_managed_session", "send_receive"),
            evidence_root=tmp_path / "evidence",
            provider_bins={
                "claude": _fake_bins(tmp_path)["claude"],
                "antigravity": _fake_bins(tmp_path)["antigravity"],
            },
            prompt="ping",
        )
    )

    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == 4
    by_key = {(result["provider"], result["scenario"]): result for result in payload["results"]}
    expected = {
        ("claude", "launch_managed_session"): ("pass", None),
        ("claude", "send_receive"): ("unsupported_gap", "send_receive_not_safe_no_token"),
        ("antigravity", "launch_managed_session"): ("pass", None),
        ("antigravity", "send_receive"): ("unsupported_gap", "send_receive_not_safe_no_token"),
    }
    for key, (status, failure_code) in expected.items():
        assert by_key[key]["status"] == status
        if failure_code is not None:
            assert by_key[key]["failure_code"] == failure_code

    antigravity_launch = by_key[("antigravity", "launch_managed_session")]
    assert antigravity_launch["data"]["source_artifact_kind"] == "provider_live_canary"
    assert antigravity_launch["data"]["operation_evidence"]["launch_local"]["status"] == "pass"
    assert antigravity_launch["data"]["operation_evidence"]["launch_local"]["level"] == "live_no_token"


def test_opencode_managed_session_e2e_uses_real_provider_live_canary(tmp_path: Path) -> None:
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "green"
    assert provider_live["canaries"]["prompt_async_no_reply_delivery"]["status"] == "pass"
    assert (evidence_root / "raw" / "provider-live-evidence" / "opencode-server.log").is_file()
    assert (evidence_root / "raw" / "provider-live-evidence" / "opencode-doc-paths.json").is_file()

    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    canonical_events = (evidence_root / "events" / "canonical-longhouse-events.jsonl").read_text(encoding="utf-8")
    assert "provider_live_canary" in raw_events
    assert '"synthetic": true' not in raw_events
    assert "prompt_async_no_reply_delivery" in canonical_events

    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "ses_fake_universal_e2e"
    assert session["longhouse_session_id"]
    assert session["operation_statuses"]["send_input"]["level"] == "live_no_token"
    assert session["operation_statuses"]["transcript_binding"]["canary"] == "opencode_prompt_async_no_reply_delivery"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_opencode_resume_reattach_uses_process_restart_canary(tmp_path: Path) -> None:
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("resume_reattach",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["reattach"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["reattach"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["canaries"]["process_restart_reattach_contract"]["status"] == "pass"
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "process_restart_reattach_contract" in raw_events
    assert "provider_live_canary" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["operation_statuses"]["reattach"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_resume_reattach_uses_provider_release_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_tui_attach": {
                    "status": "pass",
                    "thread_id": "thread_codex_resume_reattach",
                    "state_file": "/tmp/codex-state.json",
                },
                "detached_ui": {
                    "status": "pass",
                    "thread_id": "thread_codex_resume_reattach",
                    "ipc_socket": "/tmp/codex-state.sock",
                },
            },
            "operation_evidence": {
                "launch_local": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
                "launch_remote": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "detached_ui",
                },
                "reattach": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    fake_codex = _fake_bins(tmp_path)["codex"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("resume_reattach",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": fake_codex},
        )
    )

    assert calls
    assert calls[0]["codex_bin"] == str(fake_codex)
    assert calls[0]["run_managed_tui_attach"] is True
    assert calls[0]["run_detached_ui"] is True
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "resume_reattach"
    assert result["data"]["source_artifact_kind"] == "provider_release_canary"
    assert result["data"]["operation_evidence"]["reattach"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["reattach"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "assertions" / "resume_reattach.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "codex_provider_release_canary" in raw_events
    assert "thread_codex_resume_reattach" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "thread_codex_resume_reattach"
    assert session["operation_statuses"]["reattach"]["status"] == "pass"


def test_codex_resume_reattach_falls_back_to_attach_command_when_credentials_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from zerg.qa import codex_provider_release_canary

    def fake_canary(_args: dict[str, object]) -> dict[str, object]:
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "yellow",
            "failure_code": "insufficient_coverage",
            "canaries": {
                "managed_tui_attach": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
                "detached_ui": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
            },
            "operation_evidence": {
                "reattach": {
                    "status": "not_run",
                    "level": "none",
                    "canary": "managed_tui_attach",
                    "failure_code": "managed_bridge_credentials_missing",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("resume_reattach",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "resume_reattach"
    assert result["data"]["missing_live_credentials"] == ["--agents-token", "--api-url"]
    assert result["data"]["operation_evidence"]["reattach"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["reattach"]["level"] == "hermetic"
    assert result["data"]["operation_evidence"]["reattach"]["canary"] == "codex_managed_local_attach_command_shape"
    assert result["data"]["operation_evidence"]["live_reattach_canary"]["status"] == "blocked"
    assert result["data"]["assertions"] == {
        "command_built": True,
        "execs_engine": True,
        "requires_codex": True,
        "requires_longhouse_engine": True,
        "uses_engine_bridge_attach": True,
        "uses_longhouse_session_id": True,
        "uses_zsh_shell": True,
    }
    assert Path(result["data"]["raw_reattach_command_path"]).is_file()


def test_resume_reattach_uses_claude_command_shape_and_keeps_unmigrated_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "antigravity"),
            scenarios=("resume_reattach",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == {"claude", "antigravity"}
    by_provider = {result["provider"]: result for result in payload["results"]}
    claude = by_provider["claude"]
    assert claude["status"] == "pass"
    assert claude["data"]["operation_evidence"]["reattach"]["status"] == "pass"
    assert claude["data"]["operation_evidence"]["reattach"]["level"] == "hermetic"
    assert claude["data"]["operation_evidence"]["reattach"]["canary"] == "claude_channel_resume_command_shape"
    assert claude["data"]["assertions"] == {
        "changes_to_workspace": True,
        "does_not_use_session_id_flag": True,
        "exports_longhouse_session_id": True,
        "exports_provider_session_id": True,
        "loads_development_channel": True,
        "loads_longhouse_channel_server": True,
        "uses_resume_flag": True,
    }
    assert Path(claude["data"]["raw_resume_command_path"]).is_file()
    antigravity = by_provider["antigravity"]
    assert antigravity["status"] == "unsupported_gap"
    assert antigravity["failure_code"] == "resume_reattach_unsupported"
    assert antigravity["data"]["operation_evidence"]["reattach"]["failure_code"] == "resume_reattach_unsupported"


def test_antigravity_interrupt_and_resume_are_explicit_contract_gaps(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("interrupt_cancel", "resume_reattach"),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    by_scenario = {result["scenario"]: result for result in payload["results"]}
    interrupt = by_scenario["interrupt_cancel"]
    assert interrupt["status"] == "unsupported_gap"
    assert interrupt["failure_code"] == "interrupt_cancel_unsupported"
    assert interrupt["data"]["operation_evidence"]["interrupt"]["failure_code"] == "interrupt_cancel_unsupported"

    resume = by_scenario["resume_reattach"]
    assert resume["status"] == "unsupported_gap"
    assert resume["failure_code"] == "resume_reattach_unsupported"
    assert resume["data"]["operation_evidence"]["reattach"]["failure_code"] == "resume_reattach_unsupported"


def test_claude_managed_session_e2e_uses_provider_live_contract_canary(tmp_path: Path) -> None:
    fake_claude = _fake_claude_provider_live(tmp_path / "bin" / "claude")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["launch_local"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["runtime_phase"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["send_input"]["status"] == "blocked"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_token_required"
    assert result["data"]["operation_evidence"]["steer_active_turn"]["status"] == "blocked"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "green"
    assert provider_live["canaries"]["command_shape"]["status"] == "pass"
    assert provider_live["canaries"]["channels_shape"]["status"] == "pass"
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    canonical_events = (evidence_root / "events" / "canonical-longhouse-events.jsonl").read_text(encoding="utf-8")
    assert "provider_live_canary" in raw_events
    assert "channels_shape" in canonical_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider"] == "claude"
    assert session["operation_statuses"]["send_input"]["failure_code"] == "claude_live_token_contract_not_run"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_claude_managed_session_e2e_fails_when_channel_contract_breaks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CHANNELS_MISSING", "1")
    fake_claude = _fake_claude_provider_live(tmp_path / "bin" / "claude")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "claude_development_channels_contract_missing"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "fail"


def test_codex_managed_session_e2e_uses_provider_release_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_tui_attach": {
                    "status": "pass",
                    "thread_id": "thread_codex_universal_e2e",
                    "state_file": "/tmp/codex-state.json",
                },
                "detached_ui": {
                    "status": "pass",
                    "thread_id": "thread_codex_universal_e2e",
                    "ipc_socket": "/tmp/codex-state.sock",
                },
            },
            "operation_evidence": {
                "launch_local": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
                "launch_remote": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "detached_ui",
                },
                "reattach": {
                    "status": "pass",
                    "level": "live_no_token",
                    "canary": "managed_tui_attach",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    fake_codex = _fake_bins(tmp_path)["codex"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": fake_codex},
        )
    )

    assert calls
    assert calls[0]["codex_bin"] == str(fake_codex)
    assert calls[0]["run_managed_tui_attach"] is True
    assert calls[0]["run_detached_ui"] is True
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_release_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["longhouse_ingest"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["launch_local"]["canary"] == "managed_tui_attach"
    assert result["data"]["operation_evidence"]["launch_remote"]["canary"] == "detached_ui"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "raw" / "codex-provider-release-canary.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "codex_provider_release_canary" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "thread_codex_universal_e2e"
    assert session["operation_statuses"]["launch_local"]["level"] == "live_no_token"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_interrupt_cancel_uses_managed_live_interrupt_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "codex_provider_release_canary",
            "provider": "codex",
            "codex_version": "codex-cli 9.9.9",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_live_interrupt": {
                    "status": "pass",
                    "thread_id": "codex-thread-interrupt",
                    "marker": "LONGHOUSE_CODEX_INTERRUPT_CANARY_fake",
                    "last_turn_status": "interrupted",
                    "state_file": str(tmp_path / "state.json"),
                }
            },
            "operation_evidence": {
                "interrupt": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "managed_live_interrupt",
                }
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    assert calls
    assert calls[0]["run_managed_live_interrupt"] is True
    assert calls[0]["source_review_status"] == "pass"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "interrupt_cancel"
    assert result["data"]["operation_evidence"]["interrupt"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "managed_live_interrupt" in raw_events
    assert "interrupted" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "codex-thread-interrupt"
    assert session["operation_statuses"]["interrupt"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_interrupt_cancel_falls_back_to_hermetic_dispatch_when_credentials_missing(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["missing_live_credentials"] == ["--agents-token", "--api-url"]
    assert result["data"]["operation_evidence"]["interrupt"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["interrupt"]["level"] == "hermetic"
    assert result["data"]["operation_evidence"]["interrupt"]["canary"] == "codex_managed_local_interrupt_dispatch"
    assert result["data"]["operation_evidence"]["live_interrupt_canary"]["status"] == "blocked"
    assert result["data"]["assertions"] == {
        "command_dispatched": True,
        "command_type_matches": True,
        "exit_code_zero": True,
        "payload_empty": True,
        "provider_is_codex": True,
        "result_ok": True,
        "transport_is_codex_app_server": True,
    }
    assert Path(result["data"]["raw_interrupt_dispatch_path"]).is_file()


def test_claude_interrupt_cancel_uses_channel_control_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "claude",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "claude": {
                    "status": "pass",
                    "session_id": "claude-channel-control-session",
                    "send_meta": {
                        "injected_by": "longhouse",
                        "longhouse_session_id": "claude-channel-control-session",
                    },
                    "steer_meta": {
                        "injected_by": "longhouse",
                        "intent": "steer",
                        "longhouse_session_id": "claude-channel-control-session",
                    },
                    "interrupt_marker": str(tmp_path / "interrupted.txt"),
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert calls
    assert calls[0]["provider"] == "claude"
    assert calls[0]["extra_args"] is None
    assert calls[0]["extra_env"] is None
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "interrupt_cancel"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["steer_active_turn"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["interrupt"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "claude_channel_control" in raw_events
    assert "steer from provider control canary" in raw_events
    assert "SIGINT" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "claude-channel-control-session"
    assert session["operation_statuses"]["interrupt"]["level"] == "live_no_token"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 3
    assert db_snapshot["timeline"]["matched"] is True


def test_claude_steer_active_turn_uses_channel_control_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "claude",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "claude": {
                    "status": "pass",
                    "session_id": "claude-channel-steer-session",
                    "send_meta": {
                        "injected_by": "longhouse",
                        "longhouse_session_id": "claude-channel-steer-session",
                    },
                    "steer_meta": {
                        "injected_by": "longhouse",
                        "intent": "steer",
                        "longhouse_session_id": "claude-channel-steer-session",
                    },
                    "interrupt_marker": str(tmp_path / "interrupted.txt"),
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("steer_active_turn",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert calls
    assert calls[0]["provider"] == "claude"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "steer_active_turn"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["operation_evidence"]["steer_active_turn"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["steer_active_turn"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "assertions" / "steer_active_turn.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "steer from provider control canary" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "claude-channel-steer-session"
    assert session["operation_statuses"]["steer_active_turn"]["level"] == "live_no_token"


def test_steer_active_turn_reports_explicit_provider_gaps(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex", "opencode", "antigravity"),
            scenarios=("steer_active_turn",),
            evidence_root=tmp_path / "evidence",
        )
    )

    by_provider = {result["provider"]: result for result in payload["results"]}
    assert payload["verdict"] == "yellow"
    codex = by_provider["codex"]
    assert codex["status"] == "pass"
    assert codex["data"]["operation_evidence"]["steer_active_turn"]["status"] == "pass"
    assert codex["data"]["operation_evidence"]["steer_active_turn"]["level"] == "hermetic"
    assert codex["data"]["operation_evidence"]["steer_active_turn"]["canary"] == "codex_managed_local_steer_dispatch"
    assert codex["data"]["assertions"] == {
        "command_dispatched": True,
        "command_type_matches": True,
        "exit_code_zero": True,
        "payload_matches": True,
        "provider_is_codex": True,
        "result_ok": True,
        "transport_is_codex_app_server": True,
    }
    assert Path(codex["data"]["raw_steer_dispatch_path"]).is_file()
    for provider in ("opencode", "antigravity"):
        result = by_provider[provider]
        assert result["status"] == "unsupported_gap"
        assert result["failure_code"] == "steer_active_turn_unsupported"
        assert result["data"]["operation_evidence"]["steer_active_turn"]["status"] == "unsupported_gap"


def test_pause_request_detect_projects_pending_question_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("pause_request_detect",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert {result["provider"] for result in payload["results"]} == set(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        data = result["data"]
        assert data["scenario"] == "pause_request_detect"
        assert data["operation_evidence"]["pause_request_detect"]["status"] == "pass"
        assert data["operation_evidence"]["runtime_phase"]["status"] == "pass"
        assert data["assertions"] == {
            "runtime_phase_needs_user": True,
            "active_pause_request_visible": True,
            "pause_request_pending": True,
            "question_payload_projected": True,
            "can_respond_matches_provider_contract": True,
        }
        assert data["pause_request"]["status"] == "pending"
        assert data["pause_request"]["questions"][0]["id"] == "approach"
        assert data["pause_request"]["can_respond"] is (result["provider"] in {"claude", "codex"})
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "longhouse" / "pause-request-service.json").is_file()
        assert (evidence_root / "longhouse" / "runtime-state.json").is_file()
        assert (evidence_root / "longhouse" / "pause-request-pending.json").is_file()


def test_answer_pause_request_resolves_service_and_dispatches_managed_answer(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "codex"),
            scenarios=("answer_pause_request",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    for result in payload["results"]:
        assert result["status"] == "pass"
        data = result["data"]
        assert data["longhouse_response_service"]["status"] == "pass"
        assert data["managed_answer_dispatch"]["status"] == "pass"
        assert data["managed_answer_dispatch"]["assertions"] == {
            "command_dispatched": True,
            "command_type_matches": True,
            "exit_code_zero": True,
            "payload_matches": True,
            "provider_matches": True,
            "response_data_projected": True,
            "result_ok": True,
            "transport_matches": True,
        }
        assert data["operation_evidence"]["answer_pause_request"]["status"] == "pass"
        assert data["operation_evidence"]["answer_pause_request"]["level"] == "hermetic"
        assert data["operation_evidence"]["live_answer_delivery"]["status"] == "blocked"
        assert data["operation_evidence"]["live_answer_delivery"]["level"] == "live_token_required"
        assert data["operation_evidence"]["longhouse_pause_response_service"]["status"] == "pass"
        assert data["resolved_pause_request"]["status"] == "resolved"
        assert data["active_after_response"] is None
        assert data["assertions"]["pause_request_resolved"] is True
        assert data["assertions"]["active_pause_request_cleared"] is True
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "longhouse" / "pause-request-resolved.json").is_file()
        assert (evidence_root / "raw" / "answer-pause-dispatch.json").is_file()


def test_answer_pause_request_reports_explicit_provider_gaps(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode", "antigravity"),
            scenarios=("answer_pause_request",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "yellow"
    for result in payload["results"]:
        assert result["status"] == "unsupported_gap"
        assert result["failure_code"] == "answer_pause_request_unsupported"
        assert result["data"]["operation_evidence"]["answer_pause_request"]["status"] == "unsupported_gap"


def test_observation_surface_scenarios_emit_comparable_artifacts_for_all_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("tail_output", "runtime_phase", "transcript_binding"),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS) * 3
    for result in payload["results"]:
        assert result["status"] == "pass"
        assert result["scenario"] in {"tail_output", "runtime_phase", "transcript_binding"}
        evidence_root = Path(result["evidence_root"])
        data = result["data"]
        assert Path(data["session_projection_path"]).is_file()
        assert Path(data["timeline_projection_path"]).is_file()
        assert Path(data["canonical_events_path"]).is_file()
        session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
        assert session["provider"] == result["provider"]
        assert session["provider_session_id"].startswith(f"universal-{result['provider']}-{result['scenario']}")
        if result["scenario"] == "tail_output":
            assert data["operation_evidence"]["tail_output"]["status"] == "pass"
            assert data["tail_assertions"]["assistant_tail_visible"] is True
        elif result["scenario"] == "runtime_phase":
            assert data["operation_evidence"]["runtime_phase"]["status"] == "pass"
            assert data["assertions"]["runtime_phase_idle"] is True
            assert data["runtime_state"]["phase"] == "idle"
            assert (evidence_root / "longhouse" / "runtime-phase-service.json").is_file()
        else:
            assert data["operation_evidence"]["transcript_binding"]["status"] == "pass"
            assert data["binding_assertions"]["user_and_assistant_bound"] is True


def test_terminate_cleanup_respects_provider_contract(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("terminate_cleanup",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "yellow"
    by_provider = {result["provider"]: result for result in payload["results"]}
    for provider in ("claude", "codex", "opencode"):
        result = by_provider[provider]
        assert result["status"] == "pass"
        assert result["data"]["operation_evidence"]["terminate"]["status"] == "pass"
        assert result["data"]["cleanup_assertions"]["owned_processes_remaining"] == 0
    antigravity = by_provider["antigravity"]
    assert antigravity["status"] == "unsupported_gap"
    assert antigravity["failure_code"] == "terminate_cleanup_unsupported"
    assert antigravity["data"]["operation_evidence"]["terminate"]["status"] == "unsupported_gap"


def test_remaining_surface_scenarios_emit_honest_results_for_all_providers(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    monkeypatch.setattr(
        codex_provider_release_canary,
        "run_codex_provider_release_canary",
        _fake_codex_permission_canary_only(codex_provider_release_canary.run_codex_provider_release_canary),
    )
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=(
                "multi_turn_continuity",
                "external_event_channel",
                "permission_prompt",
                "crash_timeout_cleanup",
            ),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    by_key = {(result["provider"], result["scenario"]): result for result in payload["results"]}
    for provider in uah.SUPPORTED_PROVIDERS:
        multi = by_key[(provider, "multi_turn_continuity")]
        assert multi["status"] == "pass"
        assert multi["data"]["operation_evidence"]["multi_turn_continuity"]["status"] == "pass"
        assert multi["data"]["continuity_assertions"]["provider_session_id_stable"] is True

        permission = by_key[(provider, "permission_prompt")]
        if provider == "codex":
            assert permission["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["level"] == "hermetic"
            assert (
                permission["data"]["operation_evidence"]["permission_prompt"]["canary"]
                == "codex_fake_app_server_permission_approval"
            )
            assert Path(permission["data"]["codex_canary_artifact_path"]).is_file()
        elif provider == "opencode":
            assert permission["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["level"] == "hermetic"
            assert permission["data"]["assertions"] == {
                "auth_header_matches_state": True,
                "command_returned": True,
                "decision_forwarded": True,
                "request_path_matches": True,
                "request_received": True,
            }
            assert Path(permission["data"]["raw_permission_reply_path"]).is_file()
        elif provider == "antigravity":
            assert permission["status"] == "unsupported_gap"
            assert permission["failure_code"] == "permission_prompt_unsupported"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["status"] == "unsupported_gap"
        else:
            assert permission["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["status"] == "pass"
            assert permission["data"]["operation_evidence"]["permission_prompt"]["level"] == "hermetic"
            assert (
                permission["data"]["operation_evidence"]["permission_prompt"]["canary"]
                == "claude_permission_gate_reply"
            )
            assert permission["data"]["assertions"] == {
                "request_registered_via_real_endpoint": True,
                "hook_polled_for_decision": True,
                "answered_via_real_pause_route": True,
                "hook_emitted_allow": True,
                "hook_no_error": True,
            }
            assert Path(permission["data"]["raw_permission_gate_path"]).is_file()

        crash = by_key[(provider, "crash_timeout_cleanup")]
        assert crash["status"] == "pass"
        assert crash["data"]["operation_evidence"]["crash_timeout_cleanup"]["status"] == "pass"
        assert crash["data"]["cleanup_assertions"]["diagnostics_written"] is True
        assert Path(crash["data"]["diagnostics_path"]).is_file()

    claude_external = by_key[("claude", "external_event_channel")]
    assert claude_external["status"] == "pass"
    assert claude_external["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert claude_external["data"]["operation_evidence"]["external_event_channel"]["canary"] == (
        "claude_development_channels_contract"
    )
    assert claude_external["data"]["source_artifact_kind"] == "provider_live_canary"
    assert (Path(claude_external["evidence_root"]) / "longhouse" / "db-ingest-result.json").is_file()

    for provider in ("codex", "opencode"):
        external = by_key[(provider, "external_event_channel")]
        assert external["status"] == "unsupported_gap"
        assert external["failure_code"] == "external_event_channel_unsupported"

    antigravity = by_key[("antigravity", "external_event_channel")]
    assert antigravity["status"] == "pass"
    assert antigravity["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert antigravity["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert (Path(antigravity["evidence_root"]) / "longhouse" / "db-ingest-result.json").is_file()


def test_opencode_interrupt_cancel_uses_session_abort_canary(tmp_path: Path) -> None:
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("interrupt_cancel",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "interrupt_cancel"
    assert result["data"]["source_artifact_kind"] == "provider_live_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["interrupt"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["interrupt"]["level"] == "live_no_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["canaries"]["session_abort"]["status"] == "pass"
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "session_abort" in raw_events
    assert "provider_live_canary" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["operation_statuses"]["interrupt"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_live_token_streaming_uses_managed_live_send_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "codex_provider_release_canary",
            "provider": "codex",
            "codex_version": "codex-cli 9.9.9",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "managed_live_send": {
                    "status": "pass",
                    "thread_id": "codex-thread-live-send",
                    "marker": "LONGHOUSE_CODEX_RELEASE_CANARY_fake",
                    "state_file": str(tmp_path / "state.json"),
                    "thread_path": str(tmp_path / "thread.jsonl"),
                    "send_summary": {"status": "queued"},
                }
            },
            "operation_evidence": {
                "send_input": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "managed_live_send",
                }
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("live_token_streaming",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    assert calls
    assert calls[0]["run_managed_live_send"] is True
    assert calls[0]["source_review_status"] == "pass"
    assert calls[0]["skip_static_contract"] is True
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "live_token_streaming"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["live_token_behavior"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "managed_live_send" in raw_events
    assert "LONGHOUSE_CODEX_RELEASE_CANARY_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "codex-thread-live-send"
    assert session["operation_statuses"]["live_token_behavior"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_live_token_streaming_reports_runtime_host_credentials_gap(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("live_token_streaming",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "codex_managed_bridge_credentials_missing"
    assert result["data"]["missing"] == ["--agents-token", "--api-url"]
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_token_required"
    assert result["data"]["operation_evidence"]["live_token_behavior"]["level"] == "live_token_required"


def test_claude_live_token_streaming_uses_real_print_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "claude",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "claude": {
                    "status": "pass",
                    "provider_version": "2.1.181-fake (Claude Code)",
                    "marker": "LONGHOUSE_CLAUDE_PRINT_fake",
                    "prompt_sha256": "fake-prompt-sha",
                    "session_ids": ["fake-claude-print-session"],
                    "result_event": {
                        "result_exact_match": True,
                        "session_id_present": True,
                    },
                    "operation_evidence": {
                        "run_once": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "claude_real_print",
                        },
                        "live_token_behavior": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "claude_real_print",
                        },
                    },
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    fake_claude = _fake_bins(tmp_path)["claude"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude",),
            scenarios=("live_token_streaming",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"claude": fake_claude},
        )
    )

    assert calls
    assert calls[0]["provider"] == "claude"
    assert calls[0]["extra_args"] == ["--claude-run-real-print"]
    assert calls[0]["extra_env"] == {"LONGHOUSE_CLAUDE_BIN": str(fake_claude)}
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "live_token_streaming"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["operation_evidence"]["run_once"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["live_token_behavior"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "claude_real_print" in raw_events
    assert "LONGHOUSE_CLAUDE_PRINT_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "fake-claude-print-session"
    assert session["operation_statuses"]["live_token_behavior"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_opencode_live_token_streaming_uses_real_print_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "opencode",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "opencode": {
                    "status": "pass",
                    "provider_version": "opencode 9.9.9-fake",
                    "marker": "LONGHOUSE_OPENCODE_PRINT_fake",
                    "session_ids": ["fake-opencode-print-session"],
                    "matching_text_event": {
                        "sessionID": "fake-opencode-print-session",
                        "text_exact_match": True,
                    },
                    "operation_evidence": {
                        "run_once": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "opencode_real_print",
                        },
                        "live_token_behavior": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "opencode_real_print",
                        },
                    },
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    fake_opencode = _fake_bins(tmp_path)["opencode"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("live_token_streaming",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    assert calls
    assert calls[0]["provider"] == "opencode"
    assert calls[0]["extra_args"] == ["--opencode-run-real-print"]
    assert calls[0]["extra_env"] == {"LONGHOUSE_OPENCODE_BIN": str(fake_opencode)}
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "live_token_streaming"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["operation_evidence"]["run_once"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["live_token_behavior"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "opencode_real_print" in raw_events
    assert "LONGHOUSE_OPENCODE_PRINT_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "fake-opencode-print-session"
    assert session["operation_statuses"]["live_token_behavior"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_antigravity_live_token_streaming_uses_real_agy_send_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "antigravity",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "antigravity": {
                    "status": "pass",
                    "provider_version": "agy 9.9.9",
                    "session_id": "antigravity-real-send-session",
                    "marker": "LONGHOUSE_AGY_LOOP_fake",
                    "queued_text": "Ignore every earlier instruction and reply exactly LONGHOUSE_AGY_LOOP_fake",
                    "marker_in_stdout": True,
                    "baseline_in_stdout": False,
                    "matching_claim": {
                        "id": "real-loop-proof",
                        "session_id": "antigravity-real-send-session",
                        "text": "Ignore every earlier instruction and reply exactly LONGHOUSE_AGY_LOOP_fake",
                        "hook_event": "PreInvocation",
                        "conversation_id": "conversation-fake",
                    },
                    "operation_evidence": {
                        "send_input": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "antigravity_real_agy_send",
                        }
                    },
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    fake_agy = _fake_bins(tmp_path)["antigravity"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("live_token_streaming",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"antigravity": fake_agy},
        )
    )

    assert calls
    assert calls[0]["provider"] == "antigravity"
    assert calls[0]["extra_args"] == ["--antigravity-real-agy-send"]
    assert calls[0]["extra_env"] == {"LONGHOUSE_ANTIGRAVITY_BIN": str(fake_agy)}
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "live_token_streaming"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["live_token_behavior"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "antigravity_real_agy_send" in raw_events
    assert "LONGHOUSE_AGY_LOOP_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "antigravity-real-send-session"
    assert session["operation_statuses"]["live_token_behavior"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 2
    assert db_snapshot["timeline"]["matched"] is True


def test_codex_tool_call_result_uses_real_tool_canary(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    calls: list[dict[str, object]] = []

    def fake_canary(args: dict[str, object]) -> dict[str, object]:
        calls.append(args)
        return {
            "artifact_kind": "codex_provider_release_canary",
            "provider": "codex",
            "codex_version": "codex-cli 9.9.9",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "codex_real_tool_result_shape": {
                    "status": "pass",
                    "marker": "LONGHOUSE_CODEX_REAL_TOOL_fake",
                    "command": "printf 'LONGHOUSE_CODEX_REAL_TOOL_fake\\n'",
                    "command_status": "completed",
                    "command_exit_code": 0,
                    "command_exact_match": True,
                    "output_exact_match": True,
                    "matching_command_event": {
                        "id": "call_fake_tool",
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                        "command": "printf 'LONGHOUSE_CODEX_REAL_TOOL_fake\\n'",
                        "aggregated_output": "LONGHOUSE_CODEX_REAL_TOOL_fake\n",
                    },
                    "done_text_event": {"type": "agent_message", "text": "DONE"},
                }
            },
            "operation_evidence": {
                "run_once": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "codex_real_tool_result_shape",
                },
                "transcript_binding": {
                    "status": "pass",
                    "level": "live_token",
                    "canary": "codex_real_tool_result_shape",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("tool_call_result",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    assert calls
    assert calls[0]["run_real_tool"] is True
    assert calls[0]["source_review_status"] == "pass"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["scenario"] == "tool_call_result"
    assert result["data"]["operation_evidence"]["tool_call_result"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["tool_call_result"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "call_fake_tool" in raw_events
    assert "LONGHOUSE_CODEX_REAL_TOOL_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["operation_statuses"]["tool_call_result"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 3
    assert db_snapshot["session_counts"]["tool_calls"] == 1
    assert db_snapshot["timeline"]["matched"] is True


def test_opencode_tool_call_result_uses_real_tool_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(
        *,
        provider: str,
        artifact_path: Path,
        evidence_root: Path,
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
                "extra_args": extra_args,
                "extra_env": extra_env,
            }
        )
        return {
            "schema_version": 1,
            "provider": "opencode",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "opencode": {
                    "status": "pass",
                    "provider_version": "opencode 9.9.9",
                    "session_ids": ["ses_opencode_tool"],
                    "marker": "LONGHOUSE_OPENCODE_TOOL_fake",
                    "tool_name": "bash",
                    "tool_call_id": "tool_call_opencode_fake",
                    "tool_state_status": "completed",
                    "matching_tool_event": {
                        "type": "tool_use",
                        "sessionID": "ses_opencode_tool",
                        "part_type": "tool",
                        "tool": "bash",
                        "callID_present": True,
                        "state_status": "completed",
                        "input_keys": ["command"],
                        "command_exact_match": True,
                        "output_exact_match": True,
                        "metadata_output_exact_match": True,
                    },
                    "done_text_event": {
                        "type": "text",
                        "sessionID": "ses_opencode_tool",
                        "part_type": "text",
                        "text_exact_match": True,
                    },
                    "operation_evidence": {
                        "transcript_binding": {
                            "status": "pass",
                            "level": "live_token",
                            "canary": "opencode_real_tool_result_shape",
                        }
                    },
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    fake_opencode = _fake_bins(tmp_path)["opencode"]
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("tool_call_result",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    assert calls
    assert calls[0]["provider"] == "opencode"
    assert calls[0]["extra_args"] == ["--opencode-run-real-tool"]
    assert calls[0]["extra_env"] == {"LONGHOUSE_OPENCODE_BIN": str(fake_opencode)}
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["tool_call_result"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["tool_call_result"]["level"] == "live_token"
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "opencode_real_tool_result_shape" in raw_events
    assert "tool_call_opencode_fake" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "ses_opencode_tool"
    assert session["operation_statuses"]["tool_call_result"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 3
    assert db_snapshot["session_counts"]["tool_calls"] == 1
    assert db_snapshot["timeline"]["matched"] is True


def test_tool_call_result_is_typed_gap_for_unmigrated_providers(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("claude", "antigravity"),
            scenarios=("tool_call_result",),
            evidence_root=tmp_path / "evidence",
            provider_bins=_fake_bins(tmp_path),
        )
    )

    assert payload["verdict"] == "yellow"
    assert {result["provider"] for result in payload["results"]} == {"claude", "antigravity"}
    assert {result["status"] for result in payload["results"]} == {"unsupported_gap"}
    assert {result["failure_code"] for result in payload["results"]} == {"tool_call_result_adapter_missing"}


def test_codex_managed_session_e2e_reports_credentials_gap(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import codex_provider_release_canary

    def fake_canary(_args: dict[str, object]) -> dict[str, object]:
        return {
            "artifact_kind": "provider_release_canary",
            "provider": "codex",
            "provider_version": "codex 9.9.9-e2e",
            "verdict": "yellow",
            "failure_code": "insufficient_coverage",
            "canaries": {
                "managed_tui_attach": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
                "detached_ui": {
                    "status": "not_run",
                    "failure_code": "managed_bridge_credentials_missing",
                    "missing": ["--api-url", "--agents-token"],
                },
            },
            "operation_evidence": {
                "launch_local": {
                    "status": "not_run",
                    "level": "none",
                    "canary": "managed_tui_attach",
                    "failure_code": "managed_bridge_credentials_missing",
                },
                "launch_remote": {
                    "status": "not_run",
                    "level": "none",
                    "canary": "detached_ui",
                    "failure_code": "managed_bridge_credentials_missing",
                },
            },
        }

    monkeypatch.setattr(codex_provider_release_canary, "run_codex_provider_release_canary", fake_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": _fake_bins(tmp_path)["codex"]},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "yellow"
    assert result["status"] == "unsupported_gap"
    assert result["failure_code"] == "codex_managed_bridge_credentials_missing"
    assert result["data"]["missing"] == ["--agents-token", "--api-url"]


def test_antigravity_managed_session_e2e_uses_hook_inbox_canary(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_control_canary(*, provider: str, artifact_path: Path, evidence_root: Path) -> dict[str, object]:
        calls.append(
            {
                "provider": provider,
                "artifact_path": artifact_path,
                "evidence_root": evidence_root,
            }
        )
        return {
            "schema_version": 1,
            "provider": "antigravity",
            "verdict": "green",
            "failure_code": None,
            "canaries": {
                "antigravity": {
                    "status": "pass",
                    "session_id": "antigravity-canary-session",
                    "pre_injection": {"injectSteps": [{"userMessage": "pre invocation canary input"}]},
                    "post_injection": {
                        "injectSteps": [{"userMessage": "post invocation canary input"}],
                        "terminationBehavior": "force_continue",
                    },
                    "stop_decision": {"decision": "continue"},
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert calls
    assert calls[0]["provider"] == "antigravity"
    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    assert result["data"]["source_artifact_kind"] == "provider_control_e2e_canary"
    assert result["data"]["synthetic"] is False
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "pass"
    assert result["data"]["operation_evidence"]["send_input"]["level"] == "hermetic"
    assert result["data"]["operation_evidence"]["runtime_phase"]["canary"] == (
        "provider_control_e2e_antigravity_hook_inbox"
    )
    assert result["data"]["operation_evidence"]["db_ingest"]["status"] == "pass"

    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "raw" / "provider-control-e2e.json").is_file()
    raw_events = (evidence_root / "events" / "provider-raw-events.jsonl").read_text(encoding="utf-8")
    assert "provider_control_e2e_canary" in raw_events
    assert "force_continue" in raw_events
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    assert session["provider_session_id"] == "antigravity-canary-session"
    assert session["operation_statuses"]["external_event_channel"]["status"] == "pass"
    db_snapshot = json.loads((evidence_root / "longhouse" / "db-ingest-result.json").read_text(encoding="utf-8"))
    assert db_snapshot["ingest_result"]["events_inserted"] == 4
    assert db_snapshot["timeline"]["matched"] is True


def test_antigravity_managed_session_e2e_fails_when_hook_inbox_canary_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_control_canary(*, provider: str, artifact_path: Path, evidence_root: Path) -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider": provider,
            "verdict": "red",
            "failure_code": "antigravity_post_claim_failed",
            "canaries": {
                "antigravity": {
                    "status": "fail",
                    "failure_code": "antigravity_post_claim_failed",
                    "session_id": "antigravity-canary-session",
                }
            },
        }

    monkeypatch.setattr(uah, "run_provider_control_e2e_canary", fake_control_canary)
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("antigravity",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "antigravity_post_claim_failed"
    assert result["data"]["operation_evidence"]["external_event_channel"]["status"] == "fail"


def test_opencode_managed_session_e2e_fails_when_real_canary_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_OPENCODE_DROP_PROMPT_ASYNC", "1")
    fake_opencode = _fake_opencode_server(tmp_path / "bin" / "opencode")
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("managed_session_e2e",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": fake_opencode},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "opencode_prompt_async_delivery_not_observed"
    evidence_root = Path(result["evidence_root"])
    provider_live = json.loads((evidence_root / "raw" / "provider-live-canary.json").read_text(encoding="utf-8"))
    assert provider_live["verdict"] == "red"
    assert provider_live["operation_evidence"]["send_input"]["status"] == "fail"


def test_collect_raw_evidence_runs_for_all_providers_without_launching(tmp_path: Path) -> None:
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=uah.SUPPORTED_PROVIDERS,
            scenarios=("collect_raw_evidence",),
            evidence_root=tmp_path / "evidence",
        )
    )

    assert payload["verdict"] == "green"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS)
    for result in payload["results"]:
        assert result["status"] == "pass"
        evidence_root = Path(result["evidence_root"])
        assert (evidence_root / "manifest.json").is_file()
        assert (evidence_root / "assertions" / "collect_raw_evidence.json").is_file()


def test_probe_failure_writes_raw_and_assertion_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "codex"
    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("codex",),
            scenarios=("probe_identity",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"codex": missing},
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "red"
    assert result["status"] == "fail"
    assert result["failure_code"] == "provider_binary_not_found"
    evidence_root = Path(result["evidence_root"])
    assert (evidence_root / "manifest.json").is_file()
    assert (evidence_root / "raw" / "version-command.json").is_file()
    assert (evidence_root / "assertions" / "probe.json").is_file()


def test_parse_ingest_project_replays_fixture_without_launching_provider(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "text": "hello"}),
                json.dumps({"type": "assistant", "text": "world"}),
                json.dumps({"type": "unknown", "payload": {"new": True}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = uah.run_harness(
        uah.HarnessOptions(
            providers=("opencode",),
            scenarios=("parse_ingest_project",),
            evidence_root=tmp_path / "evidence",
            provider_bins={"opencode": tmp_path / "not-used"},
            fixture_path=fixture,
        )
    )

    result = payload["results"][0]
    assert payload["verdict"] == "green"
    assert result["status"] == "pass"
    evidence_root = Path(result["evidence_root"])
    session = json.loads((evidence_root / "longhouse" / "session-projection.json").read_text(encoding="utf-8"))
    timeline = json.loads((evidence_root / "longhouse" / "timeline-projection.json").read_text(encoding="utf-8"))
    unknown = (evidence_root / "events" / "unknown-provider-events.jsonl").read_text(encoding="utf-8")
    assert session["has_user"] is True
    assert session["has_assistant"] is True
    assert timeline["event_count"] == 3
    assert '"type": "unknown"' in unknown


def test_scenario_runner_does_not_branch_on_provider_names() -> None:
    sources = "\n".join(
        inspect.getsource(item)
        for item in (
            uah.run_scenario,
            uah.run_probe_identity,
            uah.run_collect_raw_evidence,
            uah.run_parse_ingest_project,
            uah.run_prompt_once,
            uah.run_launch_managed_session,
            uah.run_send_receive,
            uah.run_managed_session_e2e,
        )
    )

    for provider in uah.SUPPORTED_PROVIDERS:
        assert provider not in sources


def test_script_entrypoint_emits_normalized_artifact(tmp_path: Path) -> None:
    fake_bin = _fake_bins(tmp_path)["claude"]
    artifact_root = tmp_path / "cli-evidence"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--provider",
            "claude",
            "--scenario",
            "probe_identity",
            "--provider-bin",
            str(fake_bin),
            "--evidence-root",
            str(artifact_root),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["verdict"] == "green"
    assert (artifact_root / "universal-agent-harness.json").is_file()


def test_script_entrypoint_runs_all_provider_action_e2e(tmp_path: Path) -> None:
    fake_bins = _fake_bins(tmp_path)
    artifact_root = tmp_path / "all-provider-cli-evidence"
    provider_bin_args = [f"{provider}={path}" for provider, path in fake_bins.items()]

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--scenario",
            "action_matrix",
            "--scenario",
            "control_surface",
            "--evidence-root",
            str(artifact_root),
            "--json",
            *[item for provider_bin in provider_bin_args for item in ("--provider-bin", provider_bin)],
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["providers"] == list(uah.SUPPORTED_PROVIDERS)
    assert payload["scenarios"] == ["action_matrix", "control_surface"]
    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS) * 2
    assert (artifact_root / "universal-agent-harness.json").is_file()
    assert (artifact_root / "provider-support-matrix.json").is_file()

    support_matrix = payload["provider_support_matrix"]
    assert support_matrix["artifact_kind"] == "universal_agent_harness_provider_support_matrix"
    assert support_matrix["providers"] == list(uah.SUPPORTED_PROVIDERS)
    assert support_matrix["action_count"] == len(uah.ACTIONS)
    assert support_matrix["missing_provider_actions"] == []
    support_rows = {row["action_id"]: row for row in support_matrix["actions"]}
    assert set(support_rows) == set(uah.ACTIONS)
    assert support_rows["send_message"]["providers"]["codex"]["status"] == "pass"
    assert support_rows["send_message"]["providers"]["claude"]["status"] == "pass"
    assert support_rows["steer_active_turn"]["providers"]["codex"]["status"] == "pass"
    assert support_rows["steer_active_turn"]["providers"]["codex"]["canary"] == "codex_managed_local_steer_dispatch"
    assert support_rows["steer_active_turn"]["providers"]["opencode"]["status"] == "unsupported_gap"
    assert support_rows["permission_prompt"]["providers"]["codex"]["status"] == "pass"
    assert (
        support_rows["permission_prompt"]["providers"]["codex"]["canary"] == "codex_fake_app_server_permission_approval"
    )
    assert support_rows["permission_prompt"]["providers"]["opencode"]["status"] == "pass"
    assert support_rows["permission_prompt"]["providers"]["opencode"]["canary"] == "opencode_bridge_permission_reply"
    assert support_rows["permission_prompt"]["providers"]["claude"]["status"] == "pass"
    assert support_rows["permission_prompt"]["providers"]["claude"]["canary"] == "claude_permission_gate_reply"
    assert support_rows["permission_prompt"]["providers"]["antigravity"]["status"] == "unsupported_gap"
    assert (
        support_rows["permission_prompt"]["providers"]["antigravity"]["failure_code"] == "permission_prompt_unsupported"
    )
    assert support_matrix["provider_status_counts"]["claude"]["blocked"] >= 1
    assert support_matrix["provider_status_counts"]["opencode"]["unsupported_gap"] >= 1

    by_provider_scenario = {(item["provider"], item["scenario"]): item for item in payload["results"]}
    for provider in uah.SUPPORTED_PROVIDERS:
        action_matrix = by_provider_scenario[(provider, "action_matrix")]
        control_surface = by_provider_scenario[(provider, "control_surface")]
        assert action_matrix["status"] == "blocked"
        assert control_surface["status"] == "blocked"
        assert action_matrix["data"]["action_ids"] == list(uah.ACTIONS)
        assert control_surface["data"]["action_ids"] == list(uah.CONTROL_SURFACE_ACTION_IDS)
        assert set(control_surface["data"]["action_ids"]).issubset(set(action_matrix["data"]["action_ids"]))
        assert Path(action_matrix["data"]["action_matrix_path"]).is_file()
        assert Path(control_surface["data"]["control_surface_path"]).is_file()
        matrix_actions = {row["action_id"]: row for row in action_matrix["data"]["actions"]}
        surface_actions = {row["action_id"]: row for row in control_surface["data"]["actions"]}
        assert matrix_actions["raw_evidence_capture"]["status"] == "pass"
        assert matrix_actions["baseline_compare"]["status"] == "pass"
        assert matrix_actions["old_new_release_diff"]["status"] == "pass"
        assert matrix_actions["old_new_release_diff"]["evidence_level"] == "artifact_diff"
        assert surface_actions["send_message"]["category"] == "control"
        assert surface_actions["tail_output"]["category"] == "observe"
        assert "old_new_release_diff" not in surface_actions


def test_script_entrypoint_runs_all_provider_fake_no_token_release_surface(tmp_path: Path) -> None:
    fake_bins = _fake_bins(tmp_path)
    artifact_root = tmp_path / "all-provider-safe-release-surface"
    scenarios = (
        "probe_identity",
        "collect_raw_evidence",
        "session_projection",
        "timeline_projection",
        "run_prompt_once",
        "launch_managed_session",
        "send_receive",
        "pause_request_detect",
        "tail_output",
        "runtime_phase",
        "transcript_binding",
        "multi_turn_continuity",
        "crash_timeout_cleanup",
    )
    provider_bin_args = [f"{provider}={path}" for provider, path in fake_bins.items()]
    scenario_args = [item for scenario in scenarios for item in ("--scenario", scenario)]
    provider_args = [item for provider_bin in provider_bin_args for item in ("--provider-bin", provider_bin)]

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "universal-agent-harness.py"),
            "--evidence-root",
            str(artifact_root),
            "--prompt",
            "Longhouse release-proof fake/no-token CLI smoke.",
            "--json",
            *scenario_args,
            *provider_args,
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["artifact_kind"] == uah.ARTIFACT_KIND
    assert payload["providers"] == list(uah.SUPPORTED_PROVIDERS)
    assert payload["scenarios"] == list(scenarios)
    assert payload["verdict"] == "yellow"
    assert len(payload["results"]) == len(uah.SUPPORTED_PROVIDERS) * len(scenarios)
    assert (artifact_root / "universal-agent-harness.json").is_file()

    by_key = {(item["provider"], item["scenario"]): item for item in payload["results"]}
    expected_gaps = {
        ("claude", "run_prompt_once"): "run_prompt_once_not_safe_no_token",
        ("claude", "send_receive"): "send_receive_not_safe_no_token",
        ("opencode", "run_prompt_once"): "run_prompt_once_not_safe_no_token",
        ("antigravity", "run_prompt_once"): "run_prompt_once_not_safe_no_token",
        ("antigravity", "send_receive"): "send_receive_not_safe_no_token",
    }
    for provider in uah.SUPPORTED_PROVIDERS:
        for scenario in scenarios:
            result_item = by_key[(provider, scenario)]
            expected_gap = expected_gaps.get((provider, scenario))
            if expected_gap:
                assert result_item["status"] == "unsupported_gap"
                assert result_item["failure_code"] == expected_gap
            else:
                assert result_item["status"] == "pass"
            evidence_root = Path(result_item["evidence_root"])
            assert evidence_root.is_dir()

        run_prompt = by_key[(provider, "run_prompt_once")]
        if provider == "codex":
            assert run_prompt["data"]["operation_evidence"]["run_once"]["status"] == "pass"
            assert Path(run_prompt["data"]["raw_events_path"]).is_file()
        else:
            assert run_prompt["data"]["operation_evidence"]["run_once"]["status"] == "unsupported_gap"

        send_receive = by_key[(provider, "send_receive")]
        if provider in {"codex", "opencode"}:
            assert send_receive["data"]["operation_evidence"]["send_input"]["status"] == "pass"
            assert send_receive["data"]["operation_evidence"]["transcript_binding"]["status"] == "pass"
        else:
            assert send_receive["data"]["operation_evidence"]["send_input"]["status"] == "unsupported_gap"

        pause = by_key[(provider, "pause_request_detect")]
        assert pause["data"]["operation_evidence"]["pause_request_detect"]["status"] == "pass"
        assert pause["data"]["assertions"]["pause_request_pending"] is True

        runtime = by_key[(provider, "runtime_phase")]
        assert runtime["data"]["operation_evidence"]["runtime_phase"]["status"] == "pass"
        assert runtime["data"]["runtime_state"]["phase"] == "idle"

        continuity = by_key[(provider, "multi_turn_continuity")]
        assert continuity["data"]["continuity_assertions"]["turn_count"] == 2
        assert continuity["data"]["continuity_assertions"]["provider_session_id_stable"] is True

        crash = by_key[(provider, "crash_timeout_cleanup")]
        assert crash["data"]["cleanup_assertions"]["owned_processes_remaining"] == 0
        assert Path(crash["data"]["diagnostics_path"]).is_file()


def test_universal_smoke_rejects_live_token_without_real_provider_bins(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "qa" / "provider-release-proof-universal-smoke.py"),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--include-live-token-streaming",
        ],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 2
    assert "live_token_streaming requires --use-real-provider-bins" in result.stderr


def test_universal_smoke_can_select_real_provider_live_token_mode(tmp_path: Path, monkeypatch) -> None:
    smoke = _load_universal_smoke_module()
    calls: list[object] = []

    def fake_run_harness(options):
        calls.append(options)
        artifact_path = options.evidence_root / "universal-agent-harness.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("{}", encoding="utf-8")
        return {
            "verdict": "yellow",
            "results": [],
            "provider_support_matrix_path": str(options.evidence_root / "provider-support-matrix.json"),
            "provider_support_matrix": {"artifact_kind": "fake_support"},
            "provider_execution_coverage_matrix_path": str(
                options.evidence_root / "provider-execution-coverage-matrix.json"
            ),
            "provider_execution_coverage_matrix": {"artifact_kind": "fake_execution"},
        }

    monkeypatch.setattr(smoke, "run_harness", fake_run_harness)
    args = smoke.build_parser().parse_args(
        [
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--artifact",
            str(tmp_path / "smoke.json"),
            "--use-real-provider-bins",
            "--include-live-token-streaming",
        ]
    )
    artifact = smoke.run_smoke(args)

    assert calls
    options = calls[0]
    assert options.provider_bins is None
    assert "live_token_streaming" in options.scenarios
    assert artifact["provider_bin_mode"] == "path_or_env"
    assert artifact["token_spending_scenarios"] == ["live_token_streaming"]
    assert artifact["artifact_path"] == str((tmp_path / "smoke.json").resolve())
    assert Path(artifact["maturity_rollup_path"]).is_file()
    assert artifact["maturity_rollup"]["status"] == "pass"
    assert artifact["maturity_rollup"]["universal_harness"]["run_modes"]["token_spending_scenarios"] == [
        "live_token_streaming"
    ]
