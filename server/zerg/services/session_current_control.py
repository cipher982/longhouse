from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_session_capabilities
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_COMMAND_SEND_TEXT
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_TRANSPORT_ENGINE_CHANNEL
from zerg.services.managed_control_dispatcher import select_managed_control_transport
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
    expires_at = normalize_utc(getattr(control_overlay, "control_expires_at", None) or getattr(control_overlay, "expires_at", None))
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
    """Return whether the engine channel is known to own this session.

    Used purely as a runtime-staleness signal for the liveness facts /
    control overlay. It is no longer permitted to up-gate kernel-derived
    capability flags — the kernel projection is the only place that can
    grant live control.
    """

    if str(getattr(session, "launch_state", "") or "").strip() == "live":
        return True
    runtime_source = str(getattr(runtime_overlay, "runtime_source", "") or "").strip()
    if runtime_source in _ENGINE_ATTACHED_RUNTIME_SOURCES:
        return True
    return _control_overlay_attached(session, control_overlay, now=now or datetime.now(timezone.utc))


def current_session_capabilities(
    db: Session,
    session: AgentSession,
    *,
    owner_id: int | None = None,
) -> KernelSessionCapabilities:
    """Return user-action capabilities backed by current runtime truth.

    Capability flags come from the kernel projection.
    """
    return project_session_capabilities(db, session_id=session.id)


__all__ = [
    "current_session_capabilities",
    "engine_control_online",
    "engine_session_control_attached",
]
