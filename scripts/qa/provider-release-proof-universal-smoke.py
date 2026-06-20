#!/usr/bin/env python3
"""Run the all-provider universal release-proof smoke.

The default mode is fake/no-token and safe for CI. The opt-in live-token mode
uses real provider binaries from PATH/env and may spend provider tokens.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER_PATH = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_PATH))

from zerg.qa.universal_agent_harness import SUPPORTED_PROVIDERS
from zerg.qa.universal_agent_harness import HarnessOptions
from zerg.qa.universal_agent_harness import run_harness

DEFAULT_SCENARIOS = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "full_action_suite",
    "baseline_compare",
    "old_new_release_diff",
    "parse_ingest_project",
    "db_ingest_project",
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
    "managed_session_e2e",
)
LIVE_TOKEN_SCENARIO = "live_token_streaming"
FAKE_VERSION_BY_PROVIDER = {
    "claude": "2.9.9-fake (Claude Code)",
    "codex": "codex-cli 9.9.9",
    "opencode": "opencode 9.9.9",
    "antigravity": "agy 9.9.9",
}
FAKE_BINARY_BY_PROVIDER = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "agy",
}


def _fake_opencode_server_script() -> str:
    return r"""#!/usr/bin/env python3
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
provider_session_id = "ses_fake_universal_smoke"
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
"""


def _fake_claude_provider_live_script() -> str:
    return r"""#!/usr/bin/env python3
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
"""


def _fake_antigravity_provider_live_script() -> str:
    return r"""#!/usr/bin/env python3
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
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def default_evidence_root() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".build/canaries/provider-release-proof-universal-smoke") / stamp


def write_fake_provider_bins(root: Path) -> dict[str, Path]:
    bin_root = root / "fake-provider-bins"
    bin_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for provider in SUPPORTED_PROVIDERS:
        path = bin_root / FAKE_BINARY_BY_PROVIDER[provider]
        if provider == "opencode":
            path.write_text(_fake_opencode_server_script(), encoding="utf-8")
            path.chmod(0o755)
            result[provider] = path
            continue
        if provider == "claude":
            path.write_text(_fake_claude_provider_live_script(), encoding="utf-8")
            path.chmod(0o755)
            result[provider] = path
            continue
        if provider == "antigravity":
            path.write_text(_fake_antigravity_provider_live_script(), encoding="utf-8")
            path.chmod(0o755)
            result[provider] = path
            continue
        version = FAKE_VERSION_BY_PROVIDER[provider]
        path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    'if sys.argv[1:] == ["--version"]:',
                    f"    print({version!r})",
                    "    raise SystemExit(0)",
                    'print("unexpected fake provider args: " + repr(sys.argv[1:]), file=sys.stderr)',
                    "raise SystemExit(2)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        result[provider] = path
    return result


def write_parse_fixture(root: Path) -> Path:
    fixture_path = root / "fixtures" / "provider-events.jsonl"
    rows = (
        {"type": "user", "text": "universal smoke hello"},
        {"type": "assistant", "text": "universal smoke world"},
        {
            "type": "tool",
            "tool_name": "shell",
            "tool_call_id": "tool-smoke",
            "text": "ok",
        },
    )
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    return fixture_path


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
        return "synthetic_warning"
    return "synthetic_drift"


