from __future__ import annotations

from dataclasses import dataclass

from zerg.models.agents import AgentSession
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome


@dataclass(frozen=True)
class SessionCapabilityFlags:
    execution_home: SessionExecutionHome
    managed_transport: ManagedSessionTransport | None
    live_control_available: bool
    cloud_branch_available: bool
    host_reattach_available: bool
    reply_to_live_session_available: bool
    home_label: str | None
    requires_managed_local_tmux_reconcile: bool


def _coerce_execution_home(value: str | None) -> SessionExecutionHome | None:
    if value is None or not str(value).strip():
        return None
    try:
        return SessionExecutionHome(str(value).strip())
    except ValueError:
        return None


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
    if execution_home == SessionExecutionHome.MANAGED_HOSTED:
        return "Hosted"
    if execution_home == SessionExecutionHome.CLOUD_TAKEOVER:
        return "Moved to cloud"
    return None


def resolve_execution_home(session: AgentSession) -> SessionExecutionHome:
    stored = _coerce_execution_home(getattr(session, "execution_home", None))
    if stored is not None and stored != SessionExecutionHome.LEGACY:
        return stored

    continuation_kind = str(getattr(session, "continuation_kind", "") or "").strip().lower()
    if continuation_kind == "cloud":
        return SessionExecutionHome.CLOUD_TAKEOVER
    if continuation_kind == "runner":
        return SessionExecutionHome.MANAGED_HOSTED

    origin_label = str(getattr(session, "origin_label", "") or "").strip().lower()
    environment = str(getattr(session, "environment", "") or "").strip().lower()
    if origin_label == "cloud" or environment == "cloud":
        return SessionExecutionHome.CLOUD_TAKEOVER
    if origin_label == "hosted" or environment == "hosted":
        return SessionExecutionHome.MANAGED_HOSTED

    return stored or SessionExecutionHome.LEGACY


def resolve_managed_transport(session: AgentSession | None) -> ManagedSessionTransport | None:
    if session is None:
        return None
    return _coerce_managed_transport(getattr(session, "managed_transport", None))


def supports_live_control(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL and getattr(session, "source_runner_id", None) is not None


def supports_cloud_branch(session: AgentSession | None) -> bool:
    if session is None:
        return False
    if supports_live_control(session):
        return False
    provider = str(getattr(session, "provider", "") or "").strip().lower()
    return provider == "claude"


def supports_host_reattach(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL


def should_reconcile_managed_local_tmux_runtime(session: AgentSession | None) -> bool:
    if session is None or getattr(session, "ended_at", None) is not None:
        return False
    return (
        resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL
        and resolve_managed_transport(session) == ManagedSessionTransport.TMUX
    )


def build_session_capabilities(session: AgentSession | None) -> SessionCapabilityFlags:
    execution_home = resolve_execution_home(session) if session is not None else SessionExecutionHome.LEGACY
    managed_transport = resolve_managed_transport(session)
    live_control_available = supports_live_control(session)
    return SessionCapabilityFlags(
        execution_home=execution_home,
        managed_transport=managed_transport,
        live_control_available=live_control_available,
        cloud_branch_available=supports_cloud_branch(session),
        host_reattach_available=supports_host_reattach(session),
        reply_to_live_session_available=live_control_available,
        home_label=_execution_home_label(execution_home),
        requires_managed_local_tmux_reconcile=should_reconcile_managed_local_tmux_runtime(session),
    )
