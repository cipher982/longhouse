"""Helpers for the native Claude channel bridge transport."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from zerg.services.managed_local_shell import build_managed_local_shell_prelude
from zerg.services.managed_session_env import build_managed_session_env_exports

CLAUDE_CHANNEL_SERVER_NAME = "longhouse-channel"
CLAUDE_COORDINATION_SERVER_NAME = "longhouse-coordination"
CLAUDE_CHANNEL_DEVELOPMENT_FLAG = "--dangerously-load-development-channels"


def _quote(value: str) -> str:
    return shlex.quote(value)


def _resolve_claude_dir(claude_dir: str | Path | None = None) -> Path:
    if claude_dir is None:
        return Path.home() / ".claude"
    return Path(claude_dir).expanduser()


def resolve_claude_channel_state_root(
    *,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    return _resolve_claude_dir(claude_dir) / "channels" / "longhouse"


def build_claude_channel_state_file(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> Path:
    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id must not be empty")
    try:
        normalized = str(UUID(normalized))
    except ValueError as exc:
        raise ValueError("session_id must be a valid UUID") from exc
    state_root_path = resolve_claude_channel_state_root(state_root=state_root, claude_dir=claude_dir)
    return state_root_path / "sessions" / f"{normalized}.json"


def read_claude_channel_state(
    *,
    session_id: str,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> dict[str, Any]:
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root, claude_dir=claude_dir)
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Claude channel state at {state_path} is not a JSON object")
    return raw


def wait_for_claude_channel_state(
    *,
    session_id: str,
    timeout_secs: float = 10.0,
    poll_interval_secs: float = 0.1,
    require_ready: bool = True,
    state_root: str | Path | None = None,
    claude_dir: str | Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_secs
    state_path = build_claude_channel_state_file(session_id=session_id, state_root=state_root, claude_dir=claude_dir)
    last_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        if state_path.exists():
            try:
                last_state = read_claude_channel_state(
                    session_id=session_id,
                    state_root=state_root,
                    claude_dir=claude_dir,
                )
            except (json.JSONDecodeError, OSError, ValueError):
                time.sleep(poll_interval_secs)
                continue
            if not require_ready or bool(last_state.get("ready")):
                return last_state
        time.sleep(poll_interval_secs)
    if last_state is None:
        raise FileNotFoundError(f"Claude channel state did not appear at {state_path} within {timeout_secs:.1f}s")
    raise TimeoutError(f"Claude channel state at {state_path} did not become ready within {timeout_secs:.1f}s")


# Claude managed permission policy.
#  - "bypass" (default): launch with --dangerously-skip-permissions; the session
#    never asks and the Longhouse permission gate stays dormant. This is the
#    historical behavior and must remain the default.
#  - "remote_approve": launch WITHOUT --dangerously-skip-permissions and enable
#    the PreToolUse permission gate, so the session's permission prompts can be
#    answered remotely through Longhouse.
CLAUDE_PERMISSION_MODE_BYPASS = "bypass"
CLAUDE_PERMISSION_MODE_REMOTE_APPROVE = "remote_approve"


def build_claude_channel_exec_command(
    *,
    provider_session_id: str,
    longhouse_session_id: str,
    longhouse_run_id: str | None = None,
    cwd: str,
    resume: bool,
    hook_url: str | None = None,
    claude_command: str = "claude",
    permission_mode: str = CLAUDE_PERMISSION_MODE_BYPASS,
    mcp_config_path: str | Path | None = None,
) -> str:
    """Build the native Claude launch/resume shell command for channel sessions."""

    provider_sid = str(provider_session_id or "").strip()
    longhouse_sid = str(longhouse_session_id or "").strip()
    working_dir = str(cwd or "").strip()
    if not provider_sid:
        raise ValueError("provider_session_id must not be empty")
    if not longhouse_sid:
        raise ValueError("longhouse_session_id must not be empty")
    if not working_dir:
        raise ValueError("cwd must not be empty")

    remote_approve = str(permission_mode or "").strip() == CLAUDE_PERMISSION_MODE_REMOTE_APPROVE

    target_flag = "--resume" if resume else "--session-id"
    command_bits = [claude_command]
    # Only bypass permissions when NOT routing approvals through Longhouse.
    if not remote_approve:
        command_bits.append("--dangerously-skip-permissions")
    command_bits += [
        target_flag,
        provider_sid,
        CLAUDE_CHANNEL_DEVELOPMENT_FLAG,
        f"server:{CLAUDE_CHANNEL_SERVER_NAME}",
    ]
    if mcp_config_path is not None:
        command_bits += ["--mcp-config", str(Path(mcp_config_path))]
    inner = [
        build_managed_local_shell_prelude(required_commands=(claude_command,)),
        f"cd {_quote(working_dir)}",
        *build_managed_session_env_exports(longhouse_sid),
        f"export LONGHOUSE_CHANNEL_SESSION_ID={_quote(longhouse_sid)}",
        f"export LONGHOUSE_PROVIDER_SESSION_ID={_quote(provider_sid)}",
        f"export LONGHOUSE_CHANNEL_CWD={_quote(working_dir)}",
    ]
    if longhouse_run_id:
        inner.append(f"export LONGHOUSE_RUN_ID={_quote(str(UUID(longhouse_run_id)))}")
    if hook_url:
        inner.append(f"export LONGHOUSE_HOOK_URL={_quote(str(hook_url).strip())}")
    # Engage the permission gate ONLY in remote-approve mode. In bypass mode we
    # explicitly force it off so an inherited LONGHOUSE_PERMISSION_HOOK_ENABLED=1
    # from the parent shell can never gate a bypass/autonomous session.
    if remote_approve:
        inner.append("export LONGHOUSE_PERMISSION_HOOK_ENABLED=1")
    else:
        inner.append("export LONGHOUSE_PERMISSION_HOOK_ENABLED=0")
    inner.append("exec " + " ".join(_quote(part) for part in command_bits))
    return f"zsh -lc {_quote('; '.join(inner))}"


__all__ = [
    "CLAUDE_CHANNEL_DEVELOPMENT_FLAG",
    "CLAUDE_CHANNEL_SERVER_NAME",
    "build_claude_channel_exec_command",
    "build_claude_channel_state_file",
    "read_claude_channel_state",
    "resolve_claude_channel_state_root",
    "wait_for_claude_channel_state",
]
