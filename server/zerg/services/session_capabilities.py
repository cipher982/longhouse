from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from zerg.models.agents import AgentSession
from zerg.services.managed_control_dispatcher import select_managed_control_transport
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.session_execution_home import infer_execution_home

STEERABLE_RUNTIME_STATES = frozenset({"thinking", "running"})
_LIVE_CONTROL_TRANSPORTS = frozenset(
    {
        ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
        ManagedSessionTransport.CODEX_APP_SERVER,
    }
)


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


@dataclass(frozen=True)
class SessionInputPresentation:
    input_mode: str
    default_input_intent: str
    composer_enabled: bool
    composer_placeholder: str
    composer_disabled_reason: str | None


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
    lifecycle: str | None = None,
) -> SessionCapabilityDisplay:
    if lifecycle == "closed":
        return SessionCapabilityDisplay(
            label="Closed",
            detail="This session has ended.",
            tone="neutral",
        )

    normalized_host = _normalize_host_label(host_label or capability_flags.home_label)
    if capability_flags.live_control_available or capability_flags.reply_to_live_session_available:
        return SessionCapabilityDisplay(
            label=f"Live on {normalized_host}" if normalized_host else "Send",
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
        label="Read only",
        detail="This imported session is searchable, but Longhouse cannot steer it.",
        tone="neutral",
    )


def build_session_input_presentation(
    capability_flags: SessionCapabilityFlags,
    *,
    capability_display: SessionCapabilityDisplay,
    provider_label: str | None = None,
    lifecycle: str | None = None,
    is_executing: bool = False,
) -> SessionInputPresentation:
    provider = (provider_label or "").strip() or "session"
    provider_session = provider if provider.lower().endswith("session") else f"{provider} session"

    if capability_flags.reply_to_live_session_available:
        if capability_flags.can_steer_active_turn:
            default_intent = "steer"
        elif capability_flags.can_queue_next_input and is_executing:
            default_intent = "queue"
        else:
            default_intent = "auto"
        return SessionInputPresentation(
            input_mode="live",
            default_input_intent=default_intent,
            composer_enabled=True,
            composer_placeholder=f"Send a message to the live {provider_session}...",
            composer_disabled_reason=None,
        )

    if lifecycle == "closed":
        return SessionInputPresentation(
            input_mode="read_only",
            default_input_intent="none",
            composer_enabled=False,
            composer_placeholder="Type a message...",
            composer_disabled_reason=capability_display.detail,
        )

    if capability_flags.host_reattach_available:
        return SessionInputPresentation(
            input_mode="offline",
            default_input_intent="none",
            composer_enabled=False,
            composer_placeholder="Type a message...",
            composer_disabled_reason=(f"Longhouse can see this {provider_session}, but cannot send prompts until the engine reconnects."),
        )

    return SessionInputPresentation(
        input_mode="read_only",
        default_input_intent="none",
        composer_enabled=False,
        composer_placeholder="Type a message...",
        composer_disabled_reason=capability_display.detail,
    )


def project_current_session_capabilities(
    capability_flags: SessionCapabilityFlags,
    *,
    runtime_display,
) -> SessionCapabilityFlags:
    """Project durable managed-session metadata into current action availability.

    ``build_session_capabilities`` answers what kind of control path the session
    was created with. This helper answers what Longhouse can truthfully do right
    now. A managed session is not "live control" unless runtime truth says the
    session is open, fresh, and hosted on an online runner.
    """

    lifecycle = str(getattr(runtime_display, "lifecycle", "") or "").strip()
    host_state = str(getattr(runtime_display, "host_state", "") or "").strip()
    activity_recency = str(getattr(runtime_display, "activity_recency", "") or "").strip()
    runtime_state = str(getattr(runtime_display, "state", "") or "").strip()

    currently_live = (
        capability_flags.live_control_available and lifecycle == "open" and host_state == "online" and activity_recency == "live"
    )
    reattach_available = capability_flags.host_reattach_available and lifecycle != "closed" and not currently_live
    can_steer = currently_live and capability_flags.can_steer_active_turn and runtime_state in STEERABLE_RUNTIME_STATES

    return SessionCapabilityFlags(
        execution_home=capability_flags.execution_home,
        managed_transport=capability_flags.managed_transport,
        live_control_available=currently_live,
        host_reattach_available=reattach_available,
        reply_to_live_session_available=currently_live,
        can_queue_next_input=currently_live,
        can_steer_active_turn=can_steer,
        home_label=capability_flags.home_label,
    )


