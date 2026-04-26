from __future__ import annotations

from dataclasses import dataclass

from zerg.models.agents import AgentSession
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import infer_execution_home


@dataclass(frozen=True)
class SessionCapabilityFlags:
    execution_home: SessionExecutionHome
    managed_transport: ManagedSessionTransport | None
    live_control_available: bool
    host_reattach_available: bool
    reply_to_live_session_available: bool
    can_queue_next_input: bool
    home_label: str | None


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


def resolve_execution_home(session: AgentSession) -> SessionExecutionHome:
    return infer_execution_home(
        execution_home=getattr(session, "execution_home", None),
        continuation_kind=getattr(session, "continuation_kind", None),
        origin_label=getattr(session, "origin_label", None),
        environment=getattr(session, "environment", None),
    )


def resolve_managed_transport(session: AgentSession | None) -> ManagedSessionTransport | None:
    if session is None:
        return None
    return _coerce_managed_transport(getattr(session, "managed_transport", None))


def _has_supported_managed_transport(session: AgentSession | None) -> bool:
    return resolve_managed_transport(session) is not None


def supports_live_control(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return (
        resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL
        and _has_supported_managed_transport(session)
        and getattr(session, "source_runner_id", None) is not None
    )


def supports_host_reattach(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL and _has_supported_managed_transport(session)


def build_session_capabilities(session: AgentSession | None) -> SessionCapabilityFlags:
    execution_home = resolve_execution_home(session) if session is not None else SessionExecutionHome.LEGACY
    managed_transport = resolve_managed_transport(session)
    live_control_available = supports_live_control(session)
    return SessionCapabilityFlags(
        execution_home=execution_home,
        managed_transport=managed_transport,
        live_control_available=live_control_available,
        host_reattach_available=supports_host_reattach(session),
        reply_to_live_session_available=live_control_available,
        # Queue-next requires the same dispatch plumbing as a live send; gate
        # on live_control_available so the UI only shows the queued affordance
        # on sessions Longhouse can actually deliver into.
        can_queue_next_input=live_control_available,
        home_label=_execution_home_label(execution_home),
    )
