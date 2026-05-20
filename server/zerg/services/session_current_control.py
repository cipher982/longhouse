from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_capabilities import project_current_session_capabilities_from_facts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runner_state import managed_runner_host_state
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.session_execution_home import ManagedSessionTransport


def engine_control_online(session: AgentSession, owner_id: int | None) -> bool:
    return (
        select_managed_control_transport(
            session,
            owner_id=owner_id,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
        == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
    )


def with_engine_control_capability(
    capability_flags: SessionCapabilityFlags,
    *,
    engine_control_online: bool,
) -> SessionCapabilityFlags:
    if not engine_control_online:
        return capability_flags
    can_steer = capability_flags.managed_transport == ManagedSessionTransport.CODEX_APP_SERVER
    return SessionCapabilityFlags(
        execution_home=capability_flags.execution_home,
        managed_transport=capability_flags.managed_transport,
        live_control_available=True,
        host_reattach_available=True,
        reply_to_live_session_available=True,
        can_queue_next_input=True,
        can_steer_active_turn=can_steer,
        home_label=capability_flags.home_label,
    )


def current_session_capabilities(
    db: Session,
    session: AgentSession,
    *,
    owner_id: int | None = None,
) -> SessionCapabilityFlags:
    """Return user-action capabilities backed by current runtime truth."""
    capability_flags = build_session_capabilities(session)
    is_engine_control_online = engine_control_online(session, owner_id)
    now = datetime.now(timezone.utc)
    last_activity_at = (
        getattr(session, "last_activity_at", None) or getattr(session, "ended_at", None) or getattr(session, "started_at", None)
    )
    runtime_state_map = load_runtime_state_map(db, [session.id])
    control_state_map = load_managed_control_state_map(db, [session.id])
    runtime_overlay = resolve_runtime_overlay(
        session,
        last_activity_at=last_activity_at,
        runtime_state_map=runtime_state_map,
        now=now,
    )
    capability_flags = with_engine_control_capability(
        capability_flags,
        engine_control_online=is_engine_control_online,
    )
    binding_host_state = None
    if is_engine_control_online:
        binding_host_state = "online"
    elif capability_flags.live_control_available or capability_flags.host_reattach_available:
        binding_host_state = managed_runner_host_state(db, session)
    liveness_facts = build_session_liveness_facts(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        last_activity_at=last_activity_at,
        binding_host_state=binding_host_state,
        control_overlay=control_state_map.get(session.id),
        now=now,
    )
    return project_current_session_capabilities_from_facts(capability_flags, liveness_facts=liveness_facts, now=now)


__all__ = [
    "current_session_capabilities",
    "engine_control_online",
    "with_engine_control_capability",
]
