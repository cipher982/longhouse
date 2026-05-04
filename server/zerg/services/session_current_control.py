from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_capabilities import project_current_session_capabilities_from_facts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runner_state import managed_runner_host_state
from zerg.services.session_runtime import load_runtime_state_map
from zerg.services.session_runtime import resolve_runtime_overlay


def current_session_capabilities(db: Session, session: AgentSession) -> SessionCapabilityFlags:
    """Return user-action capabilities backed by current runtime truth."""
    capability_flags = build_session_capabilities(session)
    now = datetime.now(timezone.utc)
    last_activity_at = (
        getattr(session, "last_activity_at", None) or getattr(session, "ended_at", None) or getattr(session, "started_at", None)
    )
    runtime_state_map = load_runtime_state_map(db, [session.id])
    runtime_overlay = resolve_runtime_overlay(
        session,
        last_activity_at=last_activity_at,
        runtime_state_map=runtime_state_map,
        now=now,
    )
    binding_host_state = None
    if capability_flags.live_control_available or capability_flags.host_reattach_available:
        binding_host_state = managed_runner_host_state(db, session)
    liveness_facts = build_session_liveness_facts(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        last_activity_at=last_activity_at,
        binding_host_state=binding_host_state,
    )
    return project_current_session_capabilities_from_facts(capability_flags, liveness_facts=liveness_facts, now=now)
