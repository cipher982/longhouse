#!/usr/bin/env python3
"""Tests for live upstream managed-provider canary artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/provider-live-canary.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_opencode(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import base64
import http.server
import json
import os
import signal
import sys
from urllib.parse import urlparse

args = sys.argv[1:]
if args == ["--version"]:
    print("1.2.3-fake")
    raise SystemExit(0)

if args == ["attach", "--help"]:
    print("opencode attach <url>")
    print("-s, --session session id")
    print("-p, --password basic auth password (defaults to OPENCODE_SERVER_PASSWORD)")
    print("-u, --username basic auth username (defaults to OPENCODE_SERVER_USERNAME or 'opencode')")
    raise SystemExit(0)

if not args or args[0] != "serve":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
provider_session_id = "ses_fake_provider_live"
messages = []

def make_doc():
    prompt_async_operation = {
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
                    },
                },
            },
        },
    }
    if os.environ.get("FAKE_OPENCODE_OMIT_NOREPLY_SCHEMA") == "1":
        prompt_async_operation["requestBody"]["content"]["application/json"]["schema"]["properties"].pop("noReply")
    paths = {
        "/global/health": {"get": {"operationId": "global.health"}},
        "/session": {"post": {"operationId": "session.create"}},
        "/session/{sessionID}": {"get": {"operationId": "session.get"}},
        "/session/{sessionID}/message": {"get": {"operationId": "session.messages"}},
        "/session/{sessionID}/prompt_async": {"post": prompt_async_operation},
        "/session/{sessionID}/abort": {"post": {"operationId": "session.abort"}},
    }
    if os.environ.get("FAKE_OPENCODE_OMIT_PROMPT_ASYNC") == "1":
        paths.pop("/session/{sessionID}/prompt_async")
    if os.environ.get("FAKE_OPENCODE_OMIT_MESSAGES") == "1":
        paths.pop("/session/{sessionID}/message")
    return {"openapi": "3.1.0", "paths": paths}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status=204):
        self.send_response(status)
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
            self._json({"healthy": True, "version": "1.2.3-fake"})
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
            if os.environ.get("FAKE_OPENCODE_PROMPT_ASYNC_500") == "1":
                self._json({"error": "boom"}, 500)
                return
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            if os.environ.get("FAKE_OPENCODE_DROP_PROMPT_ASYNC") != "1":
                messages.append({
                    "info": {"id": "msg_fake_user", "sessionID": provider_session_id, "role": "user"},
                    "parts": payload.get("parts") or [],
                })
            self._empty()
            return
        if parsed.path == f"/session/{provider_session_id}/abort":
            if os.environ.get("FAKE_OPENCODE_EMPTY_ABORT") == "1":
                self._empty()
                return
            self._json(True)
            return
        self._json({"error": "not found"}, 404)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
""",
    )


