#!/usr/bin/env python3
"""Run the all-provider universal release-proof smoke with fake no-token binaries."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER_PATH = Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(SERVER_PATH))

from zerg.qa.universal_agent_harness import HarnessOptions
from zerg.qa.universal_agent_harness import SUPPORTED_PROVIDERS
from zerg.qa.universal_agent_harness import run_harness

DEFAULT_SCENARIOS = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "full_action_suite",
    "baseline_compare",
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


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    evidence_root = (args.evidence_root or default_evidence_root()).expanduser().resolve()
    artifact_path = (
        args.artifact or (evidence_root / "provider-release-proof-universal-smoke.json")
    ).expanduser().resolve()
    scenarios = tuple(args.scenario or DEFAULT_SCENARIOS)
    provider_bins = write_fake_provider_bins(evidence_root)
    fixture_path = write_parse_fixture(evidence_root)
    harness = run_harness(
        HarnessOptions(
            providers=SUPPORTED_PROVIDERS,
            scenarios=scenarios,
            evidence_root=evidence_root / "universal-agent-harness",
            provider_bins=provider_bins,
            fixture_path=fixture_path,
            prompt="Longhouse release-proof universal fake/no-token smoke.",
        )
    )
    artifact = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof_universal_smoke",
        "generated_at": utc_now(),
        "verdict": harness.get("verdict"),
        "providers": list(SUPPORTED_PROVIDERS),
        "scenarios": list(scenarios),
        "result_count": len(harness.get("results") or []),
        "evidence_root": str(evidence_root),
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
    return artifact


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
        "--json", action="store_true", help="Print the smoke artifact as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact = run_smoke(args)
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    else:
        print(f"verdict: {artifact['verdict']}")
        print(f"artifact: {artifact['artifact_path']}")
    return 1 if artifact.get("verdict") == "red" else 0


if __name__ == "__main__":
    raise SystemExit(main())
