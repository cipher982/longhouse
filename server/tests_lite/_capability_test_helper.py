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

from zerg.services.session_capabilities import SessionCapabilityFlags
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


def build_session_capabilities(session: Any) -> SessionCapabilityFlags:
    if session is None:
        return SessionCapabilityFlags(
            execution_home=SessionExecutionHome.UNMANAGED_LOCAL,
            managed_transport=None,
            live_control_available=False,
            host_reattach_available=False,
            reply_to_live_session_available=False,
            can_queue_next_input=False,
            can_steer_active_turn=False,
            home_label=None,
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
    can_steer = live_control_available and managed_transport == ManagedSessionTransport.CODEX_APP_SERVER
    return SessionCapabilityFlags(
        execution_home=execution_home,
        managed_transport=managed_transport,
        live_control_available=live_control_available,
        host_reattach_available=host_reattach_available,
        reply_to_live_session_available=live_control_available,
        can_queue_next_input=live_control_available,
        can_steer_active_turn=can_steer,
        home_label=_execution_home_label(execution_home),
    )