def _fake_claude(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.9.9-fake (Claude Code)")
    raise SystemExit(0)

if args == ["auth", "status", "--json"]:
    if os.environ.get("FAKE_CLAUDE_AUTH_NONZERO") == "1":
        print("email=should-not-appear@example.com orgId=org-secret", file=sys.stderr)
        raise SystemExit(1)
    if os.environ.get("FAKE_CLAUDE_AUTH_INVALID_JSON") == "1":
        print("email=should-not-appear@example.com orgId=org-secret")
        raise SystemExit(0)
    if os.environ.get("FAKE_CLAUDE_NOT_LOGGED_IN") == "1":
        print(json.dumps({"loggedIn": False, "authMethod": "", "apiProvider": ""}))
        raise SystemExit(0)
    if os.environ.get("FAKE_CLAUDE_API_AUTH") == "1":
        print(json.dumps({
            "loggedIn": True,
            "authMethod": "apiKey",
            "apiProvider": "anthropic",
            "email": "should-not-appear@example.com",
            "orgId": "org-secret",
        }))
        raise SystemExit(0)
    print(json.dumps({
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "email": "should-not-appear@example.com",
        "orgId": "org-secret",
        "subscriptionType": "pro",
    }))
    raise SystemExit(0)

if args == ["--help"]:
    if os.environ.get("FAKE_CLAUDE_MISSING_SESSION_ID") == "1":
        print("--resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    else:
        print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

if args == ["--dangerously-load-development-channels", "server:longhouse-channel", "--help"]:
    if os.environ.get("FAKE_CLAUDE_BAD_CHANNELS") == "1":
        print("unknown option --dangerously-load-development-channels", file=sys.stderr)
        raise SystemExit(1)
    print("--session-id --resume --dangerously-skip-permissions --mcp-config --strict-mcp-config --permission-mode")
    raise SystemExit(0)

print("unexpected fake claude args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _fake_antigravity(path: Path) -> Path:
    return _write_exe(
        path,
        r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("1.0.2-fake")
    raise SystemExit(0)

if args == ["--help"]:
    if os.environ.get("FAKE_AGY_MISSING_PLUGIN_HELP") == "1":
        print("--print --prompt-interactive --conversation")
    else:
        print("--print --prompt-interactive --conversation plugin")
    raise SystemExit(0)

if args == ["plugin", "--help"]:
    print("install <target>")
    print("list")
    print("validate")
    raise SystemExit(0)

def installed_marker():
    return Path(os.environ.get("HOME", ".")) / ".fake-agy-plugins.json"

if len(args) == 3 and args[:2] == ["plugin", "validate"]:
    root = Path(args[2])
    if not (root / "plugin.json").is_file():
        print("missing plugin.json", file=sys.stderr)
        raise SystemExit(1)
    print("[ok] " + str(root))
    raise SystemExit(0)

if len(args) == 3 and args[:2] == ["plugin", "install"]:
    if os.environ.get("FAKE_AGY_INSTALL_FAIL") == "1":
        print("install failed", file=sys.stderr)
        raise SystemExit(1)
    root = Path(args[2])
    name = json.loads((root / "plugin.json").read_text()).get("name")
    marker = installed_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"imports": [{"name": name}]}))
    print("[ok] " + str(root))
    raise SystemExit(0)

if args == ["plugin", "list"]:
    marker = installed_marker()
    if marker.exists():
        print(marker.read_text())
    else:
        print(json.dumps({"imports": []}))
    raise SystemExit(0)

print("unexpected fake agy args: " + json.dumps(args), file=sys.stderr)
raise SystemExit(2)
""",
    )


def _run_canary(root: Path, fake_bin: Path, extra_env: dict[str, str] | None = None):
    artifact = root / "artifact.json"
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--provider",
            "opencode",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def _run_provider_canary(
    root: Path,
    *,
    provider: str,
    fake_bin: Path,
    extra_env: dict[str, str] | None = None,
):
    artifact = root / "artifact.json"
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--provider",
            provider,
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def test_opencode_live_canary_stays_yellow_until_prompt_execution_is_proven() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "opencode"
        assert payload["provider_version"] == "1.2.3-fake"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["canaries"]["binary_identity"]["status"] == "pass"
        assert payload["canaries"]["server_startup"]["status"] == "pass"
        assert payload["canaries"]["schema_probe"]["status"] == "pass"
        assert payload["canaries"]["session_create"]["tokens"]["input"] == 0
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "pass"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["observed_message_count"] == 1
        assert payload["canaries"]["session_abort"]["status"] == "pass"
        assert payload["canaries"]["prompt_async_execution_contract"]["status"] == "not_run"
        assert set(payload["operation_evidence"]) == {"interrupt", "launch_local", "reattach", "send_input"}
        assert payload["operation_evidence"]["launch_local"]["level"] == "live_no_token"
        assert payload["operation_evidence"]["reattach"]["level"] == "live_no_token"
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["canary"] == "opencode_prompt_async_no_reply_delivery"
        assert "No-token behavior proof" in payload["operation_evidence"]["send_input"]["message"]
        assert payload["operation_evidence"]["interrupt"]["status"] == "pass"


def test_claude_live_canary_stays_yellow_until_live_token_contract_is_proven() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(root, provider="claude", fake_bin=fake_bin)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "claude"
        assert payload["provider_version"] == "2.9.9-fake (Claude Code)"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["canaries"]["auth_status"]["status"] == "pass"
        assert "email" not in payload["canaries"]["auth_status"]["auth"]
        assert payload["canaries"]["command_shape"]["status"] == "pass"
        assert payload["canaries"]["channels_shape"]["status"] == "pass"
        assert payload["canaries"]["detached_pty_shape"]["status"] == "pass"
        assert payload["canaries"]["live_token_contract"]["status"] == "not_run"
        assert set(payload["operation_evidence"]) == {"launch_local"}
        assert payload["operation_evidence"]["launch_local"]["status"] == "pass"
        assert payload["operation_evidence"]["launch_local"]["level"] == "live_no_token"


def test_antigravity_live_canary_stays_yellow_until_loop_invocation_is_proven() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_antigravity(root / "bin" / "agy")
        result, payload = _run_provider_canary(root, provider="antigravity", fake_bin=fake_bin)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "antigravity"
        assert payload["provider_version"] == "1.0.2-fake"
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["canaries"]["command_shape"]["status"] == "pass"
        assert payload["canaries"]["plugin_contract"]["status"] == "pass"
        assert payload["canaries"]["global_hooks_contract"]["status"] == "pass"
        assert payload["canaries"]["loop_invocation_contract"]["status"] == "not_run"
        assert set(payload["canaries"]["global_hooks_contract"]["events"]) == {
            "PostInvocation",
            "PostToolUse",
            "PreInvocation",
            "PreToolUse",
            "Stop",
        }
        assert "longhouse-runtime" in json.dumps(payload["canaries"]["plugin_contract"])
        assert set(payload["operation_evidence"]) == {"launch_local"}
        assert payload["operation_evidence"]["launch_local"]["level"] == "live_no_token"


def test_antigravity_live_canary_fails_when_plugin_install_fails() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_antigravity(root / "bin" / "agy")
        result, payload = _run_provider_canary(
            root,
            provider="antigravity",
            fake_bin=fake_bin,
            extra_env={"FAKE_AGY_INSTALL_FAIL": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "antigravity_plugin_install_failed"


def test_claude_live_canary_accepts_api_key_auth() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_API_AUTH": "1"},
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "insufficient_coverage"
        assert payload["canaries"]["auth_status"]["status"] == "pass"
        auth = payload["canaries"]["auth_status"]["auth"]
        assert auth["apiProvider"] == "anthropic"
        assert "email" not in auth
        assert "orgId" not in auth


def test_claude_live_canary_turns_yellow_when_not_logged_in() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_NOT_LOGGED_IN": "1"},
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["canaries"]["auth_status"]["status"] == "warn"
        assert payload["canaries"]["auth_status"]["reason"] == "claude_auth_not_logged_in"


def test_claude_auth_failures_do_not_publish_raw_identifiers() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_AUTH_INVALID_JSON": "1"},
        )

        serialized = json.dumps(payload)
        assert result.returncode == 1
        assert payload["failure_code"] == "claude_auth_status_invalid_json"
        assert "should-not-appear@example.com" not in serialized
        assert "org-secret" not in serialized


def test_claude_auth_nonzero_does_not_publish_raw_identifiers() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_AUTH_NONZERO": "1"},
        )

        serialized = json.dumps(payload)
        assert result.returncode == 0
        assert payload["verdict"] == "yellow"
        assert "should-not-appear@example.com" not in serialized
        assert "org-secret" not in serialized


def test_claude_live_canary_fails_when_channels_contract_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_BAD_CHANNELS": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_development_channels_contract_missing"


def test_claude_live_canary_fails_when_session_flag_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_claude(root / "bin" / "claude")
        result, payload = _run_provider_canary(
            root,
            provider="claude",
            fake_bin=fake_bin,
            extra_env={"FAKE_CLAUDE_MISSING_SESSION_ID": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "claude_command_contract_missing"
        assert payload["canaries"]["command_shape"]["missing"] == ["--session-id"]


def test_opencode_live_canary_fails_when_schema_drops_prompt_async() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_OMIT_PROMPT_ASYNC": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_path"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["level"] == "none"
        assert "prompt_async_execution_contract" not in payload["operation_evidence"]


def test_opencode_live_canary_fails_when_schema_drops_session_messages() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_OMIT_MESSAGES": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_path"
        assert failures[0]["path"] == "/session/{sessionID}/message"


def test_opencode_live_canary_fails_when_noreply_schema_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_OMIT_NOREPLY_SCHEMA": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_request_property"
        assert failures[0]["property"] == "noReply"


def test_opencode_live_canary_accepts_empty_successful_abort_response() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_EMPTY_ABORT": "1"})

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "yellow"
        assert payload["canaries"]["session_abort"]["status"] == "pass"


def test_opencode_live_canary_reports_prompt_async_request_failure_on_send_input() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_PROMPT_ASYNC_500": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_prompt_async_request_failed"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "fail"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["request_phase"] == "post_prompt_async"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["failure_code"] == "opencode_prompt_async_request_failed"


def test_opencode_live_canary_fails_when_prompt_async_delivery_is_not_observed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin, {"FAKE_OPENCODE_DROP_PROMPT_ASYNC": "1"})

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_prompt_async_delivery_not_observed"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["level"] == "none"


def main() -> int:
    tests = [
        test_opencode_live_canary_stays_yellow_until_prompt_execution_is_proven,
        test_claude_live_canary_stays_yellow_until_live_token_contract_is_proven,
        test_antigravity_live_canary_stays_yellow_until_loop_invocation_is_proven,
        test_antigravity_live_canary_fails_when_plugin_install_fails,
        test_claude_live_canary_accepts_api_key_auth,
        test_claude_live_canary_turns_yellow_when_not_logged_in,
        test_claude_auth_failures_do_not_publish_raw_identifiers,
        test_claude_auth_nonzero_does_not_publish_raw_identifiers,
        test_claude_live_canary_fails_when_channels_contract_is_missing,
        test_claude_live_canary_fails_when_session_flag_is_missing,
        test_opencode_live_canary_fails_when_schema_drops_prompt_async,
        test_opencode_live_canary_fails_when_schema_drops_session_messages,
        test_opencode_live_canary_fails_when_noreply_schema_is_missing,
        test_opencode_live_canary_accepts_empty_successful_abort_response,
        test_opencode_live_canary_reports_prompt_async_request_failure_on_send_input,
        test_opencode_live_canary_fails_when_prompt_async_delivery_is_not_observed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
