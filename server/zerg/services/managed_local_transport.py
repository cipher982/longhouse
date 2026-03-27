"""Managed-local transport seam.

Tmux is the only implemented transport today. `codex_app_server` is reserved so
launch/control callers can stop hard-coding tmux semantics before the native
Codex path is wired up.
"""

from __future__ import annotations

from dataclasses import dataclass

from zerg.models.agents import AgentSession
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
    return coerce_managed_transport(value, default=ManagedSessionTransport.TMUX) == ManagedSessionTransport.TMUX


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
    if transport != ManagedSessionTransport.TMUX:
        return None
    session_name = str(getattr(session, "managed_session_name", "") or "").strip()
    if not session_name:
        return None
    return build_tmux_attach_command(
        session_name=session_name,
        tmux_tmpdir=getattr(session, "managed_tmux_tmpdir", None),
    )


def build_managed_local_send_text_command(*, session: AgentSession, text: str) -> str:
    transport = coerce_managed_transport(
        getattr(session, "managed_transport", None),
        default=ManagedSessionTransport.TMUX,
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
    "build_managed_local_launch_transport_plan",
    "build_managed_local_send_text_command",
    "coerce_managed_transport",
    "managed_local_transport_supports_interactive_chat",
]
