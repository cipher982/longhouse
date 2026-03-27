"""Managed-local transport seam.

tmux still owns the fully runner-launched path, but native Codex managed-local
sessions now use a local `longhouse-engine codex-bridge` sidecar for reattach
and remote control.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from zerg.models.agents import AgentSession
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


class ManagedLocalTransportNotImplementedError(ManagedLocalTransportError):
    """Raised when a valid transport exists conceptually but is not wired yet."""


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


def coerce_managed_transport(
    value: str | ManagedSessionTransport | None,
    *,
    default: ManagedSessionTransport | None = None,
) -> ManagedSessionTransport | None:
    if isinstance(value, ManagedSessionTransport):
        return value
    raw = str(value or "").strip()
    if not raw:
        return default
    return ManagedSessionTransport(raw)


def managed_local_transport_supports_interactive_chat(
    value: str | ManagedSessionTransport | None,
) -> bool:
    resolved = coerce_managed_transport(value, default=ManagedSessionTransport.TMUX)
    return resolved in {ManagedSessionTransport.TMUX, ManagedSessionTransport.CODEX_APP_SERVER}


def build_managed_local_launch_transport_plan(
    *,
    transport: str | ManagedSessionTransport | None,
    session_name: str,
    cwd: str,
    entry_command: str,
    tmux_tmpdir: str | None = None,
) -> ManagedLocalLaunchTransportPlan:
    resolved = coerce_managed_transport(transport, default=ManagedSessionTransport.TMUX)
    if resolved == ManagedSessionTransport.TMUX:
        return ManagedLocalLaunchTransportPlan(
            transport=resolved,
            launch_command=build_tmux_launch_command(
                session_name=session_name,
                cwd=cwd,
                launch_command=entry_command,
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
                tmux_tmpdir=tmux_tmpdir,
            ),
        )

    raise ManagedLocalTransportNotImplementedError(f"Managed local transport '{resolved.value}' is not implemented yet")


def build_managed_local_attach_command(*, session: AgentSession) -> str | None:
    transport = coerce_managed_transport(
        getattr(session, "managed_transport", None),
        default=ManagedSessionTransport.TMUX,
    )
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
    if transport != ManagedSessionTransport.TMUX:
        return None
    session_name = str(getattr(session, "managed_session_name", "") or "").strip()
    if not session_name:
        return None
    return build_tmux_attach_command(
        session_name=session_name,
        tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
    )


def build_managed_local_interrupt_command(*, session: AgentSession) -> str:
    """Build a command to interrupt the active turn on a managed-local session."""
    transport = coerce_managed_transport(
        getattr(session, "managed_transport", None),
        default=ManagedSessionTransport.TMUX,
    )
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="interrupt",
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
    raise ManagedLocalTransportNotImplementedError(f"Managed local interrupt is not implemented for transport '{transport.value}'")


def build_managed_local_send_text_command(*, session: AgentSession, text: str) -> str:
    transport = coerce_managed_transport(
        getattr(session, "managed_transport", None),
        default=ManagedSessionTransport.TMUX,
    )
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            raise ManagedLocalTransportError("Managed local session is missing session ID")
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="send",
            args=("--text", shlex.quote(text)),
        )
    if transport != ManagedSessionTransport.TMUX:
        raise ManagedLocalTransportNotImplementedError(f"Managed local send-text is not implemented for transport '{transport.value}'")

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
    "ManagedLocalTransportNotImplementedError",
    "build_managed_local_attach_command",
    "build_managed_local_interrupt_command",
    "build_managed_local_launch_transport_plan",
    "build_managed_local_send_text_command",
    "coerce_managed_transport",
    "managed_local_transport_supports_interactive_chat",
]