def _read_attr(value: Any, name: str, default: Any = None) -> Any:
    return getattr(value, name, default) if value is not None else default


def _normalized_fact(value: Any) -> str:
    return str(value or "").strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _phase_is_current(*, phase: Any, now: datetime) -> bool:
    kind = _normalized_fact(_read_attr(phase, "kind"))
    if not kind:
        return False
    expires_at = _read_attr(phase, "expires_at")
    if expires_at is None:
        return False
    return _utc(expires_at) > _utc(now)


def _control_is_current(*, control: Any, now: datetime) -> bool:
    state = _normalized_fact(_read_attr(control, "state"))
    if state != "online":
        return False
    expires_at = _read_attr(control, "expires_at")
    if expires_at is None:
        return False
    return _utc(expires_at) > _utc(now)


def project_current_session_capabilities_from_facts(
    capability_flags: SessionCapabilityFlags,
    *,
    liveness_facts,
    now: datetime | None = None,
) -> SessionCapabilityFlags:
    """Project durable control metadata into current actions from facts only.

    ``liveness_facts`` may be the internal dataclass model or the Pydantic API
    response model. The projection intentionally avoids display labels such as
    "recent", "stale", or "Control offline"; it gates send/interrupt actions
    on explicit control facts, while steer still requires a current active
    provider phase.
    """

    lifecycle = _read_attr(liveness_facts, "lifecycle")
    phase = _read_attr(liveness_facts, "phase")
    control = _read_attr(liveness_facts, "control")
    lifecycle_state = _normalized_fact(_read_attr(lifecycle, "state"))
    current_now = now or datetime.now(timezone.utc)
    control_current = _control_is_current(control=control, now=current_now)
    phase_current = _phase_is_current(phase=phase, now=current_now)

    currently_live = capability_flags.live_control_available and lifecycle_state == "open" and control_current
    # A stale-but-steerable session (kernel says live, runtime overlay says
    # control offline) demotes live → reattach. Both buckets imply the same
    # spawned_control / adopted_control plane; the kernel's reattach flag
    # only false because the connection row hasn't been updated to detached
    # yet. This is a down-gate of live, not an up-gate of reattach — it is
    # gated on `live_control_available`, never on a kernel-False reattach.
    live_demoted_to_reattach = (
        capability_flags.live_control_available
        and lifecycle_state != "closed"
        and not currently_live
    )
    reattach_available = (
        (capability_flags.host_reattach_available or live_demoted_to_reattach)
        and lifecycle_state != "closed"
        and not currently_live
    )
    phase_kind = _normalized_fact(_read_attr(phase, "kind"))
    can_steer = currently_live and phase_current and capability_flags.can_steer_active_turn and phase_kind in STEERABLE_RUNTIME_STATES

    return SessionCapabilityFlags(
        execution_home=capability_flags.execution_home,
        managed_transport=capability_flags.managed_transport,
        live_control_available=currently_live,
        host_reattach_available=reattach_available,
        reply_to_live_session_available=currently_live,
        can_queue_next_input=currently_live,
        can_steer_active_turn=can_steer,
        home_label=capability_flags.home_label,
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


def _has_live_control_transport(session: AgentSession | None) -> bool:
    return resolve_managed_transport(session) in _LIVE_CONTROL_TRANSPORTS


def supports_live_control(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return (
        resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL
        and _has_live_control_transport(session)
        and select_managed_control_transport(session) is not None
    )


def supports_host_reattach(session: AgentSession | None) -> bool:
    if session is None:
        return False
    return resolve_execution_home(session) == SessionExecutionHome.MANAGED_LOCAL and _has_live_control_transport(session)


def build_session_capabilities(session: AgentSession | None) -> SessionCapabilityFlags:
    execution_home = resolve_execution_home(session) if session is not None else SessionExecutionHome.UNMANAGED_LOCAL
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
