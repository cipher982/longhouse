"""Deterministic tmux command builders for managed-local sessions."""

from __future__ import annotations

import re
import shlex

TMUX_SESSION_NAME_MAX = 64
_TMUX_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
MANAGED_LOCAL_TMUX_SERVER_LABEL = "longhouse-managed"
TMUX_NOT_INSTALLED_MESSAGE = "tmux is not installed"
MANAGED_LOCAL_TMUX_HISTORY_LIMIT = 50000
MANAGED_LOCAL_TMUX_DEFAULT_TERMINAL = "tmux-256color"
MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES = 1
MANAGED_LOCAL_STANDARD_PATH_PREFIXES = (
    "$HOME/.local/bin",
    "$HOME/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/home/linuxbrew/.linuxbrew/bin",
    "/home/linuxbrew/.linuxbrew/sbin",
)


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


def build_managed_local_path_export() -> str:
    """Prepend common user-local install locations without loading interactive shell state."""
    joined = ":".join(MANAGED_LOCAL_STANDARD_PATH_PREFIXES)
    return f'export PATH="{joined}:$PATH"'


def build_managed_local_conditional_zshrc_source(*, required_commands: tuple[str, ...] = ()) -> str | None:
    """Source ~/.zshrc only when fast-path PATH resolution still misses a required binary."""
    cleaned_commands = (str(command or "").strip() for command in required_commands)
    normalized = tuple(dict.fromkeys(command for command in cleaned_commands if command))
    if not normalized:
        return None

    missing_checks = " || ".join(f"! command -v {_quote(command)} >/dev/null 2>&1" for command in normalized)
    return f"if {missing_checks}; then source ~/.zshrc >/dev/null 2>&1 || true; fi"


def build_managed_local_shell_prelude(
    *,
    tmux_tmpdir: str | None = None,
    require_tmux: bool = True,
    required_commands: tuple[str, ...] = (),
) -> str:
    """Shell bootstrap shared by preflight and tmux follow-up commands."""
    required = list(required_commands)
    if require_tmux:
        required.append("tmux")

    commands = [build_managed_local_path_export()]
    zshrc_fallback = build_managed_local_conditional_zshrc_source(required_commands=tuple(required))
    if zshrc_fallback:
        commands.append(zshrc_fallback)
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


def _build_managed_local_copy_mode_scroll_command(*, direction: str) -> str:
    return f"send-keys -X -N {MANAGED_LOCAL_TMUX_WHEEL_SCROLL_LINES} scroll-{direction}"


def _build_managed_local_wheel_binding(*, table: str, direction: str) -> str:
    suffix = "Up" if direction == "up" else "Down"
    command = _build_managed_local_copy_mode_scroll_command(direction=direction)
    return f"bind-key -T {table} Wheel{suffix}Pane {command}"


def _build_managed_local_root_wheel_binding() -> str:
    return (
        'bind-key -T root WheelUpPane if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
        '"send-keys -M" '
        f'"copy-mode -e ; {_build_managed_local_copy_mode_scroll_command(direction="up")}"'
    )


def _managed_local_tmux_launch_options() -> tuple[str, ...]:
    """Options that make the dedicated managed tmux server less intrusive."""
    return (
        "set-option -s escape-time 0",
        "set-option -g status off",
        "set-option -g mouse on",
        f"set-option -g default-terminal {MANAGED_LOCAL_TMUX_DEFAULT_TERMINAL}",
        "set-option -gu terminal-features",
        "set-option -as terminal-features ',*:RGB'",
        f"set-option -g history-limit {MANAGED_LOCAL_TMUX_HISTORY_LIMIT}",
        "set-option -g remain-on-exit failed",
        "unbind-key -T root WheelUpPane",
        _build_managed_local_root_wheel_binding(),
        "unbind-key -T copy-mode WheelUpPane",
        "unbind-key -T copy-mode WheelDownPane",
        _build_managed_local_wheel_binding(table="copy-mode", direction="up"),
        _build_managed_local_wheel_binding(table="copy-mode", direction="down"),
        "unbind-key -T copy-mode-vi WheelUpPane",
        "unbind-key -T copy-mode-vi WheelDownPane",
        _build_managed_local_wheel_binding(table="copy-mode-vi", direction="up"),
        _build_managed_local_wheel_binding(table="copy-mode-vi", direction="down"),
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
    """Build a tmux command that sends text followed by submit.

    Claude's TUI reliably accepts `C-m` from tmux for turn submission, but it
    needs a real gap after the literal text send. In real managed-local
    canaries, `Enter` left the prompt sitting in the input box, and even `C-m`
    could be dropped when sent back-to-back with the text. A one-second sleep
    proved reliable while still keeping the control loop responsive.
    """
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
        commands.append("sleep 1")
        commands.append(f"{_tmux_prefix()} send-keys -t {_quote(name)} C-m")
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
