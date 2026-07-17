"""Live stock-Cursor proof harness for Cursor Helm capability promotion.

The harness owns a real PTY but treats terminal bytes as liveness evidence
only. Cursor hooks, provider-native store metadata, and process identity are
the assertion surfaces. It intentionally bypasses Runtime Host registration so
provider behavior can be proven before Longhouse advertises it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import tempfile
import termios
import threading
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

_DEFAULT_TIMEOUT_SECONDS = 90.0
_INJECT_TEXT_SETTLE_SECONDS = 0.3
_INJECT_ESCAPE_SETTLE_SECONDS = 0.1
_HOOK_EVENTS = (
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "afterAgentThought",
    "afterAgentResponse",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "beforeShellExecution",
    "afterShellExecution",
    "stop",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _marker_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cursor_binary(configured: str | None) -> str:
    value = (configured or "").strip()
    if value:
        return value
    found = shutil.which("cursor-agent")
    if not found:
        raise RuntimeError("cursor-agent was not found on PATH")
    return found


def _run_json(argv: list[str], *, cwd: Path, timeout: float = 15.0) -> dict[str, Any]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(argv)}: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {' '.join(argv)}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"command returned a non-object JSON value: {' '.join(argv)}")
    return parsed


def _provider_version(binary: str, cwd: Path) -> str:
    result = subprocess.run(
        [binary, "--version"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cursor-agent --version failed")
    return result.stdout.strip()


def _create_chat(binary: str, cwd: Path) -> str:
    result = subprocess.run(
        [binary, "create-chat"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cursor-agent create-chat failed: {(result.stderr or result.stdout).strip()}")
    value = result.stdout.strip()
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise RuntimeError(f"cursor-agent create-chat returned an invalid UUID: {value!r}") from exc


def _decode_cursor_meta_value(value: object) -> dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) % 2 == 0:
            try:
                raw = bytes.fromhex(text)
            except ValueError:
                raw = text.encode("utf-8")
        else:
            raw = text.encode("utf-8")
    else:
        raise ValueError("Cursor meta value is neither text nor bytes")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Cursor meta value is not a JSON object")
    return parsed


def _cursor_store_agent_id(path: Path) -> str | None:
    uri = f"file:{path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        value = str(_decode_cursor_meta_value(row[0]).get("agentId") or "").strip()
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return value or None


def find_cursor_store(agent_id: str, *, root: Path | None = None) -> Path | None:
    chats_root = root or (Path.home() / ".cursor" / "chats")
    if not chats_root.exists():
        return None
    for path in chats_root.glob("*/*/store.db"):
        if _cursor_store_agent_id(path) == agent_id:
            return path
    return None


def _hook_script() -> str:
    return r"""#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {"_invalid": True}
if not isinstance(payload, dict):
    payload = {"_invalid": True}

def digest(value):
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

