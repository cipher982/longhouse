#!/usr/bin/env python3
"""Tests for live upstream managed-provider canary artifacts."""

from __future__ import annotations

import json
import os
import subprocess
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
import re
import signal
import sys
import threading
import time
from pathlib import Path
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
state_path = Path(os.environ.get("FAKE_OPENCODE_STATE_FILE") or Path.cwd() / ".fake-opencode-state.json")

def load_messages():
    try:
        payload = json.loads(state_path.read_text())
    except Exception:
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    if os.environ.get("FAKE_OPENCODE_FORGET_REATTACH_ON_RESTART") == "1":
        def contains_reattach_marker(item):
            return "LONGHOUSE_OPENCODE_REATTACH_" in json.dumps(item)

        return [item for item in messages if not contains_reattach_marker(item)]
    return messages

messages = load_messages()
abort_event = threading.Event()

def save_state():
    state_path.write_text(json.dumps({"messages": messages}))

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
        "/session/{sessionID}/message": {
            "get": {"operationId": "session.messages"},
            "post": {"operationId": "session.prompt"},
        },
        "/session/{sessionID}/prompt_async": {"post": prompt_async_operation},
        "/session/{sessionID}/abort": {"post": {"operationId": "session.abort"}},
    }
    if os.environ.get("FAKE_OPENCODE_OMIT_PROMPT_ASYNC") == "1":
        paths.pop("/session/{sessionID}/prompt_async")
    if os.environ.get("FAKE_OPENCODE_OMIT_MESSAGES") == "1":
        paths.pop("/session/{sessionID}/message")
    if os.environ.get("FAKE_OPENCODE_OMIT_MESSAGE_POST") == "1":
        paths["/session/{sessionID}/message"].pop("post")
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
            has_assistant = any((item.get("info") or {}).get("role") == "assistant" for item in messages)
            if os.environ.get("FAKE_OPENCODE_MESSAGE_GET_500") == "1" and has_assistant:
                self._json({"error": "boom"}, 500)
                return
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
                save_state()
            self._empty()
            return
        if parsed.path == f"/session/{provider_session_id}/message":
            if os.environ.get("FAKE_OPENCODE_MESSAGE_POST_500") == "1":
                self._json({"error": "boom"}, 500)
                return
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8") or "{}")
            prompt_text = " ".join(
                str(part.get("text") or "")
                for part in payload.get("parts") or []
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if "LONGHOUSE_OPENCODE_ABORT_" in prompt_text:
                user_message = {
                    "info": {"id": "msg_fake_abort_user", "sessionID": provider_session_id, "role": "user"},
                    "parts": payload.get("parts") or [],
                }
                messages.append(user_message)
                save_state()
                if os.environ.get("FAKE_OPENCODE_STALE_ABORT_TRANSCRIPT") == "1":
                    messages.append({
                        "info": {
                            "id": "msg_fake_stale_abort_assistant",
                            "sessionID": provider_session_id,
                            "role": "assistant",
                            "parentID": "msg_fake_stale_user",
                            "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
                        },
                        "parts": [],
                    })
                    save_state()
                if os.environ.get("FAKE_OPENCODE_ABORT_IGNORED") == "1":
                    time.sleep(2)
                    assistant_message = {
                        "info": {
                            "id": "msg_fake_abort_assistant",
                            "sessionID": provider_session_id,
                            "role": "assistant",
                            "providerID": "fake",
                            "modelID": "fake-model",
                            "finish": "stop",
                            "cost": 0.001,
                            "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        },
                        "parts": [{"type": "text", "text": "not aborted"}],
                    }
                    messages.append(assistant_message)
                    save_state()
                    self._json(assistant_message)
                    return
                abort_event.wait(10)
                assistant_message = {
                    "info": {
                        "id": "msg_fake_abort_assistant",
                        "sessionID": provider_session_id,
                        "role": "assistant",
                        "parentID": "msg_fake_abort_user",
                        "providerID": "fake",
                        "modelID": "fake-model",
                        "cost": 0,
                        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                        "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
                    },
                    "parts": [],
                }
                messages.append(assistant_message)
                save_state()
                self._json(assistant_message)
                return
            match = re.search(r"LONGHOUSE_OPENCODE_LIVE_[0-9a-f]+", prompt_text)
            marker = match.group(0) if match else "missing-marker"
            response_text = "wrong-marker" if os.environ.get("FAKE_OPENCODE_BAD_ASSISTANT_MARKER") == "1" else marker
            user_message = {
                "info": {"id": "msg_fake_live_user", "sessionID": provider_session_id, "role": "user"},
                "parts": payload.get("parts") or [],
            }
            assistant_message = {
                "info": {
                    "id": "msg_fake_live_assistant",
                    "sessionID": provider_session_id,
                    "role": "assistant",
                    "providerID": "fake",
                    "modelID": "fake-model",
                    "finish": "stop",
                    "cost": 0.001,
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
                "parts": [{"type": "text", "text": response_text}],
            }
            messages.append(user_message)
            if os.environ.get("FAKE_OPENCODE_DROP_ASSISTANT_TRANSCRIPT") != "1":
                messages.append(assistant_message)
            save_state()
            self._json(assistant_message)
            return
        if parsed.path == f"/session/{provider_session_id}/abort":
            if os.environ.get("FAKE_OPENCODE_ABORT_500") == "1":
                self._json({"error": "boom"}, 500)
                return
            if os.environ.get("FAKE_OPENCODE_ABORT_BAD_SHAPE") == "1":
                self._json({"ok": False})
                return
            if os.environ.get("FAKE_OPENCODE_ABORT_IGNORED") != "1":
                abort_event.set()
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


def _run_canary(
    root: Path,
    fake_bin: Path,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
):
    artifact = root / "artifact.json"
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT / "server"),
            "python",
            str(CANARY),
            "--provider",
            "opencode",
            "--provider-bin",
            str(fake_bin),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
            *(extra_args or []),
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
            "uv",
            "run",
            "--project",
            str(REPO_ROOT / "server"),
            "python",
            str(CANARY),
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


def test_opencode_live_canary_proves_server_and_no_token_contracts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(root, fake_bin)

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "opencode"
        assert payload["provider_version"] == "1.2.3-fake"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["canaries"]["binary_identity"]["status"] == "pass"
        assert payload["canaries"]["server_startup"]["status"] == "pass"
        assert payload["canaries"]["schema_probe"]["status"] == "pass"
        assert payload["canaries"]["session_create"]["tokens"]["input"] == 0
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "pass"
        assert (
            payload["canaries"]["prompt_async_no_reply_delivery"][
                "observed_message_count"
            ]
            == 1
        )
        assert (
            payload["canaries"]["process_restart_reattach_contract"]["status"] == "pass"
        )
        assert payload["canaries"]["session_abort"]["status"] == "pass"
        assert "assistant_response_contract" not in payload["canaries"]
        assert "prompt_async_execution_contract" not in payload["canaries"]
        assert "active_turn_abort_contract" not in payload["canaries"]
        assert set(payload["operation_evidence"]) == {
            "interrupt",
            "launch_local",
            "reattach",
            "send_input",
            "transcript_binding",
        }
        assert payload["operation_evidence"]["launch_local"]["level"] == "live_no_token"
        assert payload["operation_evidence"]["reattach"]["level"] == "live_no_token"
        assert (
            payload["operation_evidence"]["reattach"]["canary"]
            == "opencode_process_restart_reattach_contract"
        )
        assert payload["operation_evidence"]["send_input"]["status"] == "pass"
        assert payload["operation_evidence"]["send_input"]["level"] == "live_no_token"
        assert (
            payload["operation_evidence"]["send_input"]["canary"]
            == "opencode_prompt_async_no_reply_delivery"
        )
        assert payload["operation_evidence"]["interrupt"]["status"] == "pass"
        assert payload["operation_evidence"]["interrupt"]["level"] == "live_no_token"
        assert (
            payload["operation_evidence"]["interrupt"]["canary"]
            == "opencode_abort_endpoint"
        )
        assert payload["operation_evidence"]["transcript_binding"]["status"] == "pass"
        assert (
            payload["operation_evidence"]["transcript_binding"]["level"]
            == "live_no_token"
        )
        assert (
            payload["operation_evidence"]["transcript_binding"]["canary"]
            == "opencode_prompt_async_no_reply_delivery"
        )
        serialized = json.dumps(payload)
        assert "Reply with exactly this token" not in serialized


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


def test_antigravity_live_canary_proves_hook_inbox_without_advertising_send() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_antigravity(root / "bin" / "agy")
        result, payload = _run_provider_canary(
            root, provider="antigravity", fake_bin=fake_bin
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider"] == "antigravity"
        assert payload["provider_version"] == "1.0.2-fake"
        assert payload["verdict"] == "green"
        assert payload["canaries"]["hook_inbox_claim_contract"]["status"] == "pass"
        assert payload["canaries"]["hook_inbox_claim_contract"]["pre_injection"][
            "injectSteps"
        ]
        assert (
            payload["canaries"]["hook_inbox_claim_contract"]["post_injection"][
                "terminationBehavior"
            ]
            == "force_continue"
        )
        assert (
            payload["canaries"]["hook_inbox_claim_contract"]["stop_decision"][
                "decision"
            ]
            == "continue"
        )
        assert payload["canaries"]["hook_inbox_claim_contract"][
            "empty_stop_decision"
        ] == {
            "decision": "allow",
            "reason": "",
        }
        assert payload["operation_evidence"]["launch_local"]["status"] == "pass"
        assert "send_input" not in payload.get("operation_evidence", {})


def test_opencode_live_canary_fails_when_schema_drops_prompt_async() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_OMIT_PROMPT_ASYNC": "1"}
        )

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
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_OMIT_MESSAGES": "1"}
        )

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
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_OMIT_NOREPLY_SCHEMA": "1"}
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_request_property"
        assert failures[0]["property"] == "noReply"


def test_opencode_live_canary_fails_when_message_post_schema_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root,
            fake_bin,
            {"FAKE_OPENCODE_OMIT_MESSAGE_POST": "1"},
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_schema_probe_failed"
        failures = payload["canaries"]["schema_probe"]["failures"]
        assert failures[0]["failure_code"] == "opencode_schema_missing_path"
        assert failures[0]["expected"] == "session.prompt"
        assert (
            payload["operation_evidence"]["send_input"]["canary"]
            == "opencode_prompt_schema"
        )


def test_opencode_live_canary_accepts_empty_successful_abort_response() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_EMPTY_ABORT": "1"}
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["verdict"] == "green"
        assert payload["canaries"]["session_abort"]["status"] == "pass"


def test_opencode_live_canary_reports_prompt_async_request_failure_on_send_input() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_PROMPT_ASYNC_500": "1"}
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_prompt_async_request_failed"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "fail"
        assert (
            payload["canaries"]["prompt_async_no_reply_delivery"]["request_phase"]
            == "post_prompt_async"
        )
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert (
            payload["operation_evidence"]["send_input"]["failure_code"]
            == "opencode_prompt_async_request_failed"
        )


def test_opencode_live_canary_fails_when_prompt_async_delivery_is_not_observed() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_DROP_PROMPT_ASYNC": "1"}
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_prompt_async_delivery_not_observed"
        assert payload["canaries"]["prompt_async_no_reply_delivery"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["level"] == "none"


def test_opencode_live_canary_fails_when_restart_loses_transcript_marker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fake_bin = _fake_opencode(root / "bin" / "opencode")
        result, payload = _run_canary(
            root, fake_bin, {"FAKE_OPENCODE_FORGET_REATTACH_ON_RESTART": "1"}
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "opencode_reattach_transcript_marker_missing"
        assert (
            payload["canaries"]["process_restart_reattach_contract"]["status"] == "fail"
        )
        assert payload["operation_evidence"]["reattach"]["status"] == "fail"
        assert payload["operation_evidence"]["reattach"]["level"] == "none"
        assert (
            payload["operation_evidence"]["reattach"]["failure_code"]
            == "opencode_reattach_transcript_marker_missing"
        )


def main() -> int:
    tests = [
        test_opencode_live_canary_proves_server_and_no_token_contracts,
        test_antigravity_live_canary_fails_when_plugin_install_fails,
        test_antigravity_live_canary_proves_hook_inbox_without_advertising_send,
        test_opencode_live_canary_fails_when_schema_drops_prompt_async,
        test_opencode_live_canary_fails_when_schema_drops_session_messages,
        test_opencode_live_canary_fails_when_noreply_schema_is_missing,
        test_opencode_live_canary_fails_when_message_post_schema_is_missing,
        test_opencode_live_canary_accepts_empty_successful_abort_response,
        test_opencode_live_canary_reports_prompt_async_request_failure_on_send_input,
        test_opencode_live_canary_fails_when_prompt_async_delivery_is_not_observed,
        test_opencode_live_canary_fails_when_restart_loses_transcript_marker,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
