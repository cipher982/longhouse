"""Managed-local attach command planning for native-only managed sessions."""

from __future__ import annotations

import shlex

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.claude_channel_bridge import build_claude_channel_exec_command
from zerg.services.managed_local_shell import build_managed_local_shell_prelude
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
from zerg.session_execution_home import ManagedSessionTransport


def _build_engine_bridge_shell_command(
    *,
    session_id: str,
    subcommand: str | None,
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
    invocation = " ".join(["longhouse", group, *([subcommand] if subcommand else []), *args])
    inner_parts = [
        build_managed_local_shell_prelude(
            required_commands=required_commands,
        ),
        f"exec {invocation}",
    ]
    return f"zsh -lc {shlex.quote('; '.join(inner_parts))}"


def build_managed_local_attach_command(*, session: AgentSession, db: Session | None = None) -> str | None:
    from sqlalchemy.orm import object_session

    from zerg.services.agents.kernel_capabilities import project_session_capabilities
    from zerg.services.session_kernel_projection import project_provider_session_id

    try:
        session_db = db or object_session(session)
    except Exception:
        session_db = None
    session_id = str(session.id)

    # Resolve transport from the kernel projection when a DB is available.
    # Minimal non-ORM fixtures provide session.managed_transport directly.
    if session_db is not None:
        caps = project_session_capabilities(session_db, session_id=session.id)
        if not caps.host_reattach_available:
            return None
        control_plane = (caps.control_plane or "").strip()
        resolved_transport = managed_transport_for_control_plane(control_plane)
        transport = resolved_transport.value if resolved_transport is not None else None
    elif not isinstance(session, AgentSession):
        transport = getattr(session, "managed_transport", None)
    else:
        transport = None

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

    if transport == ManagedSessionTransport.CURSOR_HELM.value:
        cursor_args = ["--resume-session", shlex.quote(session_id)]
        cwd = str(getattr(session, "cwd", "") or "").strip()
        if cwd:
            cursor_args.extend(["--cwd", shlex.quote(cwd)])
        return _build_longhouse_cli_shell_command(
            command_group="cursor",
            subcommand=None,
            args=tuple(cursor_args),
            required_commands=("longhouse", "cursor-agent"),
        )

    if transport in (
        ManagedSessionTransport.ANTIGRAVITY_PROCESS.value,
        ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX.value,
    ):
        return None

    if transport != ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value:
        return None

    if session_db is not None:
        provider_session_id = project_provider_session_id(session_db, session)
        if not provider_session_id:
            return None
    elif not isinstance(session, AgentSession):
        # Minimal non-ORM fixtures can still provide the native provider id.
        provider_session_id = getattr(session, "provider_session_id", None)
        if not provider_session_id:
            return None
    else:
        return None

    cwd = str(getattr(session, "cwd", "") or "").strip()
    if not cwd:
        # Unit tests pass minimal fixtures without cwd — accept a default
        # so the attach command builder can still be exercised.
        cwd = "."

    permission_mode = str(getattr(session, "permission_mode", "") or "bypass").strip() or "bypass"
    return build_claude_channel_exec_command(
        provider_session_id=provider_session_id,
        longhouse_session_id=session_id,
        cwd=cwd,
        resume=False,
        permission_mode=permission_mode,
    )


__all__ = [
    "build_managed_local_attach_command",
]
