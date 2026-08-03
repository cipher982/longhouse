"""Live stock-Cursor proof harness for Cursor Helm capability promotion.

The harness owns a real PTY but treats terminal bytes as liveness evidence
only. Cursor hooks, provider-native store metadata, and process identity are
the assertion surfaces. Most scenarios bypass Runtime Host registration;
conversation reset uses the production Helm wrapper so alias rotation and
source binding are measured too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
from datetime import UTC
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from zerg.qa.conversation_reset import classify_identity_transition
from zerg.qa.conversation_reset import evaluate_reset_observation
from zerg.qa.conversation_reset import longhouse_provider_aliases
from zerg.qa.conversation_reset import longhouse_source_binding
from zerg.qa.pty_session import ProviderPtySession as CursorPtySession

_DEFAULT_TIMEOUT_SECONDS = 90.0
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

_ARTIFACT_SECRET_PATTERNS = (
    (re.compile(rb"sk-ant-api\d{2}-[A-Za-z0-9_-]+"), b"sk-ant-<redacted>"),
    (re.compile(rb"sk-or-v1-[A-Za-z0-9_-]+"), b"sk-or-v1-<redacted>"),
    (re.compile(rb"crsr_[A-Za-z0-9]+"), b"crsr_<redacted>"),
    (re.compile(rb"sk-[A-Za-z0-9_-]{20,}"), b"sk-<redacted>"),
)


def _artifact_secret_values() -> tuple[bytes, ...]:
    names = (
        "CURSOR_API_KEY",
        "CURSOR_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "CODEX_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "ZAI_API_KEY",
    )
    return tuple(
        sorted(
            {value.encode("utf-8") for name in names if (value := str(os.environ.get(name) or "").strip())},
            key=len,
            reverse=True,
        )
    )


def _scrub_artifact_tree(root: Path) -> None:
    """Keep retained Gate 0 evidence free of provider credentials."""

    exact_values = _artifact_secret_values()
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            original = path.read_bytes()
        except OSError:
            continue
        redacted = original
        for secret in exact_values:
            redacted = redacted.replace(secret, b"<provider-secret-redacted>")
        for pattern, replacement in _ARTIFACT_SECRET_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != original:
            path.write_bytes(redacted)


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


def wait_for_hook_match(
    path: Path,
    *,
    longhouse_session_id: str,
    predicate: Any,
    after_count: int = 0,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait for a structured hook row matching a provider-specific predicate."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in read_hook_events(path)[after_count:]:
            if row.get("longhouse_session_id") != longhouse_session_id:
                continue
            if predicate(row):
                return row
        time.sleep(0.1)
    raise TimeoutError("Cursor hook did not produce the requested structured event")


def wait_for_store(agent_id: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = find_cursor_store(agent_id)
        if path is not None:
            return path
        time.sleep(0.2)
    raise TimeoutError(f"Cursor store was not created for agent {agent_id}")


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
    probe_prompt: str | None = None,
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
        probe_text = probe_prompt or f"Reply with exactly LONGHOUSE_CURSOR_GATE0_{name.upper()}_{uuid4().hex[:10]} and nothing else."
        before = len(read_hook_events(events_path))
        session.submit_idle(probe_text)
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
        expected_prompt_digest = _marker_digest(probe_text)
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
            "prompt_sha256": prompt.get("prompt_sha256"),
            "expected_prompt_sha256": expected_prompt_digest,
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


def _conversation_reset_scenario(
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
    marker_a = f"LONGHOUSE_CURSOR_RESET_A_{uuid4().hex[:10]}"
    marker_b = f"LONGHOUSE_CURSOR_RESET_B_{uuid4().hex[:10]}"
    prompt_a = f"Reply with exactly {marker_a} and nothing else."
    prompt_b = f"Reply with exactly {marker_b} and nothing else."
    argv = [binary, "--resume", provider_id, "--workspace", str(workspace), "--force"]
    if model:
        argv.extend(["--model", model])
    argv.append(prompt_a)
    before = len(read_hook_events(events_path))
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env(longhouse_session_id, events_path),
        terminal_path=terminal_path,
    )
    try:
        prompt_a_event = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeSubmitPrompt",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            generation_id=str(prompt_a_event.get("generation_id") or ""),
            after_count=before,
            timeout=timeout,
        )
        old_store = wait_for_store(provider_id, timeout=timeout)
        reset_start = len(read_hook_events(events_path))
        session.submit_idle("/clear")

        eager_deadline = time.monotonic() + min(3.0, timeout)
        eager_event: dict[str, Any] | None = None
        while time.monotonic() < eager_deadline and eager_event is None:
            eager_event = next(
                (
                    row
                    for row in read_hook_events(events_path)[reset_start:]
                    if row.get("longhouse_session_id") == longhouse_session_id
                    and row.get("event") == "sessionStart"
                    and row.get("conversation_id")
                    and row.get("conversation_id") != provider_id
                ),
                None,
            )
            if eager_event is None:
                time.sleep(0.1)

        marker_b_start = len(read_hook_events(events_path))
        session.submit_idle(prompt_b)
        prompt_b_event = wait_for_hook_match(
            events_path,
            longhouse_session_id=longhouse_session_id,
            after_count=marker_b_start,
            timeout=timeout,
            predicate=lambda row: row.get("event") == "beforeSubmitPrompt"
            and row.get("conversation_id")
            and row.get("conversation_id") != provider_id
            and row.get("prompt_sha256") == _marker_digest(prompt_b),
        )
        new_provider_id = str(prompt_b_event.get("conversation_id") or "")
        response_b = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="afterAgentResponse",
            conversation_id=new_provider_id,
            generation_id=str(prompt_b_event.get("generation_id") or ""),
            after_count=marker_b_start,
            timeout=timeout,
        )
        stop_b = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=new_provider_id,
            generation_id=str(prompt_b_event.get("generation_id") or ""),
            after_count=marker_b_start,
            timeout=timeout,
        )
        new_store = wait_for_store(new_provider_id, timeout=timeout)
        reset_events = read_hook_events(events_path)[reset_start:]
        marker_a_digest = _marker_digest(prompt_a)
        reset_boundary = next(
            (row for row in reset_events if row.get("event") == "sessionStart" and row.get("conversation_id") == new_provider_id),
            None,
        )
        copied_marker_a = any(
            row.get("conversation_id") == new_provider_id and row.get("prompt_sha256") == marker_a_digest for row in reset_events
        )
        return {
            "status": "passed",
            "provider_conversation_id": new_provider_id,
            "longhouse_session_id": longhouse_session_id,
            "reset_command": "/clear",
            "reset_command_accepted": (reset_boundary is not None and response_b.get("conversation_id") == new_provider_id),
            "identity_transition": classify_identity_transition(provider_id, new_provider_id),
            "identity_allocation": "eager" if eager_event is not None else "lazy",
            "before": {
                "provider_session_id": provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": str(session.process.pid),
                "run_id": str(prompt_a_event.get("generation_id") or ""),
                "raw_source_ids": [str(old_store)],
                "raw_source_hashes": [_file_sha256(old_store)],
                "marker_digest": marker_a_digest,
            },
            "after": {
                "provider_session_id": new_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": str(session.process.pid),
                "run_id": str(prompt_b_event.get("generation_id") or ""),
                "raw_source_ids": [str(new_store)],
                "raw_source_hashes": [_file_sha256(new_store)],
                "marker_digest": _marker_digest(prompt_b),
            },
            "provider_transition": {
                "pre_reset_history_retained": old_store.exists() and _cursor_store_agent_id(old_store) == provider_id,
                "post_reset_turn_bound_to_active_identity": response_b.get("conversation_id") == new_provider_id
                and stop_b.get("conversation_id") == new_provider_id,
                "pre_reset_messages_not_copied": not copied_marker_a,
            },
            "archive": {
                "pre_reset_raw_preserved": old_store.exists(),
                "post_reset_raw_preserved": new_store.exists(),
                "reset_boundary_observable": reset_boundary is not None,
                "tail_marker_order": [marker_a_digest, "reset", _marker_digest(prompt_b)] if reset_boundary is not None else [],
                "source_identity_preserved": old_store != new_store,
                "archive_evidence_source": "cursor_hooks_and_store",
                "archive_artifact": str(events_path),
            },
            "longhouse": {
                "provider_alias_ids": [provider_id],
                "timeline_session_ids": [longhouse_session_id],
                "provider_alias_matches_before": True,
                "provider_alias_matches_after": provider_id == new_provider_id,
            },
            "process_alive_after_reset": session.alive(),
        }
    finally:
        session.close()


def _managed_reset_registration_payload(body: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "session_id": str(body.get("session_id") or ""),
        "run_id": run_id,
        "managed_transport": "cursor_helm",
        "hook_token": "test-hook-authority",
        "coordination_token": "test-coordination-authority",
    }


def _managed_reset_outcome_payload() -> dict[str, bool]:
    return {"recorded": True}


def _managed_conversation_reset_scenario(
    *,
    binary: str,
    workspace: Path,
    events_path: Path,
    terminal_path: Path,
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    engine = shutil.which("longhouse-engine")
    if not engine:
        raise RuntimeError("longhouse-engine was not found on PATH")
    hook_configuration = subprocess.run(
        [engine, "cursor-helm", "configure-hooks"],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if hook_configuration.returncode != 0:
        detail = (hook_configuration.stderr or hook_configuration.stdout).strip()
        raise RuntimeError(f"native Cursor hook configuration failed: {detail}")

    marker_a = f"LONGHOUSE_CURSOR_RESET_A_{uuid4().hex[:10]}"
    marker_b = f"LONGHOUSE_CURSOR_RESET_B_{uuid4().hex[:10]}"
    prompt_a = f"Reply with exactly {marker_a} and nothing else."
    prompt_b = f"Reply with exactly {marker_b} and nothing else."
    registration_run_id = str(uuid4())

    class RegistrationHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except json.JSONDecodeError:
                body = {}
            if self.path == "/api/sessions/managed-local/this-device":
                payload = _managed_reset_registration_payload(body, registration_run_id)
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
            elif self.path.endswith("/launch-outcome"):
                encoded = json.dumps(_managed_reset_outcome_payload()).encode("utf-8")
                self.send_response(200)
            else:
                encoded = b"{}"
                self.send_response(202)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    registration_server = ThreadingHTTPServer(("127.0.0.1", 0), RegistrationHandler)
    registration_thread = threading.Thread(
        target=registration_server.serve_forever,
        name="cursor-reset-registration-stub",
        daemon=True,
    )
    registration_thread.start()
    registration_url = f"http://127.0.0.1:{registration_server.server_address[1]}"
    argv = [
        "longhouse",
        "cursor",
        "--cwd",
        str(workspace),
        "--project",
        "zerg",
        "--name",
        "Cursor conversation reset qualification",
        "--cursor-bin",
        binary,
        "--permission-mode",
        "auto_approve",
        "--url",
        registration_url,
        "--token",
        "test-device-authority",
        "--",
    ]
    if model:
        argv.extend(["--model", model])
    argv.append(prompt_a)
    before = len(read_hook_events(events_path))
    started_at = time.time()
    session = CursorPtySession.start(
        argv=argv,
        cwd=workspace,
        env=_child_env("", events_path),
        terminal_path=terminal_path,
    )
    state_root = Path.home() / ".longhouse" / "managed-local" / "cursor-helm"

    def wait_value(predicate, message: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if not session.alive():
                raise RuntimeError(f"managed Cursor exited before {message} ({session.process.returncode})")
            time.sleep(0.2)
        raise TimeoutError(message)

    def launched_state():
        for path in state_root.glob("*.json"):
            try:
                if path.stat().st_mtime < started_at - 1:
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                payload.get("ready") is True
                and Path(str(payload.get("cwd") or "")).resolve() == workspace
                and str(payload.get("provider_session_id") or "")
            ):
                return path, payload
        return None

    try:
        state_path, initial_state = wait_value(launched_state, "Longhouse Cursor Helm state did not become ready")
        longhouse_session_id = str(initial_state.get("session_id") or state_path.stem)
        provider_id = str(initial_state.get("provider_session_id") or "")
        provider_pid = str(initial_state.get("cursor_pid") or session.process.pid)
        prompt_a_event = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="beforeSubmitPrompt",
            conversation_id=provider_id,
            after_count=before,
            timeout=timeout,
        )
        wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="afterAgentResponse",
            conversation_id=provider_id,
            generation_id=str(prompt_a_event.get("generation_id") or ""),
            after_count=before,
            timeout=timeout,
        )
        wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=provider_id,
            generation_id=str(prompt_a_event.get("generation_id") or ""),
            after_count=before,
            timeout=timeout,
        )
        old_store = wait_for_store(provider_id, timeout=timeout)
        reset_start = len(read_hook_events(events_path))
        session.submit_idle("/clear")
        eager_deadline = time.monotonic() + min(3.0, timeout)
        eager_event: dict[str, Any] | None = None
        while time.monotonic() < eager_deadline and eager_event is None:
            eager_event = next(
                (
                    row
                    for row in read_hook_events(events_path)[reset_start:]
                    if row.get("longhouse_session_id") == longhouse_session_id
                    and row.get("event") == "sessionStart"
                    and row.get("conversation_id")
                    and row.get("conversation_id") != provider_id
                ),
                None,
            )
            if eager_event is None:
                time.sleep(0.1)

        marker_b_start = len(read_hook_events(events_path))
        session.submit_idle(prompt_b)
        prompt_b_event = wait_for_hook_match(
            events_path,
            longhouse_session_id=longhouse_session_id,
            after_count=marker_b_start,
            timeout=timeout,
            predicate=lambda row: row.get("event") == "beforeSubmitPrompt"
            and row.get("conversation_id")
            and row.get("conversation_id") != provider_id
            and row.get("prompt_sha256") == _marker_digest(prompt_b),
        )
        new_provider_id = str(prompt_b_event.get("conversation_id") or "")
        response_b = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="afterAgentResponse",
            conversation_id=new_provider_id,
            generation_id=str(prompt_b_event.get("generation_id") or ""),
            after_count=marker_b_start,
            timeout=timeout,
        )
        stop_b = wait_for_hook(
            events_path,
            longhouse_session_id=longhouse_session_id,
            event="stop",
            conversation_id=new_provider_id,
            generation_id=str(prompt_b_event.get("generation_id") or ""),
            after_count=marker_b_start,
            timeout=timeout,
        )
        new_store = wait_for_store(new_provider_id, timeout=timeout)

        def rotated_state():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return payload if payload.get("provider_session_id") == new_provider_id else None

        final_state = wait_value(rotated_state, "Longhouse did not rotate the active Cursor provider identity")
        claim_path = state_root / "binding-probes" / f"{longhouse_session_id}.json"

        def rotated_claim():
            try:
                payload = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            history = payload.get("previous_conversation_uuids") or []
            return payload if payload.get("conversation_uuid") == new_provider_id and provider_id in history else None

        wait_value(rotated_claim, "Longhouse did not preserve the Cursor conversation predecessor")
        bound_session_id = wait_value(
            lambda: longhouse_source_binding("cursor", new_provider_id),
            "Longhouse did not bind the post-reset Cursor source to the managed session",
        )
        aliases = wait_value(
            lambda: (
                values
                if provider_id in (values := longhouse_provider_aliases("cursor", longhouse_session_id)) and new_provider_id in values
                else None
            ),
            "Longhouse did not retain both Cursor provider aliases",
        )
        reset_boundary = provider_id in aliases and final_state.get("provider_session_id") == new_provider_id
        marker_a_digest = _marker_digest(prompt_a)
        marker_b_digest = _marker_digest(prompt_b)
        copied_marker_a = any(
            row.get("conversation_id") == new_provider_id and row.get("prompt_sha256") == marker_a_digest
            for row in read_hook_events(events_path)[reset_start:]
        )
        observation = {
            "status": "passed",
            "provider_conversation_id": new_provider_id,
            "longhouse_session_id": longhouse_session_id,
            "reset_command": "/clear",
            "reset_command_accepted": reset_boundary
            and response_b.get("conversation_id") == new_provider_id
            and stop_b.get("status") == "completed",
            "identity_transition": classify_identity_transition(provider_id, new_provider_id),
            "identity_allocation": "eager" if eager_event is not None else "lazy",
            "before": {
                "provider_session_id": provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": provider_pid,
                "run_id": str(initial_state.get("run_id") or ""),
                "raw_source_ids": [str(old_store)],
                "raw_source_hashes": [_file_sha256(old_store)],
                "marker_digest": marker_a_digest,
            },
            "after": {
                "provider_session_id": new_provider_id,
                "longhouse_session_id": longhouse_session_id,
                "provider_process_id": str(final_state.get("cursor_pid") or provider_pid),
                "run_id": str(final_state.get("run_id") or ""),
                "raw_source_ids": [str(new_store)],
                "raw_source_hashes": [_file_sha256(new_store)],
                "marker_digest": marker_b_digest,
            },
            "provider_transition": {
                "pre_reset_history_retained": old_store.exists() and _cursor_store_agent_id(old_store) == provider_id,
                "post_reset_turn_bound_to_active_identity": response_b.get("conversation_id") == new_provider_id
                and stop_b.get("conversation_id") == new_provider_id,
                "pre_reset_messages_not_copied": not copied_marker_a,
            },
            "archive": {
                "pre_reset_raw_preserved": old_store.exists(),
                "post_reset_raw_preserved": new_store.exists(),
                "reset_boundary_observable": reset_boundary,
                "tail_marker_order": [marker_a_digest, "reset", marker_b_digest] if reset_boundary else [],
                "source_identity_preserved": old_store != new_store,
                "archive_evidence_source": "cursor_hooks_binding_claim_and_store",
                "archive_artifact": str(events_path),
            },
            "longhouse": {
                "provider_alias_ids": list(aliases),
                "timeline_session_ids": [longhouse_session_id],
                "provider_alias_matches_before": provider_id in aliases,
                "provider_alias_matches_after": new_provider_id in aliases,
                "source_bound_session_id": bound_session_id,
                "source_binding_matches": bound_session_id == longhouse_session_id,
            },
            "process_alive_after_reset": session.alive(),
        }
        session.submit_idle("/exit")
        return observation
    finally:
        session.close()
        registration_server.shutdown()
        registration_server.server_close()
        registration_thread.join(timeout=2.0)


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
    resolved_binary = Path(binary).expanduser().resolve(strict=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_root = Path(args.artifact_root).expanduser() / timestamp
    artifact_root.mkdir(parents=True, exist_ok=False)
    workspace = Path(tempfile.mkdtemp(prefix="workspace-", dir=artifact_root))
    (workspace / "README.md").write_text("# Longhouse Cursor Helm Gate 0\n", encoding="utf-8")
    events_path = artifact_root / "events.ndjson"
    write_project_hooks(workspace, events_path)
    version: str | None = None
    auth: dict[str, Any] = {}
    report: dict[str, Any] = {
        "schema_version": 1,
        "gate": "cursor_helm_gate0",
        "provider": "cursor",
        "provider_version": version,
        "provider_bin": str(resolved_binary),
        "provider_executable_identity": f"sha256:{_file_sha256(resolved_binary)}",
        "longhouse_commit": _git_commit(),
        "started_at": _now(),
        "artifact_root": str(artifact_root),
        "workspace": str(workspace),
        "mutated_user_hooks": False,
        "auth": {
            "status": None,
            "is_authenticated": False,
        },
        "scenarios": {},
    }
    output_path = artifact_root / "gate0.json"

    def write_report() -> dict[str, Any]:
        report["finished_at"] = _now()
        if report.get("scenarios"):
            try:
                report["native_evidence"] = _snapshot_native_evidence(report, artifact_root)
            except (OSError, RuntimeError) as exc:
                if report.get("status") == "passed":
                    raise
                report["native_evidence_failure"] = f"{type(exc).__name__}: {exc}"
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _scrub_artifact_tree(artifact_root)
        _refresh_native_evidence_receipts(report, artifact_root)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _scrub_artifact_tree(artifact_root)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def record_reset_observation(value: dict[str, Any]) -> dict[str, Any]:
        observation = {
            **value,
            "schema_version": 1,
            "scenario": "conversation_reset",
            "provider": "cursor",
            "evidence_class": "live_token",
            "provider_version": version,
            "provider_executable_identity": report["provider_executable_identity"],
        }
        reset_path = artifact_root / "conversation-reset-observation.json"
        reset_path.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["conversation_reset_observation_path"] = str(reset_path)
        report["conversation_reset_evaluation"] = evaluate_reset_observation(observation)
        if report["conversation_reset_evaluation"]["status"] != "pass":
            raise RuntimeError(
                "Cursor conversation-reset semantic assertions failed: "
                + ", ".join(report["conversation_reset_evaluation"]["failed_assertions"])
            )
        return observation

    try:
        version = _provider_version(binary, workspace)
        auth = _run_json([binary, "status", "--format", "json"], cwd=workspace)
        report["provider_version"] = version
        report["auth"] = {
            "status": auth.get("status"),
            "is_authenticated": auth.get("isAuthenticated") is True,
        }
        if auth.get("isAuthenticated") is not True:
            raise RuntimeError("cursor-agent is not authenticated")
        report["scenarios"]["workspace_trust"] = _trust_workspace(
            binary=binary,
            workspace=workspace,
            events_path=events_path,
            model=args.model,
            timeout=args.timeout,
        )
        if args.conversation_reset_only:
            report["scenarios"]["conversation_reset"] = record_reset_observation(
                _managed_conversation_reset_scenario(
                    binary=binary,
                    workspace=workspace,
                    events_path=events_path,
                    terminal_path=artifact_root / "conversation-reset.terminal.raw",
                    timeout=args.timeout,
                    model=args.model,
                )
            )
            report["selected_identity_path"] = "conversation_reset"
            report["status"] = "passed"
            report["failure_code"] = None
            return write_report()
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
            probe_prompt=args.input_prompt,
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
        report["scenarios"]["conversation_reset"] = record_reset_observation(
            _managed_conversation_reset_scenario(
                binary=binary,
                workspace=workspace,
                events_path=events_path,
                terminal_path=artifact_root / "conversation-reset.terminal.raw",
                timeout=args.timeout,
                model=args.model,
            )
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
    return write_report()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_native_evidence(report: dict[str, Any], artifact_root: Path) -> list[dict[str, Any]]:
    """Copy provider-native Cursor stores into the retained Gate 0 artifact."""

    native_root = artifact_root / "native-stores"
    receipts: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, Any]] = {}
    for scenario_name, scenario in report.get("scenarios", {}).items():
        if not isinstance(scenario, dict):
            continue
        candidates: list[tuple[str, str | None]] = []

        def add_scenario_sources(source_record: dict[str, Any], *, label: str) -> None:
            expected_agent_id = next(
                (
                    str(source_record.get(key) or "").strip()
                    for key in ("provider_conversation_id", "provider_session_id", "store_agent_id")
                    if str(source_record.get(key) or "").strip()
                ),
                None,
            )
            store_db = source_record.get("store_db")
            if isinstance(store_db, str) and store_db.strip():
                if expected_agent_id is None:
                    raise RuntimeError(f"Cursor native store source has no provider identity: {label}")
                candidates.append((store_db, expected_agent_id))
            raw_source_ids = source_record.get("raw_source_ids")
            if isinstance(raw_source_ids, list):
                for raw_source in raw_source_ids:
                    if not isinstance(raw_source, str) or not raw_source.strip():
                        continue
                    if expected_agent_id is None:
                        raise RuntimeError(f"Cursor native source has no provider identity: {label}")
                    candidates.append((raw_source, expected_agent_id))

        add_scenario_sources(scenario, label=scenario_name)
        for nested_name in ("before", "after"):
            nested = scenario.get(nested_name)
            if isinstance(nested, dict):
                add_scenario_sources(nested, label=f"{scenario_name}.{nested_name}")
        for raw_source, expected_agent_id in candidates:
            source = Path(raw_source).expanduser().resolve(strict=True)
            if not source.is_file():
                raise RuntimeError(f"Cursor native store is not a regular file: {source}")
            source_key = str(source)
            receipt = by_source.get(source_key)
            if receipt is None:
                native_root.mkdir(parents=True, exist_ok=True)
                destination = native_root / f"store-{hashlib.sha256(source_key.encode()).hexdigest()[:20]}.db"
                source_sha256 = _file_sha256(source)
                shutil.copy2(source, destination)
                retained_sha256 = _file_sha256(destination)
                if source_sha256 != retained_sha256:
                    raise RuntimeError("Cursor native store copy is not byte-exact")
                payload = destination.read_bytes()
                exact_secrets = _artifact_secret_values()
                if any(secret in payload for secret in exact_secrets) or any(
                    pattern.search(payload) for pattern, _replacement in _ARTIFACT_SECRET_PATTERNS
                ):
                    raise RuntimeError("Cursor native store contains provider credential material")
                try:
                    connection = sqlite3.connect(f"file:{destination}?mode=ro", uri=True, timeout=1.0)
                    try:
                        blob_count = int(connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0])
                    finally:
                        connection.close()
                except (sqlite3.Error, TypeError, ValueError) as exc:
                    raise RuntimeError(f"Cursor native store is not a readable transcript store: {destination}") from exc
                if blob_count < 1:
                    raise RuntimeError(f"Cursor native store contains no transcript blobs: {destination}")
                store_agent_id = _cursor_store_agent_id(destination)
                if not store_agent_id:
                    raise RuntimeError(f"Cursor native store has no provider session identity: {destination}")
                receipt = {
                    "kind": "cursor_store_db",
                    "path": str(destination.relative_to(artifact_root)),
                    "sha256": retained_sha256,
                    "source_sha256": source_sha256,
                    "size": destination.stat().st_size,
                    "source_scenarios": [],
                    "provider_session_ids": [store_agent_id],
                    "native_blob_count": blob_count,
                    "byte_exact": True,
                }
                by_source[source_key] = receipt
                receipts.append(receipt)
            if expected_agent_id and expected_agent_id not in receipt["provider_session_ids"]:
                raise RuntimeError("Cursor native store identity does not match its Gate 0 scenario")
            if scenario_name not in receipt["source_scenarios"]:
                receipt["source_scenarios"].append(scenario_name)

    events_path = artifact_root / "events.ndjson"
    if events_path.is_file():
        receipts.append(
            {
                "kind": "cursor_hook_events",
                "path": str(events_path.relative_to(artifact_root)),
                "sha256": _file_sha256(events_path),
                "size": events_path.stat().st_size,
            }
        )
    if not any(item.get("kind") == "cursor_store_db" for item in receipts):
        raise RuntimeError("Cursor Gate 0 produced no retained native store evidence")
    return receipts


def _refresh_native_evidence_receipts(report: dict[str, Any], artifact_root: Path) -> None:
    refreshed: list[dict[str, Any]] = []
    for item in report.get("native_evidence", []):
        if not isinstance(item, dict):
            continue
        relative = item.get("path")
        if not isinstance(relative, str):
            continue
        path = (artifact_root / relative).resolve()
        if not path.is_relative_to(artifact_root.resolve()) or not path.is_file():
            raise RuntimeError(f"Cursor native evidence receipt is missing: {relative}")
        refreshed.append(
            {
                **item,
                "sha256": _file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    report["native_evidence"] = refreshed


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
    parser.add_argument(
        "--input-prompt",
        help="Exact prompt to bind into the create_chat_resume evidence for universal send_receive qualification.",
    )
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--conversation-reset-only",
        action="store_true",
        help="Run workspace trust plus the conversation-reset scenario only.",
    )
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
