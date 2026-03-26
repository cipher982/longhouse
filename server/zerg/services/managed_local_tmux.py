"""Deterministic tmux command builders for managed-local sessions."""

from __future__ import annotations

import re
import shlex

from zerg.session_execution_home import ManagedSessionTransport

TMUX_SESSION_NAME_MAX = 64
_TMUX_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
MANAGED_LOCAL_TMUX_SERVER_LABEL = "longhouse-managed"
TMUX_NOT_INSTALLED_MESSAGE = "tmux is not installed"
MANAGED_LOCAL_TMUX_HISTORY_LIMIT = 50000


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


def _normalize_tmux_tmpdir(value: str | None) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def build_managed_local_shell_prelude(*, tmux_tmpdir: str | None = None, require_tmux: bool = True) -> str:
    """Shell bootstrap shared by preflight and tmux follow-up commands."""
    commands = ["source ~/.zshrc >/dev/null 2>&1"]
    missing_tmux_message = _quote(TMUX_NOT_INSTALLED_MESSAGE)
    missing_tmux_guard = f"command -v tmux >/dev/null 2>&1 || {{ echo {missing_tmux_message} >&2; exit 11; }}"
    normalized_tmpdir = _normalize_tmux_tmpdir(tmux_tmpdir)
    if normalized_tmpdir:
        commands.append(f"export TMUX_TMPDIR={_quote(normalized_tmpdir)}")
    if require_tmux:
        commands.append(missing_tmux_guard)
    return "; ".join(commands)


def _wrap_managed_local_shell_command(
    command: str,
    *,
    tmux_tmpdir: str | None = None,
    exec_command: bool = False,
) -> str:
    inner = [
        build_managed_local_shell_prelude(tmux_tmpdir=tmux_tmpdir),
        f"{'exec ' if exec_command else ''}{_require_non_empty('command', command)}",
    ]
    return f"zsh -lc {_quote('; '.join(inner))}"


def _tmux_prefix() -> str:
    return f"tmux -L {_quote(MANAGED_LOCAL_TMUX_SERVER_LABEL)}"


def _managed_local_tmux_launch_options() -> tuple[str, ...]:
    """Options that make the dedicated managed tmux server less intrusive."""
    return (
        "set-option -s escape-time 0",
        "set-option -g status off",
        "set-option -g mouse on",
        f"set-option -g history-limit {MANAGED_LOCAL_TMUX_HISTORY_LIMIT}",
        "set-option -g remain-on-exit failed",
    )


def build_tmux_launch_command(
    *,
    session_name: str,
    cwd: str,
    launch_command: str,
    tmux_tmpdir: str | None = None,
) -> str:
    """Build a detached tmux launch command for a managed local session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    working_dir = _require_non_empty("cwd", cwd)
    entry = _require_non_empty("launch_command", launch_command)
    script_path = f"/tmp/longhouse-managed-{name}.zsh"
    script_body = "\n".join(
        [
            "#!/bin/zsh",
            "set -e",
            f"exec {entry}",
        ]
    )
    write_script = "\n".join(
        [
            f"cat > {_quote(script_path)} <<'__LONGHOUSE_MANAGED_LOCAL__'",
            script_body,
            "__LONGHOUSE_MANAGED_LOCAL__",
            f"chmod +x {_quote(script_path)}",
        ]
    )
    tmux_segments = [f"{_tmux_prefix()} start-server", *_managed_local_tmux_launch_options()]
    tmux_segments.append(f"new-session -d -s {_quote(name)} -c {_quote(working_dir)} {_quote(script_path)}")
    tmux_command = " \\; ".join(tmux_segments)
    return _wrap_managed_local_shell_command("\n".join([write_script, tmux_command]), tmux_tmpdir=tmux_tmpdir)


def build_tmux_has_session_command(*, session_name: str, tmux_tmpdir: str | None = None) -> str:
    """Build a probe command that exits 0 when the tmux session exists."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} has-session -t {_quote(name)}",
        tmux_tmpdir=tmux_tmpdir,
    )


def build_tmux_current_command_command(*, session_name: str, tmux_tmpdir: str | None = None) -> str:
    """Build a command that prints the active pane command for a tmux session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} display-message -p -t {_quote(name)} '#{{pane_current_command}}'",
        tmux_tmpdir=tmux_tmpdir,
    )


def build_tmux_kill_session_command(*, session_name: str, tmux_tmpdir: str | None = None) -> str:
    """Build a best-effort session kill command for cleanup."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} kill-session -t {_quote(name)}",
        tmux_tmpdir=tmux_tmpdir,
    )


def build_tmux_capture_command(*, session_name: str, lines: int = 200, tmux_tmpdir: str | None = None) -> str:
    """Build a pane-capture command for the tmux session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    if lines <= 0:
        raise ValueError("lines must be positive")
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} capture-pane -pt {_quote(name)} -S -{int(lines)}",
        tmux_tmpdir=tmux_tmpdir,
    )


def build_tmux_set_remain_on_exit_command(
    *,
    session_name: str,
    mode: str = "failed",
    tmux_tmpdir: str | None = None,
) -> str:
    """Build a command that preserves failed panes for inspection."""
    name = normalize_tmux_session_name(session_name, prefix="")
    normalized_mode = _require_non_empty("mode", mode)
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} set-option -t {_quote(name)} remain-on-exit {_quote(normalized_mode)}",
        tmux_tmpdir=tmux_tmpdir,
    )


def build_tmux_attach_command(*, session_name: str, tmux_tmpdir: str | None = None) -> str:
    """Build the user-facing attach command for a managed local session."""
    name = normalize_tmux_session_name(session_name, prefix="")
    return _wrap_managed_local_shell_command(
        f"{_tmux_prefix()} attach -t {_quote(name)}",
        tmux_tmpdir=tmux_tmpdir,
        exec_command=True,
    )


def build_tmux_send_text_command(*, session_name: str, text: str, tmux_tmpdir: str | None = None) -> str:
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
            commands.append(f"{_tmux_prefix()} send-keys -t {_quote(name)} -l -- {_quote(line)}")
        commands.append(f"{_tmux_prefix()} send-keys -t {_quote(name)} Enter")
    return _wrap_managed_local_shell_command(" && ".join(commands), tmux_tmpdir=tmux_tmpdir)


def build_tmux_paste_text_command(*, session_name: str, text: str, tmux_tmpdir: str | None = None) -> str:
    """Build a tmux command that pastes text as bracketed paste, then submits.

    Codex's composer distinguishes literal typing from paste handling. Using a
    named tmux buffer plus `paste-buffer -pr` preserves multiline text inside a
    bracketed paste transaction, then a final Enter submits the composed turn.
    """

    name = normalize_tmux_session_name(session_name, prefix="")
    raw = str(text or "")
    if not raw.strip():
        raise ValueError("text must not be empty")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    buffer_name = normalize_tmux_session_name(f"send-{name}", prefix="")
    commands = [
        f"{_tmux_prefix()} set-buffer -b {_quote(buffer_name)} {_quote(normalized)}",
        f"{_tmux_prefix()} paste-buffer -dpr -b {_quote(buffer_name)} -t {_quote(name)}",
        f"{_tmux_prefix()} send-keys -t {_quote(name)} Enter",
    ]
    return _wrap_managed_local_shell_command(" && ".join(commands), tmux_tmpdir=tmux_tmpdir)
