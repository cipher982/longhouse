#!/usr/bin/env python3
"""Hermetic managed-provider control E2E canaries.

These canaries exercise Longhouse's provider-specific control commands without
spending model tokens. They use isolated fake provider endpoints where needed
and verify that Longhouse can drive the same local control paths used by the
Machine Agent.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _status(status: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": status}
    payload.update(fields)
    return payload


def _fail(code: str, message: str, **fields: Any) -> dict[str, Any]:
    payload = {"status": "fail", "failure_code": code, "message": message}
    payload.update(fields)
    return payload


def _command_evidence(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "argv": list(result.args) if isinstance(result.args, list) else result.args,
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


def _server_cwd(args: argparse.Namespace) -> Path:
    return args.repo_root / "server"


def _server_python_cmd(args: argparse.Namespace) -> list[str]:
    if args.python_bin:
        return [args.python_bin]
    venv_python = _server_cwd(args) / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python)]
    return ["uv", "run", "python"]


def _runtime_env(args: argparse.Namespace, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DATABASE_URL", "sqlite://")
    env.setdefault("TESTING", "1")
    env.setdefault("AUTH_DISABLED", "1")
    env.setdefault("FERNET_SECRET", base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    env.setdefault("JWT_SECRET", base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    env.setdefault("INTERNAL_API_SECRET", base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        env.update(extra)
    return env


def _longhouse_cmd(args: argparse.Namespace) -> list[str]:
    if args.longhouse_bin:
        return [args.longhouse_bin]
    return [*_server_python_cmd(args), "-m", "zerg.cli.main"]


def _run_longhouse(
    args: argparse.Namespace,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_longhouse_cmd(args), *command],
        cwd=str(_server_cwd(args)),
        env=_runtime_env(args, env),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _first_event(events: list[dict[str, Any]], event: str) -> dict[str, Any] | None:
    for row in events:
        if row.get("event") == event:
            return row
    return None


def _exception_failure(code: str, exc: BaseException) -> dict[str, Any]:
    return _fail(
        code,
        f"{type(exc).__name__}: {exc}",
        traceback=traceback.format_exception_only(type(exc), exc),
    )


def _queue_process_stdout(process: subprocess.Popen[str]) -> "queue.Queue[str]":
    lines: "queue.Queue[str]" = queue.Queue()

    def _pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=_pump, daemon=True).start()
    return lines


def _fake_interruptible_process(marker: Path) -> subprocess.Popen[str]:
    script = (
        "import pathlib,signal,sys,time\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "def handle(sig, frame):\n"
        "    marker.write_text('sigint\\n', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, handle)\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(0.2)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_claude_channel_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    state_root = root / "claude-state"
    interrupt_marker = root / "claude-interrupted.txt"
    fake_claude = _fake_interruptible_process(interrupt_marker)
    bridge: subprocess.Popen[str] | None = None
    try:
        assert fake_claude.stdout is not None
        ready = fake_claude.stdout.readline().strip()
        if ready != "ready":
            return _fail("claude_fake_process_not_ready", "fake Claude process did not become ready")

        bridge = subprocess.Popen(
            [
                *_longhouse_cmd(args),
                "claude-channel",
                "serve",
                "--session-id",
                session_id,
                "--provider-session-id",
                "claude-provider-canary",
                "--state-root",
                str(state_root),
                "--auth-token",
                "canary-token",
                "--claude-pid",
                str(fake_claude.pid),
            ],
            cwd=str(_server_cwd(args)),
            env=_runtime_env(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout_lines = _queue_process_stdout(bridge)
        assert bridge.stdin is not None
        bridge.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "provider-control-e2e", "version": "0.1"},
                    },
                }
            )
            + "\n"
        )
        bridge.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        bridge.stdin.flush()
        initialize = json.loads(stdout_lines.get(timeout=5.0))
        if initialize.get("id") != 1:
            return _fail("claude_initialize_failed", "Claude channel bridge did not initialize", initialize=initialize)

        send = _run_longhouse(
            args,
            [
                "claude-channel",
                "send",
                "--session-id",
                session_id,
                "--state-root",
                str(state_root),
                "--text",
                "hello from provider control canary",
            ],
        )
        if send.returncode != 0:
            return _fail("claude_send_failed", "claude-channel send failed", evidence=_command_evidence(send))
        send_notification = json.loads(stdout_lines.get(timeout=5.0))
        send_params = send_notification.get("params", {})
        send_meta = send_params.get("meta")
        expected_send_meta = {
            "injected_by": "longhouse",
            "longhouse_session_id": session_id,
        }
        if send_params.get("content") != "hello from provider control canary" or send_meta != expected_send_meta:
            return _fail(
                "claude_send_payload_mismatch",
                "claude-channel send emitted the wrong channel notification",
                notification=send_notification,
            )

        steer = _run_longhouse(
            args,
            [
                "claude-channel",
                "send",
                "--session-id",
                session_id,
                "--state-root",
                str(state_root),
                "--text",
                "steer from provider control canary",
                "--meta",
                "intent=steer",
            ],
        )
        if steer.returncode != 0:
            return _fail("claude_steer_failed", "claude-channel steer send failed", evidence=_command_evidence(steer))
        steer_notification = json.loads(stdout_lines.get(timeout=5.0))
        steer_params = steer_notification.get("params", {})
        steer_meta = steer_params.get("meta")
        expected_steer_meta = {
            "injected_by": "longhouse",
            "intent": "steer",
            "longhouse_session_id": session_id,
        }
        if steer_params.get("content") != "steer from provider control canary" or steer_meta != expected_steer_meta:
            return _fail(
                "claude_steer_payload_mismatch",
                "claude-channel steer emitted the wrong channel notification",
                notification=steer_notification,
            )

        interrupt = _run_longhouse(
            args,
            [
                "claude-channel",
                "interrupt",
                "--session-id",
                session_id,
                "--state-root",
                str(state_root),
            ],
        )
        if interrupt.returncode != 0:
            return _fail(
                "claude_interrupt_failed",
                "claude-channel interrupt failed",
                evidence=_command_evidence(interrupt),
            )
        fake_claude.wait(timeout=5.0)
        if interrupt_marker.read_text(encoding="utf-8").strip() != "sigint":
            return _fail("claude_interrupt_marker_missing", "fake Claude process did not receive SIGINT")

        return _status(
            "pass",
            session_id=session_id,
            send_meta=send_meta,
            steer_meta=steer_meta,
            interrupt_marker=str(interrupt_marker),
        )
    except Exception as exc:  # noqa: BLE001
        return _exception_failure("claude_channel_canary_exception", exc)
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                bridge.kill()
        if fake_claude.poll() is None:
            fake_claude.terminate()
            try:
                fake_claude.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                fake_claude.kill()


def _fake_opencode(path: Path) -> Path:
    return _write_executable(
        path,
        r'''#!/usr/bin/env python3
import base64
import http.server
import json
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

events = Path(os.environ.get("FAKE_OPENCODE_EVENTS", "opencode-events.jsonl"))

def record(payload):
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")

args = sys.argv[1:]
if args == ["--version"]:
    print("opencode 0.0.0-canary")
    raise SystemExit(0)

if args and args[0] == "attach":
    record({
        "event": "attach",
        "args": args,
        "username": os.environ.get("OPENCODE_SERVER_USERNAME"),
        "password_present": bool(os.environ.get("OPENCODE_SERVER_PASSWORD")),
    })
    raise SystemExit(0)

if not args or args[0] != "serve":
    print("unexpected fake opencode args: " + json.dumps(args), file=sys.stderr)
    raise SystemExit(2)

username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
provider_session_id = "opencode-provider-canary"

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

    def _authorized(self):
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        return self.headers.get("Authorization") == expected

    def do_GET(self):
        if self.path == "/global/health":
            self._json({"healthy": True})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if parsed.path == "/session":
            record({"event": "session.create", "query": parsed.query, "payload": payload})
            self._json({"id": provider_session_id})
            return
        if parsed.path.endswith("/prompt_async"):
            record({"event": "prompt_async", "path": parsed.path, "query": parsed.query, "payload": payload})
            self._json({})
            return
        if parsed.path.endswith("/abort"):
            record({"event": "abort", "path": parsed.path, "query": parsed.query, "payload": payload})
            self._json({})
            return
        self._json({"error": "not found"}, 404)

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
record({"event": "serve", "args": args})
print(f"opencode server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
server.serve_forever()
''',
    )


def run_opencode_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_dir = root / ".claude"
    events_path = root / "opencode-events.jsonl"
    fake_bin = _fake_opencode(root / "bin" / "opencode")
    env = {"FAKE_OPENCODE_EVENTS": str(events_path)}
    try:
        launch = _run_longhouse(
            args,
            [
                "opencode-channel",
                "launch",
                "--session-id",
                session_id,
                "--cwd",
                str(workspace),
                "--api-url",
                "http://longhouse.test",
                "--api-token",
                "canary-token",
                "--device-id",
                "provider-control-canary",
                "--config-dir",
                str(config_dir),
                "--opencode-bin",
                str(fake_bin),
                "--wait-ready-secs",
                "10",
            ],
            env=env,
            timeout=20,
        )
        if launch.returncode != 0:
            return _fail("opencode_launch_failed", "opencode-channel launch failed", evidence=_command_evidence(launch))
        launch_payload = json.loads(launch.stdout)

        send = _run_longhouse(
            args,
            [
                "opencode-channel",
                "send",
                "--session-id",
                session_id,
                "--config-dir",
                str(config_dir),
                "--text",
                "hello",
            ],
            env=env,
        )
        if send.returncode != 0:
            return _fail("opencode_send_failed", "opencode-channel send failed", evidence=_command_evidence(send))

        interrupt = _run_longhouse(
            args,
            ["opencode-channel", "interrupt", "--session-id", session_id, "--config-dir", str(config_dir)],
            env=env,
        )
        if interrupt.returncode != 0:
            return _fail(
                "opencode_interrupt_failed",
                "opencode-channel interrupt failed",
                evidence=_command_evidence(interrupt),
            )

        attach = _run_longhouse(
            args,
            [
                "opencode-channel",
                "attach",
                "--session-id",
                session_id,
                "--config-dir",
                str(config_dir),
                "--opencode-bin",
                str(fake_bin),
                "--",
                "--canary-attach",
            ],
            env=env,
        )
        if attach.returncode != 0:
            return _fail("opencode_attach_failed", "opencode-channel attach failed", evidence=_command_evidence(attach))

        events = _read_json_lines(events_path)
        observed = {row.get("event") for row in events}
        expected = {"serve", "session.create", "prompt_async", "abort", "attach"}
        missing = sorted(expected - observed)
        if missing:
            return _fail(
                "opencode_events_missing",
                "fake OpenCode server did not observe all events",
                missing=missing,
                events=events,
            )
        prompt_event = _first_event(events, "prompt_async") or {}
        if prompt_event.get("payload") != {"parts": [{"type": "text", "text": "hello"}]}:
            return _fail(
                "opencode_prompt_payload_mismatch",
                "OpenCode prompt_async payload did not match the managed send contract",
                event=prompt_event,
            )
        attach_event = _first_event(events, "attach") or {}
        if attach_event.get("username") != "opencode" or attach_event.get("password_present") is not True:
            return _fail(
                "opencode_attach_env_mismatch",
                "OpenCode attach did not receive server credentials in the process environment",
                event=attach_event,
            )

        return _status(
            "pass",
            session_id=session_id,
            provider_session_id=launch_payload.get("provider_session_id"),
            observed_events=sorted(observed),
        )
    except Exception as exc:  # noqa: BLE001
        return _exception_failure("opencode_canary_exception", exc)
    finally:
        _run_longhouse(
            args,
            ["opencode-channel", "stop", "--session-id", session_id, "--config-dir", str(config_dir)],
            env=env,
            timeout=10,
        )


def _install_antigravity_hook(args: argparse.Namespace, root: Path, config_dir: Path) -> Path:
    code = textwrap.dedent(
        f"""
        from pathlib import Path
        from zerg.cli.antigravity import _ensure_antigravity_runtime_plugin
        path = _ensure_antigravity_runtime_plugin(
            config_dir=Path({str(config_dir)!r}),
            antigravity_cli_root=Path({str(root / "ag-cli")!r}),
            engine_path="/bin/true",
            global_hooks_path=Path({str(root / "hooks.json")!r}),
        )
        print(path)
        """
    )
    result = subprocess.run(
        [*_server_python_cmd(args), "-c", code],
        cwd=str(_server_cwd(args)),
        env=_runtime_env(args),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return Path(result.stdout.strip()) / "longhouse-antigravity-hook.sh"


def _invoke_antigravity_hook(
    script: Path,
    event: str,
    *,
    session_id: str,
    config_dir: Path,
    payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    from_env = {
        "LONGHOUSE_HOOK_PYTHON": sys.executable,
        "LONGHOUSE_ENGINE": "/bin/true",
        "LONGHOUSE_MANAGED_SESSION_ID": session_id,
        "LONGHOUSE_ANTIGRAVITY_INBOX_DIR": str(config_dir / "managed-local" / "antigravity" / "inbox" / session_id),
        "LONGHOUSE_ANTIGRAVITY_STATE_DIR": str(config_dir / "managed-local" / "antigravity" / "sessions"),
        "PATH": os.defpath,
    }
    return subprocess.run(
        [str(script), event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=from_env,
        timeout=10,
    )


def _enqueue_antigravity_direct(
    args: argparse.Namespace,
    session_id: str,
    text: str,
    config_dir: Path,
) -> dict[str, Any]:
    code = textwrap.dedent(
        f"""
        import json
        from pathlib import Path
        from zerg.cli.antigravity_channel import enqueue_antigravity_message
        print(json.dumps(enqueue_antigravity_message(
            session_id={session_id!r},
            text={text!r},
            config_dir=Path({str(config_dir)!r}),
        )))
        """
    )
    result = subprocess.run(
        [*_server_python_cmd(args), "-c", code],
        cwd=str(_server_cwd(args)),
        env=_runtime_env(args),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def _wait_for_antigravity_pending_message(config_dir: Path, session_id: str, *, timeout_secs: float = 10.0) -> Path:
    inbox_dir = config_dir / "managed-local" / "antigravity" / "inbox" / session_id
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        messages = sorted(inbox_dir.glob("msg-*.json")) if inbox_dir.exists() else []
        for message in messages:
            try:
                json.loads(message.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            return message
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for Antigravity inbox message in {inbox_dir}")


def run_antigravity_canary(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    session_id = "antigravity-canary-session"
    config_dir = root / ".claude"
    send_proc: subprocess.Popen[str] | None = None
    hook_payload = {
        "conversationId": "antigravity-provider-canary",
        "workspacePaths": [str(root / "workspace")],
        "transcriptPath": str(root / "transcript.jsonl"),
        "stepIdx": 7,
    }
    try:
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        script = _install_antigravity_hook(args, root, config_dir)

        send_proc = subprocess.Popen(
            [
                *_longhouse_cmd(args),
                "antigravity-channel",
                "send",
                "--config-dir",
                str(config_dir),
                "--session-id",
                session_id,
                "--text",
                "pre invocation canary input",
                "--wait-claimed-secs",
                "20",
            ],
            cwd=str(_server_cwd(args)),
            env=_runtime_env(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_antigravity_pending_message(config_dir, session_id)
        pre = _invoke_antigravity_hook(
            script,
            "PreInvocation",
            session_id=session_id,
            config_dir=config_dir,
            payload=hook_payload,
        )
        send_stdout, send_stderr = send_proc.communicate(timeout=25)
        if send_proc.returncode != 0:
            return _fail(
                "antigravity_send_claim_failed",
                "antigravity-channel send did not observe a claim",
                stdout=send_stdout,
                stderr=send_stderr,
            )
        pre_payload = json.loads(pre.stdout or "{}")
        if pre_payload.get("injectSteps") != [{"userMessage": "pre invocation canary input"}]:
            return _fail(
                "antigravity_pre_injection_missing",
                "PreInvocation did not inject queued input",
                output=pre_payload,
                stderr=pre.stderr,
            )

        _enqueue_antigravity_direct(args, session_id, "post invocation canary input", config_dir)
        post = _invoke_antigravity_hook(
            script,
            "PostInvocation",
            session_id=session_id,
            config_dir=config_dir,
            payload=hook_payload,
        )
        post_payload = json.loads(post.stdout or "{}")
        if post_payload.get("terminationBehavior") != "force_continue":
            return _fail(
                "antigravity_force_continue_missing",
                "PostInvocation did not request force_continue",
                output=post_payload,
            )

        _enqueue_antigravity_direct(args, session_id, "stop canary input", config_dir)
        stop = _invoke_antigravity_hook(
            script,
            "Stop",
            session_id=session_id,
            config_dir=config_dir,
            payload=hook_payload,
        )
        stop_payload = json.loads(stop.stdout or "{}")
        if stop_payload.get("decision") != "continue":
            return _fail(
                "antigravity_stop_continue_missing",
                "Stop did not continue with pending inbox input",
                output=stop_payload,
            )

        return _status(
            "pass",
            session_id=session_id,
            pre_injection=pre_payload,
            post_injection=post_payload,
            stop_decision=stop_payload,
        )
    except Exception as exc:  # noqa: BLE001
        return _exception_failure("antigravity_canary_exception", exc)
    finally:
        if send_proc is not None and send_proc.poll() is None:
            send_proc.terminate()
            try:
                send_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                send_proc.kill()


def classify(canaries: dict[str, dict[str, Any]]) -> tuple[str, str | None]:
    for name, result in canaries.items():
        if result.get("status") == "fail":
            return "red", str(result.get("failure_code") or name)
    return "green", None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_repo_root_from_script())
    parser.add_argument("--provider", choices=["claude", "opencode", "antigravity", "all"], default="all")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--python-bin")
    parser.add_argument("--longhouse-bin")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_root = args.evidence_root or args.repo_root / ".build/canaries/provider-control-e2e" / timestamp
    artifact_path = args.artifact or evidence_root / "provider-control-e2e.json"
    evidence_root.mkdir(parents=True, exist_ok=True)

    selected = ["claude", "opencode", "antigravity"] if args.provider == "all" else [args.provider]
    canaries: dict[str, dict[str, Any]] = {}
    for provider in selected:
        provider_root = evidence_root / provider
        provider_root.mkdir(parents=True, exist_ok=True)
        if provider == "claude":
            canaries[provider] = run_claude_channel_canary(args, provider_root)
        elif provider == "opencode":
            canaries[provider] = run_opencode_canary(args, provider_root)
        elif provider == "antigravity":
            canaries[provider] = run_antigravity_canary(args, provider_root)

    verdict, failure_code = classify(canaries)
    artifact = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "provider": args.provider,
        "verdict": verdict,
        "failure_code": failure_code,
        "canaries": canaries,
        "evidence_root": str(evidence_root),
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0 if verdict == "green" else 1


if __name__ == "__main__":
    raise SystemExit(main())