def write_synthetic_release_proof(
    root: Path,
    provider: str,
    name: str,
    *,
    version: str,
    status: str = "pass",
) -> Path:
    proof_dir = root / "synthetic-old-new-proofs" / provider / name
    artifact_dir = proof_dir / "evidence"
    source_artifact = artifact_dir / "source.json"
    stdout = artifact_dir / "stdout.log"
    stderr = artifact_dir / "stderr.log"
    normalized_contract = artifact_dir / "normalized" / "contract.json"
    provider_contract = artifact_dir / "normalized" / "provider-contract.json"
    operation_evidence_artifact = (
        artifact_dir / "normalized" / "operation-evidence.json"
    )
    session_projection = artifact_dir / "normalized" / "session-projection.json"
    action_matrix = artifact_dir / "normalized" / "action-matrix.json"
    control_surface = artifact_dir / "normalized" / "control-surface.json"
    provider_version = f"{provider} {version}"
    canary = "universal_smoke_synthetic_old_new_diff"
    failure_code = _proof_failure_for_status(status)
    operation_evidence = {
        "send_input": {
            "status": status,
            "level": "synthetic",
            "canary": canary,
            "failure_code": failure_code,
        },
        "old_new_release_diff": {
            "status": "pass",
            "level": "artifact_diff",
            "canary": "provider_release_proof_old_new_diff",
        },
    }
    action_rows = [
        {
            "action_id": "send_message",
            "category": "control",
            "status": status,
            "support": True,
            "support_reason": "synthetic_provider_scoped_proof",
            "required_evidence": "synthetic",
            "evidence_level": "synthetic",
            "proof_scope": "provider_release_proof_smoke",
            "contract_operation": "send_input",
            "canary": canary,
            "failure_code": failure_code,
        },
        {
            "action_id": "old_new_release_diff",
            "category": "release_diff",
            "status": "pass",
            "support": True,
            "support_reason": "provider_release_proof",
            "required_evidence": "artifact_diff",
            "evidence_level": "artifact_diff",
            "proof_scope": "provider_release_proof_old_new",
            "canary": "provider_release_proof_old_new_diff",
        },
    ]
    normalized = {
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": provider_version,
        "verdict": _proof_verdict_for_status(status),
        "failure_code": failure_code,
        "operation_evidence": operation_evidence,
    }
    provider_contract_payload = {
        "artifact_kind": "provider_release_proof_provider_contract",
        "provider": provider,
        "provider_version": provider_version,
        "contract_operations": {
            "send_input": {
                "status": status,
                "level": "synthetic",
                "canary": canary,
                "failure_code": failure_code,
            }
        },
    }
    operation_evidence_payload = {
        "artifact_kind": "provider_release_proof_operation_evidence",
        "provider": provider,
        "provider_version": provider_version,
        "operation_evidence": operation_evidence,
    }
    session_projection_payload = {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": provider,
        "provider_version": provider_version,
        "status": "captured",
        "projection": {
            "artifact_kind": "longhouse_session_projection",
            "provider": provider,
            "status": status,
            "checks": {"send_input": {"status": status, "failure_code": failure_code}},
            "operation_statuses": operation_evidence,
        },
    }
    action_matrix_payload = {
        "artifact_kind": "provider_release_proof_action_matrix",
        "provider": provider,
        "provider_version": provider_version,
        "status": "captured",
        "action_matrix": {
            "artifact_kind": "provider_release_proof_action_matrix",
            "provider": provider,
            "action_count": len(action_rows),
            "action_ids": [row["action_id"] for row in action_rows],
            "status_counts": (
                {"pass": len(action_rows)}
                if status == "pass"
                else {status: 1, "pass": 1}
            ),
            "actions": action_rows,
        },
    }
    control_surface_payload = {
        "artifact_kind": "provider_release_proof_control_surface",
        "provider": provider,
        "provider_version": provider_version,
        "status": "captured",
        "control_surface": {
            "artifact_kind": "provider_release_proof_control_surface",
            "provider": provider,
            "action_count": 1,
            "action_ids": ["send_message"],
            "status_counts": {status: 1},
            "actions": [action_rows[0]],
        },
    }
    write_json(source_artifact, {"synthetic": True, "provider": provider, "side": name})
    stdout.parent.mkdir(parents=True, exist_ok=True)
    stdout.write_text("synthetic old/new smoke\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    write_json(normalized_contract, normalized)
    write_json(provider_contract, provider_contract_payload)
    write_json(operation_evidence_artifact, operation_evidence_payload)
    write_json(session_projection, session_projection_payload)
    write_json(action_matrix, action_matrix_payload)
    write_json(control_surface, control_surface_payload)
    proof = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": provider_version,
        "scenario_id": f"{provider}-universal-smoke-old-new-v1",
        "scenario_version": 1,
        "verdict": _proof_verdict_for_status(status),
        "failure_code": failure_code,
        "normalized": normalized,
        "artifacts": {
            "source_artifact": str(source_artifact.resolve()),
            "stdout": str(stdout.resolve()),
            "stderr": str(stderr.resolve()),
            "normalized_contract": str(normalized_contract.resolve()),
            "provider_contract": str(provider_contract.resolve()),
            "operation_evidence": str(operation_evidence_artifact.resolve()),
            "session_projection": str(session_projection.resolve()),
            "action_matrix": str(action_matrix.resolve()),
            "control_surface": str(control_surface.resolve()),
        },
    }
    proof_path = proof_dir / "proof.json"
    write_json(proof_path, proof)
    return proof_path


def write_synthetic_old_new_release_proofs(
    root: Path,
) -> tuple[dict[str, Path], dict[str, Path]]:
    old_paths: dict[str, Path] = {}
    new_paths: dict[str, Path] = {}
    for provider in SUPPORTED_PROVIDERS:
        old_paths[provider] = write_synthetic_release_proof(
            root,
            provider,
            "old",
            version="9.9.8-smoke",
        )
        new_paths[provider] = write_synthetic_release_proof(
            root,
            provider,
            "new",
            version="9.9.9-smoke",
        )
    return old_paths, new_paths


