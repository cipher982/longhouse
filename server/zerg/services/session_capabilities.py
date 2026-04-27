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
    # True when mid-turn steer is likely to land. Gated on transport being
    # codex_app_server (the only transport with a turn/steer primitive
    # today). The bridge may still reject a steer if the active turn ended
    # between UI check and dispatch — callers must handle that race.
    can_steer_active_turn: bool
    home_label: str | None


@dataclass(frozen=True)
class SessionCapabilityDisplay:
    label: str
    detail: str
    tone: str


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


def _normalize_host_label(host_label: str | None) -> str | None:
    value = (host_label or "").strip()
    if not value:
        return None
    if value.lower().startswith("on "):
        value = value[3:].strip()
    return value or None


def build_session_capability_display(
    capability_flags: SessionCapabilityFlags,
    *,
    host_label: str | None = None,
) -> SessionCapabilityDisplay:
    normalized_host = _normalize_host_label(host_label or capability_flags.home_label)
    if capability_flags.live_control_available or capability_flags.reply_to_live_session_available:
        return SessionCapabilityDisplay(
            label=f"Live on {normalized_host}" if normalized_host else "Live control",
            detail="Longhouse can send prompts into this live session.",
            tone="success",
        )
    if capability_flags.host_reattach_available:
        return SessionCapabilityDisplay(
            label="Control offline",
            detail="Reattach on the host to resume control.",
            tone="warning",
        )
    return SessionCapabilityDisplay(
        label="Search only",
        detail="This imported session is searchable, but Longhouse cannot steer it.",
        tone="neutral",
    )


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
    can_steer = live_control_available and managed_transport == ManagedSessionTransport.CODEX_APP_SERVER
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
        can_steer_active_turn=can_steer,
        home_label=_execution_home_label(execution_home),
    )
