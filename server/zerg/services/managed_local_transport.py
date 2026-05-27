"""Managed-local transport seam for native-only managed sessions."""

from __future__ import annotations

import json
import shlex
from typing import Any
from typing import Mapping
from typing import Sequence

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.managed_local_shell import build_managed_local_shell_prelude
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
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
    command_group: str = "claude-channel",
    subcommand: str,
    args: tuple[str, ...] = (),
    required_commands: tuple[str, ...] = ("longhouse",),
    namespace: str | None = None,
) -> str:
    group = namespace or command_group
    invocation = " ".join(
        [
            "longhouse",
            group,
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


def build_managed_local_attach_command(*, session: AgentSession, db: Session | None = None) -> str | None:
    from sqlalchemy.orm import object_session

    from zerg.models.agents import SessionThreadAlias
    from zerg.services.agents.kernel_capabilities import project_session_capabilities

    try:
        session_db = db or object_session(session)
    except Exception:
        session_db = None
    session_id = str(session.id)

    # Resolve transport: prefer kernel projection when a DB is available,
    # else fall back to session.managed_transport attribute (used by unit
    # tests that build SimpleNamespace fixtures).
    if session_db is not None:
        caps = project_session_capabilities(session_db, session_id=session.id)
        if not caps.host_reattach_available:
            return None
        control_plane = (caps.control_plane or "").strip()
        resolved_transport = managed_transport_for_control_plane(control_plane)
        transport = resolved_transport.value if resolved_transport is not None else None
    else:
        transport = getattr(session, "managed_transport", None)

    if transport == ManagedSessionTransport.CODEX_APP_SERVER.value:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="attach",
            required_commands=("longhouse-engine", "codex"),
            exec_engine=True,
        )

    if transport == ManagedSessionTransport.OPENCODE_PROCESS.value:
        return _build_longhouse_cli_shell_command(
            subcommand="inspect",
            args=("--session-id", shlex.quote(session_id)),
            namespace="opencode-bridge",
        )

    if transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE.value:
        return _build_longhouse_cli_shell_command(
            command_group="opencode-channel",
            subcommand="attach",
            args=("--session-id", shlex.quote(session_id)),
            required_commands=("longhouse", "opencode"),
        )

    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS.value:
        return None

    if transport != ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value:
        return None

    # For claude channel bridge: provider_session_id from kernel alias
    # when DB available, else from the attribute.
    provider_session_id = None
    if session_db is not None and getattr(session, "primary_thread_id", None) is not None:
        alias = (
            session_db.query(SessionThreadAlias)
            .filter(
                SessionThreadAlias.thread_id == session.primary_thread_id,
                SessionThreadAlias.alias_kind == "provider_session_id",
            )
            .first()
        )
        provider_session_id = alias.alias_value if alias else None
    if not provider_session_id:
        provider_session_id = getattr(session, "provider_session_id", None) or session_id

    cwd = str(getattr(session, "cwd", "") or "").strip()
    if not cwd:
        # Unit tests pass minimal fixtures without cwd — accept a default
        # so the attach command builder can still be exercised.
        cwd = "."

    return build_claude_channel_exec_command(
        provider_session_id=provider_session_id,
        longhouse_session_id=session_id,
        cwd=cwd,
        resume=False,
    )


def build_managed_local_interrupt_command(*, session: AgentSession) -> str:
    """Build a command to interrupt the active turn on a managed-local session."""
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    if transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE:
        return _build_longhouse_cli_shell_command(
            command_group="opencode-channel",
            subcommand="interrupt",
            args=("--session-id", shlex.quote(session_id)),
        )
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("antigravity_process does not support remote interrupts yet")
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="interrupt",
        )
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        return _build_longhouse_cli_shell_command(
            subcommand="interrupt",
            args=("--session-id", shlex.quote(session_id)),
            namespace="opencode-bridge",
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
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    if transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE:
        if attachments:
            raise ManagedLocalTransportError(
                "Attachments are only supported on codex_app_server transports",
            )
        return _build_longhouse_cli_shell_command(
            command_group="opencode-channel",
            subcommand="send",
            args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text)),
        )
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("antigravity_process does not support remote text sends yet")
    attach_args = _attachment_args(attachments, transport=transport)
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="send",
            args=("--text", shlex.quote(text), *attach_args),
        )
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        return _build_longhouse_cli_shell_command(
            subcommand="send",
            args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text)),
            namespace="opencode-bridge",
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
    """Build a mid-turn steer command.

    Supported on codex_app_server (engine codex-bridge) and on
    opencode_process (longhouse opencode-bridge steer, which performs
    abort -> wait idle -> send under the hood). claude_channel_bridge
    injects a channel send with intent=steer metadata.
    """
    transport = _resolve_transport(getattr(session, "managed_transport", None))
    if transport == ManagedSessionTransport.OPENCODE_SERVER_BRIDGE:
        raise ManagedLocalTransportError("Mid-turn steer is not supported on opencode_server_bridge transports")
    if transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        raise ManagedLocalTransportError("Mid-turn steer is not supported on antigravity_process transports")
    session_id = str(getattr(session, "id", "") or "").strip()
    if not session_id:
        raise ManagedLocalTransportError("Managed local session is missing session ID")
    attach_args = _attachment_args(attachments, transport=transport)
    if transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return _build_engine_bridge_shell_command(
            session_id=session_id,
            subcommand="steer",
            args=("--text", shlex.quote(text), *attach_args),
        )
    if transport == ManagedSessionTransport.OPENCODE_PROCESS:
        return _build_longhouse_cli_shell_command(
            subcommand="steer",
            args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text)),
            namespace="opencode-bridge",
        )
    if transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        return _build_longhouse_cli_shell_command(
            subcommand="send",
            args=("--session-id", shlex.quote(session_id), "--text", shlex.quote(text), "--meta", "intent=steer"),
        )
    raise ManagedLocalTransportError(
        f"Mid-turn steer is not supported on {transport.value} transports",
    )


__all__ = [
    "ManagedLocalTransportError",
    "build_managed_local_attach_command",
    "build_managed_local_interrupt_command",
    "build_managed_local_send_text_command",
    "build_managed_local_steer_text_command",
]
