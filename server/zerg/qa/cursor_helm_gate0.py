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
import traceback
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
    "gate_permission": os.environ.get("LONGHOUSE_CURSOR_GATE0_PERMISSION", ""),
}
line = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
events_path = os.environ["LONGHOUSE_CURSOR_GATE0_EVENTS"]
fd = os.open(events_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
try:
    os.write(fd, line)
finally:
    os.close(fd)

gate_permission = os.environ.get("LONGHOUSE_CURSOR_GATE0_PERMISSION", "")
if event in {"beforeShellExecution", "beforeMCPExecution"} and gate_permission in {"allow", "deny", "ask"}:
    print(json.dumps({"permission": gate_permission, "user_message": "Longhouse Gate 0"}))
elif event == "sessionStart":
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
                    "failClosed": event in {"beforeShellExecution", "beforeMCPExecution"},
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
    generation_id: str | None = None,
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
            if generation_id is not None and row.get("generation_id") != generation_id:
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

    def interrupt(self) -> None:
        if not self.alive():
            raise RuntimeError(f"Cursor process exited before Ctrl-C ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, b"\x03")

    def submit_active(self, text: str) -> None:
        if not self.alive():
            raise RuntimeError(f"Cursor process exited before active steer ({self.process.returncode})")
        with self._write_lock:
            os.write(self.master_fd, text.encode("utf-8"))
            time.sleep(_INJECT_TEXT_SETTLE_SECONDS)
            os.write(self.master_fd, b"\r")

    def close(self) -> None:
        self._stop_reader.set()
        if self.alive():
            process_group: int | None = None
            try:
                process_group = os.getpgid(self.process.pid)
                os.killpg(process_group, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if process_group is None:
                        process_group = os.getpgid(self.process.pid)
                    os.killpg(process_group, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
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


def _trust_workspace(
    *,
    binary: str,
    workspace: Path,
    events_path: Path,
    model: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Use Cursor's supported headless trust flag before opening the TUI."""
    longhouse_session_id = str(uuid4())
    marker = f"LONGHOUSE_CURSOR_GATE0_TRUST_{uuid4().hex[:10]}"
    argv = [
        binary,
        "--print",
        "--trust",
        "--mode",
        "ask",
        "--workspace",
        str(workspace),
    ]
    if model:
        argv.extend(["--model", model])
    argv.append(f"Reply with exactly {marker} and nothing else.")
    result = subprocess.run(
        argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Cursor workspace trust preflight failed ({result.returncode}): {detail}")
    return {
        "status": "passed",
        "longhouse_session_id": longhouse_session_id,
        "response_digest_present": bool(result.stdout.strip()),
    }


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
    launch_marker = f"LONGHOUSE_CURSOR_GATE0_BOOT_{name.upper()}_{uuid4().hex[:10]}"
    argv = [binary, *launch_args, "--workspace", str(workspace), "--force"]
    if model:
        argv.extend(["--model", model])
    argv.append(f"Reply with exactly {launch_marker} and nothing else.")
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
        boot_event_count = len(read_hook_events(events_path))
        wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            after_count=max(0, boot_event_count - 1),
            timeout=timeout,
        )
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
        facts = {
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
        required = {
            "store_identity": facts["store_agent_id"] == provider_id,
            "process_alive_after_turn": facts["process_alive_after_turn"] is True,
            "prompt_digest_matches": facts["prompt_digest_matches"] is True,
            "response_digest_present": facts["response_digest_present"] is True,
        }
        failed = [name for name, passed in required.items() if not passed]
        if failed:
            raise RuntimeError(f"Cursor identity scenario {name} failed proof: {', '.join(failed)}")
        return facts
    finally:
        session.close()


def _cancel_scenario(
    *,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    provider_id: str,
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    longhouse_session_id = str(uuid4())
    argv = [
        binary,
        "--resume",
        provider_id,
        "--workspace",
        str(workspace),
        "--force",
    ]
    if model:
        argv.extend(["--model", model])
    argv.append("Run exactly the shell command `sleep 30`, then reply with DONE.")
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        terminal_path=terminal_path,
    )
    try:
        before = len(read_hook_events(events_path))
        shell = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeShellExecution",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        generation_id = str(shell.get("generation_id") or "")
        session.interrupt()
        stop = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        if stop.get("generation_id") != generation_id:
            raise RuntimeError("Ctrl-C stopped a different Cursor generation")
        if stop.get("status") == "completed" and stop.get("is_interrupt") is not True:
            raise RuntimeError("Ctrl-C did not report provider interruption semantics")
        if not session.alive():
            raise RuntimeError("Ctrl-C exited the Cursor TUI")
        time.sleep(0.25)
        leaked_response = next(
            (
                row
                for row in read_hook_events(events_path)[before:]
                if row.get("event") == "afterAgentResponse" and row.get("generation_id") == generation_id
            ),
            None,
        )
        if leaked_response is not None:
            raise RuntimeError("Ctrl-C stopped the tool but allowed the cancelled generation to respond")

        marker = f"LONGHOUSE_CURSOR_GATE0_AFTER_CANCEL_{uuid4().hex[:10]}"
        turn_start = len(read_hook_events(events_path))
        session.submit_idle(f"Reply with exactly {marker} and nothing else.")
        prompt = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeSubmitPrompt",
            conversation_id=provider_id,
            after_count=turn_start,
            timeout=timeout,
        )
        completed = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            generation_id=str(prompt.get("generation_id") or ""),
            after_count=turn_start,
            timeout=timeout,
        )
        if completed.get("status") != "completed":
            raise RuntimeError("Cursor did not complete the post-cancel turn")
        return {
            "status": "passed",
            "provider_conversation_id": provider_id,
            "longhouse_session_id": longhouse_session_id,
            "cancel_generation_id": generation_id,
            "cancel_status": stop.get("status"),
            "cancel_is_interrupt": stop.get("is_interrupt"),
            "process_alive_after_cancel": session.alive(),
            "next_generation_id": prompt.get("generation_id"),
            "next_turn_completed": True,
        }
    finally:
        session.close()


def _resume_scenario(
    *,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    provider_id: str,
    longhouse_session_id: str,
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    marker = f"LONGHOUSE_CURSOR_GATE0_RESUME_{uuid4().hex[:10]}"
    argv = [binary, "--resume", provider_id, "--workspace", str(workspace), "--force"]
    if model:
        argv.extend(["--model", model])
    argv.append(f"Reply with exactly {marker} and nothing else.")
    before = len(read_hook_events(events_path))
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        terminal_path=terminal_path,
    )
    try:
        prompt = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeSubmitPrompt",
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
        return {
            "status": "passed",
            "provider_conversation_id": provider_id,
            "longhouse_session_id": longhouse_session_id,
            "generation_id": prompt.get("generation_id"),
            "stop_status": stop.get("status"),
            "store_agent_id": _cursor_store_agent_id(wait_for_store(provider_id, timeout=timeout)),
            "process_alive_after_turn": session.alive(),
        }
    finally:
        session.close()


def _permission_scenario(
    *,
    decision: str,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    provider_id: str,
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    longhouse_session_id = str(uuid4())
    marker_file = workspace / f"permission-{decision}.txt"
    argv = [binary, "--resume", provider_id, "--workspace", str(workspace)]
    if model:
        argv.extend(["--model", model])
    argv.append(f"Run exactly `printf ALLOWED > {marker_file}` once, then report the result.")
    env = _child_env(longhouse_session_id, events_path)
    env["LONGHOUSE_CURSOR_GATE0_PERMISSION"] = decision
    before = len(read_hook_events(events_path))
    session = CursorPtySession.start(argv=argv, cwd=workspace, env=env, terminal_path=terminal_path)
    try:
        shell = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeShellExecution",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        if decision == "allow":
            wait_for_hook(
                events_path,
                longhouse_session_id=longhouse_session_id,
                event="afterShellExecution",
                conversation_id=provider_id,
                after_count=before,
                timeout=timeout,
            )
            if not marker_file.exists():
                raise RuntimeError("Cursor ignored permission=allow")
        else:
            time.sleep(1)
            if marker_file.exists():
                raise RuntimeError(f"Cursor executed shell after permission={decision}")
        return {
            "status": "passed",
            "decision": decision,
            "provider_conversation_id": provider_id,
            "generation_id": shell.get("generation_id"),
            "side_effect_present": marker_file.exists(),
            "process_alive": session.alive(),
        }
    finally:
        session.close()


def _active_steer_scenario(
    *,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    provider_id: str,
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    longhouse_session_id = str(uuid4())
    argv = [binary, "--resume", provider_id, "--workspace", str(workspace), "--force"]
    if model:
        argv.extend(["--model", model])
    argv.append("Run exactly `sleep 8`, then reply with exactly ORIGINAL and nothing else.")
    before = len(read_hook_events(events_path))
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        terminal_path=terminal_path,
    )
    try:
        shell = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeShellExecution",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        generation_id = str(shell.get("generation_id") or "")
        session.submit_active("Instead, reply with exactly STEERED and nothing else.")
        response = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="afterAgentResponse",
            conversation_id=provider_id,
            generation_id=generation_id,
            after_count=before,
            timeout=timeout,
        )
        steered = response.get("text_sha256") == _marker_digest("STEERED")
        return {
            "status": "passed" if steered else "unsupported",
            "provider_conversation_id": provider_id,
            "generation_id": generation_id,
            "same_generation_response": True,
            "response_was_steered": steered,
            "process_alive": session.alive(),
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
        report["scenarios"]["workspace_trust"] = _trust_workspace(
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            model=args.model,
            timeout=args.timeout,
        )
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
        first_identity = report["scenarios"]["create_chat_resume"]
        report["scenarios"]["native_resume_continuity"] = _resume_scenario(
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            terminal_path=artifact_root / "native-resume.terminal.raw",
            provider_id=provider_id,
            longhouse_session_id=str(first_identity["longhouse_session_id"]),
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
        cancel_provider_id = _create_chat(binary, workspace)
        report["scenarios"]["ctrl_c_cancel"] = _cancel_scenario(
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            terminal_path=artifact_root / "ctrl-c-cancel.terminal.raw",
            provider_id=cancel_provider_id,
            timeout=args.timeout,
            model=args.model,
        )
        for decision in ("allow", "deny", "ask"):
            permission_provider_id = _create_chat(binary, workspace)
            report["scenarios"][f"permission_{decision}"] = _permission_scenario(
                decision=decision,
                binary=binary,
                workspace=workspace,
                events_path=events_path,
                terminal_path=artifact_root / f"permission-{decision}.terminal.raw",
                provider_id=permission_provider_id,
                timeout=args.timeout,
                model=args.model,
            )
        steer_provider_id = _create_chat(binary, workspace)
        report["scenarios"]["active_steer"] = _active_steer_scenario(
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            terminal_path=artifact_root / "active-steer.terminal.raw",
            provider_id=steer_provider_id,
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
        report["traceback"] = traceback.format_exc()
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
