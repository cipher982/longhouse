"""Deterministic tmux command builders for managed-local sessions."""

from __future__ import annotations

import re
import shlex

from zerg.session_execution_home import ManagedSessionTransport

TMUX_SESSION_NAME_MAX = 64
_TMUX_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
MANAGED_LOCAL_TMUX_SERVER_LABEL = "longhouse-managed"


def normalize_tmux_session_name(seed: str, *, prefix: str = "lh") -> str:
    """Build a safe tmux session name from user/session input."""
    raw = str(seed or "").strip()
    if not raw:
        raise ValueError("tmux session seed must not be empty")
    cleaned = _TMUX_SAFE_CHARS.sub("-", raw).strip("-")
    if not cleaned:
        raise ValueError("tmux session seed did not contain any safe characters")
    name = f"{prefix}-{cleaned}" if prefix else cleaned
    return name[:TMUX_SESSION_NAME_MAX].rstrip("-")


def validate_managed_transport(value: str | None) -> ManagedSessionTransport | None:
    """Validate managed transport string, returning None for empty values."""
    raw = str(value or "").strip()
    if not raw:
        return None
    return ManagedSessionTransport(raw)


def _quote(value: str) -> str:
    return shlex.quote(value)


def _require_non_empty(name: str, value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{name} must not be empty")
    return raw


def _tmux_prefix() -> str:
    return f"tmux -L {_quote(MANAGED_LOCAL_TMUX_SERVER_LABEL)}"


def build_tmux_launch_command(*, session_name: str, cwd: str, launch_command: str) -> str:
    """Build a detached tmux launch command for a managed local session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    working_dir = _require_non_empty("cwd", cwd)
    entry = _require_non_empty("launch_command", launch_command)
    inner = f"cd {_quote(working_dir)} && exec {entry}"
    return f"{_tmux_prefix()} new-session -d -s {_quote(name)} {_quote(inner)}"


def build_tmux_has_session_command(*, session_name: str) -> str:
    """Build a probe command that exits 0 when the tmux session exists."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return f"{_tmux_prefix()} has-session -t {_quote(name)}"


def build_tmux_current_command_command(*, session_name: str) -> str:
    """Build a command that prints the active pane command for a tmux session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return f"{_tmux_prefix()} display-message -p -t {_quote(name)} '#{{pane_current_command}}'"


def build_tmux_kill_session_command(*, session_name: str) -> str:
    """Build a best-effort session kill command for cleanup."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return f"{_tmux_prefix()} kill-session -t {_quote(name)}"


def build_tmux_capture_command(*, session_name: str, lines: int = 200) -> str:
    """Build a pane-capture command for the tmux session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    if lines <= 0:
        raise ValueError("lines must be positive")
    return f"{_tmux_prefix()} capture-pane -pt {_quote(name)} -S -{int(lines)}"


def build_tmux_set_remain_on_exit_command(*, session_name: str, mode: str = "failed") -> str:
    """Build a command that preserves failed panes for inspection."""
    name = normalize_tmux_session_name(session_name, prefix="")
    normalized_mode = _require_non_empty("mode", mode)
    return f"{_tmux_prefix()} set-option -t {_quote(name)} remain-on-exit {_quote(normalized_mode)}"


def build_tmux_attach_command(*, session_name: str) -> str:
    """Build the user-facing attach command for a managed local session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return f"{_tmux_prefix()} attach -t {_quote(name)}"


def build_tmux_send_text_command(*, session_name: str, text: str) -> str:
    """Build a tmux command that sends text followed by Enter."""
    name = normalize_tmux_session_name(session_name, prefix="")
    raw = str(text or "")
    if not raw.strip():
        raise ValueError("text must not be empty")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n")
    commands: list[str] = []
    for line in lines:
        if line:
            commands.append(f"{_tmux_prefix()} send-keys -t {_quote(name)} -- {_quote(line)} Enter")
        else:
            commands.append(f"{_tmux_prefix()} send-keys -t {_quote(name)} Enter")
    return " && ".join(commands)