row = {
    "observed_at": datetime.now(timezone.utc).isoformat(),
    "event": event,
    "conversation_id": str(payload.get("conversation_id") or ""),
    "generation_id": str(payload.get("generation_id") or ""),
    "model": str(payload.get("model") or ""),
    "longhouse_session_id": os.environ.get("LONGHOUSE_SESSION_ID", ""),
    "hook_pid": os.getpid(),
    "cwd": str(payload.get("cwd") or ""),
    "tool_name": str(payload.get("tool_name") or ""),
    "status": str(payload.get("status") or ""),
    "is_interrupt": payload.get("is_interrupt"),
    "prompt_sha256": digest(payload.get("prompt")),
    "text_sha256": digest(payload.get("text")),
    "command_sha256": digest(payload.get("command")),
}
line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
events_path = os.environ["LONGHOUSE_CURSOR_GATE0_EVENTS"]
fd = os.open(events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
try:
    os.write(fd, line)
finally:
    os.close(fd)

if event == "sessionStart":
    print(json.dumps({"continue": True}))
elif event == "beforeSubmitPrompt":
    print(json.dumps({"continue": True}))
else:
    print("{}")
"""


def write_project_hooks(workspace: Path, events_path: Path) -> Path:
    cursor_dir = workspace / ".cursor"
    hooks_dir = cursor_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = hooks_dir / "longhouse-gate0-hook.py"
    script.write_text(_hook_script(), encoding="utf-8")
    script.chmod(0o755)
    hooks = {
        "version": 1,
        "hooks": {
            event: [
                {
                    "command": f"{script} {event}",
                    "timeout": 5,
                    "failClosed": False,
                }
            ]
            for event in _HOOK_EVENTS
        },
    }
    (cursor_dir / "hooks.json").write_text(json.dumps(hooks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.touch(mode=0o600, exist_ok=True)
    return script


def read_hook_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def wait_for_hook(
    path: Path,
    *,
    longhouse_session_id: str,
    event: str | None = None,
    conversation_id: str | None = None,
    after_count: int = 0,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = read_hook_events(path)
        for row in events[after_count:]:
            if row.get("longhouse_session_id") != longhouse_session_id:
                continue
            if event is not None and row.get("event") != event:
                continue
            if conversation_id is not None and row.get("conversation_id") != conversation_id:
                continue
            return row
        time.sleep(0.1)
    raise TimeoutError(f"Cursor hook did not produce event={event!r} conversation_id={conversation_id!r}")


def wait_for_store(agent_id: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = find_cursor_store(agent_id)
        if path is not None:
            return path
        time.sleep(0.2)
    raise TimeoutError(f"Cursor store was not created for agent {agent_id}")


@dataclass
class CursorPtySession:
    process: subprocess.Popen[bytes]
    master_fd: int
    terminal_path: Path
    _reader: threading.Thread
    _stop_reader: threading.Event
    _write_lock: threading.Lock

    @classmethod
    def start(
        cls,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        terminal_path: Path,
    ) -> CursorPtySession:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 132, 0, 0))

        def child_setup() -> None:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=child_setup,
        )
        os.close(slave_fd)
        stop_reader = threading.Event()
        terminal_path.parent.mkdir(parents=True, exist_ok=True)

        def drain() -> None:
            with terminal_path.open("ab", buffering=0) as output:
                while not stop_reader.is_set():
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.2)
                    except (OSError, ValueError):
                        return
                    if not ready:
                        if process.poll() is not None:
                            return
                        continue
                    try:
                        chunk = os.read(master_fd, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    output.write(chunk)

        reader = threading.Thread(target=drain, daemon=True, name="cursor-gate0-terminal-drain")
        reader.start()
        return cls(
            process=process,
            master_fd=master_fd,
            terminal_path=terminal_path,
            _reader=reader,
            _stop_reader=stop_reader,
            _write_lock=threading.Lock(),
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def submit_idle(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"Cursor process exited before submit ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, text.encode("utf-8"))
            time.sleep(_INJECT_TEXT_SETTLE_SECONDS)
            os.write(self.master_fd, b"\x1b")
            time.sleep(_INJECT_ESCAPE_SETTLE_SECONDS)
            os.write(self.master_fd, b"\r")

    def close(self) -> None:
        self._stop_reader.set()
        if self.alive():
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self._reader.join(timeout=2)


def _child_env(longhouse_session_id: str, events_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LONGHOUSE_SESSION_ID"] = longhouse_session_id
    env["LONGHOUSE_CURSOR_GATE0_EVENTS"] = str(events_path)
    for key in (
        "CI",
        "CONTINUOUS_INTEGRATION",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "CIRCLECI",
        "TRAVIS",
        "BUILDKITE",
        "TEAMCITY_VERSION",
        "BUILD_NUMBER",
        "BUILD_ID",
        "BITBUCKET_BUILD_NUMBER",
        "JENKINS_URL",
    ):
        env.pop(key, None)
    env["TERM"] = env.get("TERM") if env.get("TERM") not in {None, "", "dumb"} else "xterm-256color"
    env["LINES"] = "40"
    env["COLUMNS"] = "132"
    return env


def _identity_scenario(
    *,
    name: str,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    provider_id: str,
    launch_args: list[str],
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    longhouse_session_id = str(uuid4())
    argv = [binary, *launch_args, "--workspace", str(workspace), "--force"]
    if model:
        argv.extend(["--model", model])
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        terminal_path=terminal_path,
    )
    started_at = _now()
    try:
        first = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            conversation_id=provider_id,
            timeout=timeout,
        )
        store = wait_for_store(provider_id, timeout=timeout)
        marker = f"LONGHOUSE_CURSOR_GATE0_{name.upper()}_{uuid4().hex[:10]}"
        before = len(read_hook_events(events_path))
        session.submit_idle(f"Reply with exactly {marker} and nothing else.")
        prompt = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeSubmitPrompt",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        response = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="afterAgentResponse",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        stop = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        expected_prompt_digest = _marker_digest(f"Reply with exactly {marker} and nothing else.")
        return {
            "status": "passed",
            "started_at": started_at,
            "finished_at": _now(),
            "longhouse_session_id": longhouse_session_id,
            "provider_conversation_id": provider_id,
            "store_agent_id": _cursor_store_agent_id(store),
            "store_db": str(store),
            "cursor_pid": session.process.pid,
            "process_alive_after_turn": session.alive(),
            "first_hook_event": first.get("event"),
            "prompt_digest_matches": prompt.get("prompt_sha256") == expected_prompt_digest,
            "response_digest_present": bool(response.get("text_sha256")),
            "stop_status": stop.get("status"),
        }
    finally:
        session.close()


def run_gate0(args: argparse.Namespace) -> dict[str, Any]:
    binary = _cursor_binary(args.cursor_bin)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_root = Path(args.artifact_root).expanduser() / timestamp
    artifact_root.mkdir(parents=True, exist_ok=False)
    workspace = Path(tempfile.mkdtemp(prefix="workspace-", dir=artifact_root))
    (workspace / "README.md").write_text("# Longhouse Cursor Helm Gate 0\n", encoding="utf-8")
    events_path = artifact_root / "events.ndjson"
    write_project_hooks(workspace, events_path)
    version = _provider_version(binary, workspace)
    auth = _run_json([binary, "status", "--format", "json"], cwd=workspace)
    report: dict[str, Any] = {
        "schema_version": 1,
        "gate": "cursor_helm_gate0",
        "provider": "cursor",
        "provider_version": version,
        "longhouse_commit": _git_commit(),
        "started_at": _now(),
        "artifact_root": str(artifact_root),
        "workspace": str(workspace),
        "mutated_user_hooks": False,
        "auth": {
            "status": auth.get("status"),
            "is_authenticated": auth.get("isAuthenticated") is True,
        },
        "scenarios": {},
    }
    output_path = artifact_root / "gate0.json"
    try:
        if auth.get("isAuthenticated") is not True:
            raise RuntimeError("cursor-agent is not authenticated")
        provider_id = _create_chat(binary, workspace)
        report["scenarios"]["create_chat_resume"] = _identity_scenario(
            name="create_chat_resume",
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            terminal_path=artifact_root / "create-chat-resume.terminal.raw",
            provider_id=provider_id,
            launch_args=["--resume", provider_id],
            timeout=args.timeout,
            model=args.model,
        )
        requested_id = str(uuid4())
        report["scenarios"]["new_session_id"] = _identity_scenario(
            name="new_session_id",
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            terminal_path=artifact_root / "new-session-id.terminal.raw",
            provider_id=requested_id,
            launch_args=["--new-session-id", requested_id],
            timeout=args.timeout,
            model=args.model,
        )
        report["selected_identity_path"] = "create_chat_resume"
        report["status"] = "passed"
        report["failure_code"] = None
    except Exception as exc:
        report["status"] = "failed"
        report["failure_code"] = type(exc).__name__
        report["error"] = str(exc)
    report["finished_at"] = _now()
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _git_commit() -> str | None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cursor-bin", help="Explicit stock cursor-agent binary")
    parser.add_argument("--model", help="Optional model override for low-cost proof turns")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--artifact-root",
        default=str(Path.home() / ".longhouse" / "canaries" / "provider-live" / "cursor"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_gate0(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
