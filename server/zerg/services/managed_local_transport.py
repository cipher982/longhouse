"""Managed-local transport seam for native-only managed sessions."""

from __future__ import annotations

import json
import shlex
from typing import Any, Mapping, Sequence

from zerg.models.agents import AgentSession
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.managed_local_shell import build_managed_local_shell_prelude
from zerg.session_execution_home import ManagedSessionTransport


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
            required_commands=required_commands,
        ),
        f"exec {invocation}",
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner_parts))}"


def _resolve_transport(value: str | ManagedSessionTransport | None) -> ManagedSessionTransport:
    if isinstance(value, ManagedSessionTransport):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ManagedLocalTransportError("Managed local session is missing transport metadata")
    try:
        return ManagedSessionTransport(raw)
    except ValueError as exc:
        raise ManagedLocalTransportError(f"Unsupported managed local transport: {raw}") from exc


def build_managed_local_attach_command(*, session: AgentSession) -> str | None:
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport in {
        ManagedSessionTransport.OPENCODE_PROCESS,
        ManagedSessionTransport.ANTIGRAVITY_PROCESS,
    }:
        return None
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


def build_managed_local_interrupt_command(*, session: AgentSession) -> str:
    """Build a command to interrupt the active turn on a managed-local session."""
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        raise ManagedLocalTransportError("opencode_process does not support remote interrupts yet")
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("antigravity_process does not support remote interrupts yet")
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="interrupt",
        )
    return _build_longhouse_cli_shell_command(
        subcommand="interrupt",
        args=("--session-id", shlex.quote(session_id)),
    )


def _attachment_args(
    attachments: Sequence[Mapping[str, Any]] | None,
    *,
    transport: ManagedSessionTransport,
) -> tuple[str, ...]:
    """Serialize attachment refs into `--attachments-json <json>` for the
    engine codex-bridge subprocess. Returns empty tuple when there are no
    attachments so text-only sends keep their previous shape.

    Only the codex_app_server transport supports attachments today; for any
    other transport, a non-empty list is a hard error rather than a silent
    drop.
    """
    if not attachments:
        return ()
    if transport != ManagedSessionTransport.CODEX_APP_SERVER:
        raise ManagedLocalTransportError(
            "Attachments are only supported on codex_app_server transports",
        )
    payload = json.dumps(list(attachments), separators=(",", ":"))
    return ("--attachments-json", shlex.quote(payload))


def build_managed_local_send_text_command(
    *,
    session: AgentSession,
    text: str,
    attachments: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        raise ManagedLocalTransportError("opencode_process does not support remote text sends yet")
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("antigravity_process does not support remote text sends yet")
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    attach_args = _attachment_args(attachments, transport=transport)
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="send",
            args=("--text", shlex.quote(text), *attach_args),
        )
    return _build_longhouse_cli_shell_command(
        subcommand="send",
        args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text)),
    )


def build_managed_local_steer_text_command(
    *,
    session: AgentSession,
    text: str,
    attachments: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Build a mid-turn steer command. Codex-only this batch; Claude channel
    has no equivalent first-class primitive yet."""
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        raise ManagedLocalTransportError("Mid-turn steer is not supported on opencode_process transports")
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("Mid-turn steer is not supported on antigravity_process transports")
    if transport != ManagedSessionTransport.CODEX_APP_SERVER:
        raise ManagedLocalTransportError(
            "Mid-turn steer is only supported on codex_app_server transports",
        )
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    attach_args = _attachment_args(attachments, transport=transport)
    return _build_engine_bridge_shell_command(
        session_id=session_id,
        subcommand="steer",
        args=("--text", shlex.quote(text), *attach_args),
    )


__all__ = [
    "ManagedLocalTransportError",
    "build_managed_local_attach_command",
    "build_managed_local_interrupt_command",
    "build_managed_local_send_text_command",
    "build_managed_local_steer_text_command",
]
