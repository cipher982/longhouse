"""Managed-local transport seam.

Launch resolution decides the transport per session. Command builders read the
stored transport from existing sessions.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from zerg.models.agents import AgentSession
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.managed_local_tmux import build_managed_local_shell_prelude
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_tmux import build_tmux_capture_command
from zerg.services.managed_local_tmux import build_tmux_current_command_command
from zerg.services.managed_local_tmux import build_tmux_has_session_command
from zerg.services.managed_local_tmux import build_tmux_kill_session_command
from zerg.services.managed_local_tmux import build_tmux_launch_command
from zerg.services.managed_local_tmux import build_tmux_paste_text_command
from zerg.services.managed_local_tmux import build_tmux_send_text_command
from zerg.session_execution_home import ManagedSessionTransport


@dataclass(frozen=True)
class ManagedLocalLaunchTransportPlan:
    transport: ManagedSessionTransport
    launch_command: str
    verify_session_command: str
    verify_command: str | None
    capture_command: str | None
    cleanup_command: str | None
    attach_command: str | None


class ManagedLocalTransportError(ValueError):
    """Base error for managed-local transport planning."""


def _build_engine_bridge_shell_command(
    *,
    session_id: str,
    subcommand: str,
    args: tuple[str, ...] = (),
    required_commands: tuple[str, ...] = ("longhouse-engine",),
    exec_engine: bool = False,
) -> str:
    engine_invocation = " ".join(
        [
            '"$engine"',
            "codex-bridge",
            subcommand,
            "--session-id",
            shlex.quote(session_id),
            *args,
        ]
    )
    inner_parts = [
        build_managed_local_shell_prelude(
            require_tmux=False,
            required_commands=required_commands,
        ),
        'engine="$(command -v longhouse-engine || true)"',
        '[ -n "$engine" ] || { echo "longhouse-engine is not available" >&2; exit 12; }',
        (f"exec {engine_invocation}" if exec_engine else engine_invocation),
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner_parts))}"


def _build_longhouse_cli_shell_command(
    *,
    subcommand: str,
    args: tuple[str, ...] = (),
    required_commands: tuple[str, ...] = ("longhouse",),
) -> str:
    invocation = " ".join(
        [
            "longhouse",
            "claude-channel",
            subcommand,
            *args,
        ]
    )
    inner_parts = [
        build_managed_local_shell_prelude(
            require_tmux=False,
            required_commands=required_commands,
        ),
        f"exec {invocation}",
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner_parts))}"


def _resolve_transport(value: str | ManagedSessionTransport | None) -> ManagedSessionTransport:
    """Resolve a stored transport value to an enum. Internal use only."""
    if isinstance(value, ManagedSessionTransport):
        return value
    raw = str(value or "").strip()
    if not raw:
        return ManagedSessionTransport.TMUX
    return ManagedSessionTransport(raw)


def build_managed_local_launch_transport_plan(
    *,
    session_name: str,
    cwd: str,
    entry_command: str,
    session_id: str | None = None,
    provider: str | None = None,
    tmux_tmpdir: str | None = None,
) -> ManagedLocalLaunchTransportPlan:
    """Build a tmux launch plan for tmux-backed managed-local sessions."""
    return ManagedLocalLaunchTransportPlan(
        transport=ManagedSessionTransport.TMUX,
        launch_command=build_tmux_launch_command(
            session_name=session_name,
            cwd=cwd,
            launch_command=entry_command,
            session_id=session_id,
            provider=provider,
            tmux_tmpdir=tmux_tmpdir,
        ),
        verify_session_command=build_tmux_has_session_command(
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
        ),
        verify_command=build_tmux_current_command_command(
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
        ),
        capture_command=build_tmux_capture_command(
            session_name=session_name,
            lines=80,
            tmux_tmpdir=tmux_tmpdir,
        ),
        cleanup_command=build_tmux_kill_session_command(
            session_name=session_name,
            tmux_tmpdir=tmux_tmpdir,
        ),
        attach_command=build_tmux_attach_command(
            session_name=session_name,
            session_id=session_id,
            tmux_tmpdir=tmux_tmpdir,
        ),
    )


def build_managed_local_attach_command(*, session: AgentSession) -> str | None:
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            return None
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="attach",
            required_commands=("longhouse-engine", "codex"),
            exec_engine=True,
        )
    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        provider_session_id = str(getattr(session, "provider_session_id", "") or "").strip()
        session_id = str(getattr(session, "id", "") or "").strip()
        cwd = str(getattr(session, "cwd", "") or "").strip()
        if not provider_session_id or not session_id or not cwd:
            return None
        return build_claude_channel_exec_command(
            provider_session_id=provider_session_id,
            longhouse_session_id=session_id,
            cwd=cwd,
            resume=False,
        )
    session_name = str(getattr(session, "managed_session_name", "") or "").strip()
    if not session_name:
        return None
    return build_tmux_attach_command(
        session_name=session_name,
        session_id=str(getattr(session, "id", "") or "").strip() or None,
        tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
    )


def build_managed_local_interrupt_command(*, session: AgentSession) -> str:
    """Build a command to interrupt the active turn on a managed-local session."""
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="interrupt",
        )
    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_longhouse_cli_shell_command(
            subcommand="interrupt",
            args=("--session-id", shlex.quote(session_id)),
        )
    if transport == ManagedSessionTransport.TMUX:
        session_name = str(getattr(session, "managed_session_name", "") or "").strip()
        if not session_name:
            raise ManagedLocalTransportError("Managed local session is missing tmux metadata")
        from zerg.services.managed_local_tmux import _quote
        from zerg.services.managed_local_tmux import _tmux_prefix
        from zerg.services.managed_local_tmux import _wrap_managed_local_shell_command
        from zerg.services.managed_local_tmux import normalize_tmux_session_name

        name = normalize_tmux_session_name(session_name, prefix="")
        cmd = f"{_tmux_prefix()} send-keys -t {_quote(name)} C-c"
        return _wrap_managed_local_shell_command(
            cmd,
            tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
        )
    raise ManagedLocalTransportError(f"Unknown managed local transport: {transport.value}")


def build_managed_local_send_text_command(*, session: AgentSession, text: str) -> str:
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="send",
            args=("--text", shlex.quote(text)),
        )
    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_longhouse_cli_shell_command(
            subcommand="send",
            args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text)),
        )
    # tmux transport (Claude)
    session_name = str(getattr(session, "managed_session_name", "") or "").strip()
    if not session_name:
        raise ManagedLocalTransportError("Managed local session is missing tmux metadata")

    provider = str(getattr(session, "provider", "") or "").strip().lower()
    tmux_tmpdir = getattr(session, "managed_tmux_tmpdir", None)
    if provider == "codex":
        return build_tmux_paste_text_command(
            session_name=session_name,
            text=text,
            tmux_tmpdir=tmux_tmpdir,
        )
    return build_tmux_send_text_command(
        session_name=session_name,
        text=text,
        tmux_tmpdir=tmux_tmpdir,
    )


__all__ = [
    "ManagedLocalLaunchTransportPlan",
    "ManagedLocalTransportError",
    "build_managed_local_attach_command",
    "build_managed_local_interrupt_command",
    "build_managed_local_launch_transport_plan",
    "build_managed_local_send_text_command",
]
