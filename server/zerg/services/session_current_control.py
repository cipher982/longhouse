from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.services.managed_control_state import CONTROL_SOURCE_LEGACY_RUNNER
from zerg.services.managed_control_state import engine_channel_control_overlay
from zerg.services.managed_control_state import live_transport_control_overlay
from zerg.services.managed_control_state import load_managed_control_state_map
from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_capabilities import project_current_session_capabilities_from_facts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runner_state import managed_runner_host_state
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay
from zerg.session_execution_home import ManagedSessionTransport
from zerg.utils.time import normalize_utc

_ENGINE_ATTACHED_RUNTIME_SOURCES = {"codex_bridge", "codex_bridge_live"}


def engine_control_online(session: AgentSession, owner_id: int | None) -> bool:
    return (
        select_managed_control_transport(
            session,
            owner_id=owner_id,
            command_type=MANAGED_CONTROL_COMMAND_SEND_TEXT,
        )
        == MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
    )


def _normalized(value) -> str:
    return str(value or "").strip()


def _control_overlay_attached(session: AgentSession, control_overlay, *, now: datetime) -> bool:
    if control_overlay is None:
        return False
    session_id = _normalized(getattr(session, "id", None))
    overlay_session_id = _normalized(getattr(control_overlay, "session_id", None))
    if overlay_session_id and session_id and overlay_session_id != session_id:
        return False
    transport = _normalized(getattr(control_overlay, "transport", None))
    session_transport = _normalized(getattr(session, "managed_transport", None))
    if transport and session_transport and transport != session_transport:
        return False
    control_state = _normalized(getattr(control_overlay, "control_state", None)).lower()
    lease_state = _normalized(getattr(control_overlay, "lease_state", None)).lower()
    if control_state not in {"online", "attached"} and lease_state != "attached":
        return False
    expires_at = normalize_utc(
        getattr(control_overlay, "control_expires_at", None) or getattr(control_overlay, "expires_at", None)
    )
    if expires_at is None:
        return False
    return expires_at > now


def engine_session_control_attached(
    session: AgentSession,
    runtime_overlay,
    *,
    control_overlay=None,
    now: datetime | None = None,
) -> bool:
    """Return whether the engine channel is known to own this session."""

    if str(getattr(session, "launch_state", "") or "").strip() == "live":
        return True
    runtime_source = str(getattr(runtime_overlay, "runtime_source", "") or "").strip()
    if runtime_source in _ENGINE_ATTACHED_RUNTIME_SOURCES:
        return True
    return _control_overlay_attached(session, control_overlay, now=now or datetime.now(timezone.utc))


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
    last_activity_at = getattr(session, "last_activity_at", None)
    if last_activity_at is None:
        last_activity_at = getattr(session, "ended_at", None) or getattr(session, "started_at", None)
    runtime_state_map = load_runtime_state_map(db, [session.id])
    control_state_map = load_managed_control_state_map(db, [session.id])
    control_overlay = control_state_map.get(session.id)
    runtime_overlay = resolve_runtime_overlay(
        session,
        last_activity_at=last_activity_at,
        runtime_state_map=runtime_state_map,
        now=now,
    )
    is_engine_session_attached = is_engine_control_online and engine_session_control_attached(
        session,
        runtime_overlay,
        control_overlay=control_overlay,
        now=now,
    )
    capability_flags = with_engine_control_capability(
        capability_flags,
        engine_control_online=is_engine_session_attached,
    )
    binding_host_state = None
    if is_engine_session_attached:
        binding_host_state = "online"
        control_overlay = engine_channel_control_overlay(session, seen_at=now)
    elif (capability_flags.live_control_available or capability_flags.host_reattach_available) and getattr(
        session, "source_runner_id", None
    ) is not None:
        binding_host_state = managed_runner_host_state(db, session)
        if binding_host_state == "online" and control_overlay is None:
            control_overlay = live_transport_control_overlay(
                session,
                source=CONTROL_SOURCE_LEGACY_RUNNER,
                seen_at=now,
            )
    liveness_facts = build_session_liveness_facts(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        last_activity_at=last_activity_at,
        binding_host_state=binding_host_state,
        control_overlay=control_overlay,
        now=now,
    )
    return project_current_session_capabilities_from_facts(capability_flags, liveness_facts=liveness_facts, now=now)


__all__ = [
    "current_session_capabilities",
    "engine_control_online",
    "engine_session_control_attached",
    "with_engine_control_capability",
]
