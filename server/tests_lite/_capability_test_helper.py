"""Test-only capability flag construction.

The production capability path is the kernel projection
(``project_session_capabilities``) plus the legacy adapter
(``build_session_capabilities_from_kernel``). Some legacy unit/runtime
tests pre-date the kernel and seed sessions with the (now-removed)
``execution_home``/``managed_transport`` columns.

This helper mirrors the deleted ``build_session_capabilities`` function
so those tests keep exercising the runtime overlay logic without a
kernel-row fixture. Production code MUST NOT import this — it is a
test-fixture shim, not a fallback.
"""

from __future__ import annotations

from typing import Any

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import infer_execution_home

_LIVE_CONTROL_TRANSPORTS = frozenset(
    {
        ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
        ManagedSessionTransport.CODEX_APP_SERVER,
    }
)


def _coerce_managed_transport(value: str | None) -> ManagedSessionTransport | None:
    if value is None or not str(value).strip():
        return None
    try:
        return ManagedSessionTransport(str(value).strip())
    except ValueError:
        return None


def _execution_home_label(execution_home: SessionExecutionHome) -> str | None:
    if execution_home == SessionExecutionHome.MANAGED_LOCAL:
        return "On this Mac"
    return None


def _control_plane_for_transport(managed_transport: ManagedSessionTransport | None) -> str | None:
    if managed_transport == ManagedSessionTransport.CODEX_APP_SERVER:
        return "codex_bridge"
    if managed_transport == ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE:
        return "claude_channel_bridge"
    if managed_transport == ManagedSessionTransport.OPENCODE_PROCESS:
        return "opencode_process"
    if managed_transport == ManagedSessionTransport.ANTIGRAVITY_PROCESS:
        return "antigravity_process"
    return None


def build_session_capabilities(session: Any) -> KernelSessionCapabilities:
    if session is None:
        return KernelSessionCapabilities(
            session_id="",
            thread_id=None,
            run_id=None,
            connection_id=None,
            control_plane=None,
            connection_state=None,
            control_label="imported",
            live_control_available=False,
            host_reattach_available=False,
            observe_only=False,
            search_only=True,
            can_send_input=False,
            can_interrupt=False,
            can_terminate=False,
            can_tail_output=False,
            can_resume=False,
            staleness_reason="imported_only",
        )
    execution_home = infer_execution_home(
        execution_home=getattr(session, "execution_home", None),
        continuation_kind=getattr(session, "continuation_kind", None),
        origin_label=getattr(session, "origin_label", None),
        environment=getattr(session, "environment", None),
    )
    managed_transport = _coerce_managed_transport(getattr(session, "managed_transport", None))
    is_managed_local = execution_home == SessionExecutionHome.MANAGED_LOCAL
    has_live_transport = managed_transport in _LIVE_CONTROL_TRANSPORTS
    has_runner = getattr(session, "source_runner_id", None) is not None
    live_control_available = bool(is_managed_local and has_live_transport and has_runner)
    host_reattach_available = bool(is_managed_local and has_live_transport)
    can_send = live_control_available

    control_label = "imported"
    if live_control_available:
        control_label = "live"
    elif host_reattach_available:
        control_label = "reattach"

    return KernelSessionCapabilities(
        session_id=str(getattr(session, "id", "")),
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane=_control_plane_for_transport(managed_transport),
        connection_state="attached" if live_control_available else "detached",
        control_label=control_label,
        live_control_available=live_control_available,
        host_reattach_available=host_reattach_available,
        observe_only=False,
        search_only=not (live_control_available or host_reattach_available),
        can_send_input=can_send,
        can_interrupt=live_control_available,
        can_terminate=live_control_available,
        can_tail_output=live_control_available,
        can_resume=live_control_available or host_reattach_available,
        staleness_reason=None if live_control_available else "imported_only",
    )