def write_maturity_rollup(
    *, artifact_path: Path, evidence_root: Path
) -> dict[str, Any]:
    maturity_path = evidence_root / "provider-release-proof-maturity.json"
    maturity_script = (
        Path(__file__).resolve().with_name("provider-release-proof-maturity.py")
    )
    result = subprocess.run(
        [
            sys.executable,
            str(maturity_script),
            "--universal-artifact",
            str(artifact_path),
            "--artifact",
            str(maturity_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return {
            "status": "fail",
            "maturity_rollup_path": str(maturity_path),
            "failure_code": "maturity_rollup_failed",
            "returncode": result.returncode,
            "stderr": result.stderr,
        }
    payload = json.loads(maturity_path.read_text(encoding="utf-8"))
    universal_harness = (
        payload.get("universal_harness")
        if isinstance(payload.get("universal_harness"), dict)
        else {}
    )
    return {
        "status": "pass",
        "maturity_rollup_path": str(maturity_path),
        "universal_harness": {
            key: universal_harness.get(key)
            for key in (
                "status",
                "run_modes",
                "execution_coverage_pass_percent",
                "required_evidence_rollup",
            )
            if universal_harness.get(key) is not None
        },
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    evidence_root = (
        (args.evidence_root or default_evidence_root()).expanduser().resolve()
    )
    artifact_path = (
        (
            args.artifact
            or (evidence_root / "provider-release-proof-universal-smoke.json")
        )
        .expanduser()
        .resolve()
    )
    scenarios = _selected_scenarios(args)
    provider_bins = (
        None if args.use_real_provider_bins else write_fake_provider_bins(evidence_root)
    )
    fixture_path = write_parse_fixture(evidence_root)
    old_proof_paths, new_proof_paths = write_synthetic_old_new_release_proofs(
        evidence_root
    )
    harness = run_harness(
        HarnessOptions(
            providers=SUPPORTED_PROVIDERS,
            scenarios=scenarios,
            evidence_root=evidence_root / "universal-agent-harness",
            provider_bins=provider_bins,
            fixture_path=fixture_path,
            prompt=_smoke_prompt(args),
            old_proof_paths=old_proof_paths,
            new_proof_paths=new_proof_paths,
            baseline_root=evidence_root / "baselines",
        )
    )
    artifact = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof_universal_smoke",
        "generated_at": utc_now(),
        "verdict": harness.get("verdict"),
        "providers": list(SUPPORTED_PROVIDERS),
        "scenarios": list(scenarios),
        "provider_bin_mode": "path_or_env" if args.use_real_provider_bins else "fake",
        "token_spending_scenarios": [LIVE_TOKEN_SCENARIO]
        if LIVE_TOKEN_SCENARIO in scenarios
        else [],
        "result_count": len(harness.get("results") or []),
        "evidence_root": str(evidence_root),
        "synthetic_old_proof_paths": {
            provider: str(path) for provider, path in old_proof_paths.items()
        },
        "synthetic_new_proof_paths": {
            provider: str(path) for provider, path in new_proof_paths.items()
        },
        "universal_harness_artifact": str(
            evidence_root / "universal-agent-harness" / "universal-agent-harness.json"
        ),
        "provider_support_matrix_path": harness.get("provider_support_matrix_path"),
        "provider_support_matrix": harness.get("provider_support_matrix"),
        "provider_execution_coverage_matrix_path": harness.get(
            "provider_execution_coverage_matrix_path"
        ),
        "provider_execution_coverage_matrix": harness.get(
            "provider_execution_coverage_matrix"
        ),
    }
    artifact["artifact_path"] = str(artifact_path)
    write_json(artifact_path, artifact)
    maturity_rollup = write_maturity_rollup(
        artifact_path=artifact_path,
        evidence_root=evidence_root,
    )
    artifact["maturity_rollup"] = maturity_rollup
    artifact["maturity_rollup_path"] = maturity_rollup.get("maturity_rollup_path")
    write_json(artifact_path, artifact)
    return artifact


def _selected_scenarios(args: argparse.Namespace) -> tuple[str, ...]:
    scenarios = list(args.scenario or DEFAULT_SCENARIOS)
    if args.include_live_token_streaming and LIVE_TOKEN_SCENARIO not in scenarios:
        scenarios.append(LIVE_TOKEN_SCENARIO)
    return tuple(scenarios)


def _smoke_prompt(args: argparse.Namespace) -> str:
    if args.use_real_provider_bins:
        return "Longhouse release-proof universal real-provider smoke."
    return "Longhouse release-proof universal fake/no-token smoke."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument(
        "--scenario",
        action="append",
        help="Universal scenario to run. Repeatable; defaults to fake/no-token smoke surface.",
    )
    parser.add_argument(
        "--use-real-provider-bins",
        action="store_true",
        help="Resolve provider binaries from PATH/env instead of generating fake no-token binaries.",
    )
    parser.add_argument(
        "--include-live-token-streaming",
        action="store_true",
        help=(
            "Append live_token_streaming to the scenario list. Requires --use-real-provider-bins and may spend tokens."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the smoke artifact as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    requested_scenarios = set(args.scenario or ())
    if (
        args.include_live_token_streaming or LIVE_TOKEN_SCENARIO in requested_scenarios
    ) and not args.use_real_provider_bins:
        parser.error(
            "live_token_streaming requires --use-real-provider-bins because it may spend provider tokens"
        )
    artifact = run_smoke(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {artifact['artifact_path']}")
    return 1 if artifact.get("verdict") == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
