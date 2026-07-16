"""Session response models and builders.

Shared data layer for converting ORM session/event objects into API response
models.  Both the ``agents`` and ``timeline`` router families import from here
— no router should ever import response models from another router.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import and_
from sqlalchemy import or_

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionLaunchAttempt
from zerg.models.agents import SessionMediaRef
from zerg.models.agents import SessionTurn
from zerg.models.live_store import LiveLaunchReadiness
from zerg.services.agents import AgentsStore
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.live_launch_readiness import LiveLaunchReadinessView
from zerg.services.live_launch_readiness import latest_live_launch_readiness_map as query_live_launch_readiness_map
from zerg.services.live_launch_readiness import project_live_launch_readiness
from zerg.services.managed_control_state import CONTROL_SOURCE_RUNNER_CONNECTION
from zerg.services.managed_control_state import engine_channel_control_overlay
from zerg.services.managed_control_state import live_transport_control_overlay
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.services.provisional_events import TranscriptPreview
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.send_affordance import OFFLINE_HOST_STATES
from zerg.services.send_affordance import SendDisabledReason
from zerg.services.send_affordance import project_send_affordance
from zerg.services.session_capabilities import build_session_capability_display
from zerg.services.session_continue_targets import resolve_native_continue_target
from zerg.services.session_current_control import engine_control_online
from zerg.services.session_current_control import engine_session_control_attached
from zerg.services.session_kernel_projection import SessionControlProjection
from zerg.services.session_kernel_projection import project_session_control_fields
from zerg.services.session_kernel_projection import project_session_kernel_fields
from zerg.services.session_launch_lifecycle import RemoteExecutionLifetime
from zerg.services.session_launch_lifecycle import RemoteLaunchErrorCode
from zerg.services.session_launch_lifecycle import RemoteLaunchLifecycle
from zerg.services.session_launch_lifecycle import RemoteLaunchLifecycleState
from zerg.services.session_launch_lifecycle import project_remote_launch_lifecycle
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runner_state import managed_runner_host_state
from zerg.services.session_runtime import EXPLICIT_CLOSED_TERMINAL_STATES
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime import build_fallback_runtime_view
from zerg.services.session_runtime import should_include_runtime_view
from zerg.services.session_runtime_display import ActivityRecency
from zerg.services.session_runtime_display import ControlPath
from zerg.services.session_runtime_display import HostState
from zerg.services.session_runtime_display import Lifecycle
from zerg.services.session_runtime_display import PresenceState
from zerg.services.session_runtime_display import SignalTier
from zerg.services.session_runtime_display import TerminalReason
from zerg.services.session_runtime_display import Tone
from zerg.services.session_runtime_display import TruthTier
from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_state_contract import build_session_state_facts
from zerg.services.session_title import resolve_timeline_title
from zerg.services.session_title import resolve_title_provenance
from zerg.services.session_turns import hash_user_text
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode
from zerg.utils.time import UTCBaseModel
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)
_LAUNCH_ATTEMPT_MISSING = object()

PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS = timedelta(minutes=2)
PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS = timedelta(minutes=10)
MOBILE_TOOL_OUTPUT_MAX_CHARS = 2000
DROPPED_TOOL_AGE = timedelta(hours=1)
_TRUSTED_NON_RUNNER_CONTROL_PLANES = trusted_non_runner_control_planes()
_CODEX_TURN_ABORTED_PREFIX = "<turn_aborted>"
_CODEX_TURN_INTERRUPTED_TEXT = "User interrupted the turn"

# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _json_obj(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _codex_first_input_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "input_text":
            continue
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None


def _classify_codex_turn_interrupted(event: AgentEvent) -> str | None:
    raw = _json_obj(decode_raw_json(event))
    if raw is None:
        role = str(getattr(event, "role", "") or "").lower()
        if role == "system" and event.content_text == _CODEX_TURN_INTERRUPTED_TEXT:
            return "interrupted"
        return None

    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None

    if raw.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
        reason = payload.get("reason")
        return "interrupted" if reason == "interrupted" else None

    if raw.get("type") != "response_item":
        return None
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    first_text = _codex_first_input_text(payload)
    if isinstance(first_text, str) and first_text.lstrip().startswith(_CODEX_TURN_ABORTED_PREFIX):
        return "marker_only"
    return None


def build_session_action_response(event: AgentEvent) -> TranscriptActionResponse | None:
    """Project provider lifecycle/control evidence as a transcript action."""
    provider_reason = _classify_codex_turn_interrupted(event)
    if provider_reason is None:
        return None

    return TranscriptActionResponse(
        id=f"event:{event.id}:turn_interrupted",
        kind="turn_interrupted",
        provider="codex",
        source="user",
        provider_reason=provider_reason,
        event_id=event.id,
    )


def _coerce_session_loop_mode(value: str | None) -> SessionLoopMode:
    return coerce_session_loop_mode(value)


def build_attach_command(session: AgentSession) -> str | None:
    return build_managed_local_attach_command(session=session)


def build_session_capabilities_response(
    session: AgentSession | None = None,
    *,
    capability_flags=None,
    runtime_display=None,
    runtime_facts=None,
    kernel_capabilities: KernelSessionCapabilities | None = None,
    can_continue: bool = False,
    continue_targets: list[SessionContinueTarget] | None = None,
    launch_lifecycle: RemoteLaunchLifecycle | None = None,
) -> SessionCapabilitiesResponse:
    if capability_flags is None:
        raise RuntimeError("capability_flags is required; the kernel adapter must build them")
    host_label = None
    if session is not None:
        host_label = str(getattr(session, "device_id", "") or "").strip() or None
    lifecycle = _runtime_lifecycle_state(runtime_display=runtime_display, runtime_facts=runtime_facts)
    host_state = _runtime_host_state(runtime_display=runtime_display, runtime_facts=runtime_facts)
    control_unavailable = _runtime_control_unavailable(runtime_facts)
    availability_host_state = "offline" if control_unavailable else host_state
    capability_display = build_session_capability_display(
        capability_flags,
        host_label=host_label,
        lifecycle=lifecycle,
        host_state=availability_host_state,
    )
    host_state_norm = (availability_host_state or "").strip().lower()
    runtime_offline = host_state_norm in OFFLINE_HOST_STATES
    lifecycle_closed = lifecycle == "closed"
    control_available = not runtime_offline and not control_unavailable and not lifecycle_closed
    effective_live_control = bool(capability_flags.live_control_available) and control_available
    effective_reply = bool(capability_flags.reply_to_live_session_available) and control_available
    effective_queue = bool(capability_flags.can_queue_next_input) and control_available
    effective_steer = bool(capability_flags.can_steer_active_turn) and control_available
    effective_host_reattach = bool(capability_flags.host_reattach_available) and not lifecycle_closed
    input_presentation = project_send_affordance(
        capability_flags,
        read_only_reason=capability_display.detail,
        provider_label=_provider_label(session),
        lifecycle=lifecycle,
        is_executing=_runtime_is_executing(runtime_display=runtime_display, runtime_facts=runtime_facts),
        host_state=availability_host_state,
        can_start_turn=bool(kernel_capabilities.can_start_turn) if kernel_capabilities is not None else False,
        start_turn_blocked_by=(kernel_capabilities.start_turn_blocked_by if kernel_capabilities is not None else None),
    )
    launch_state = launch_lifecycle.state if launch_lifecycle is not None else None
    launch_failed = launch_state in {"launch_failed", "launch_orphaned"}
    display_label = "Launch failed" if launch_failed else capability_display.label
    display_detail = (
        launch_lifecycle.error_message or "The session did not start."
        if launch_failed and launch_lifecycle is not None
        else capability_display.detail
    )
    display_tone = "accent" if launch_failed else capability_display.tone
    composer_enabled = False if launch_failed else input_presentation.composer_enabled
    composer_disabled_reason = "Launch failed." if launch_failed else input_presentation.composer_disabled_reason
    send_disabled_reason = "read_only" if launch_failed else input_presentation.send_disabled_reason
    return SessionCapabilitiesResponse(
        live_control_available=False if launch_failed else effective_live_control,
        host_reattach_available=effective_host_reattach,
        reply_to_live_session_available=False if launch_failed else effective_reply,
        can_queue_next_input=False if launch_failed else effective_queue,
        can_steer_active_turn=False if launch_failed else effective_steer,
        display_label=display_label,
        display_detail=display_detail,
        display_tone=display_tone,
        input_mode=input_presentation.input_mode,
        default_input_intent=input_presentation.default_input_intent,
        composer_enabled=composer_enabled,
        composer_placeholder=input_presentation.composer_placeholder,
        composer_disabled_reason=composer_disabled_reason,
        send_disabled_reason=send_disabled_reason,
        control_label=(kernel_capabilities.control_label if kernel_capabilities is not None else None),
        observe_only=(kernel_capabilities.observe_only if kernel_capabilities is not None else False),
        search_only=(kernel_capabilities.search_only if kernel_capabilities is not None else False),
        staleness_reason=(kernel_capabilities.staleness_reason if kernel_capabilities is not None else None),
        can_send_input=(bool(kernel_capabilities.can_send_input) and control_available if kernel_capabilities is not None else False),
        can_interrupt=(bool(kernel_capabilities.can_interrupt) and control_available if kernel_capabilities is not None else False),
        can_terminate=(bool(kernel_capabilities.can_terminate) and control_available if kernel_capabilities is not None else False),
        can_tail_output=(kernel_capabilities.can_tail_output if kernel_capabilities is not None else False),
        can_resume=(bool(kernel_capabilities.can_resume) and not lifecycle_closed if kernel_capabilities is not None else False),
        turn_state=(kernel_capabilities.turn_state if kernel_capabilities is not None else "idle"),
        can_start_turn=(bool(kernel_capabilities.can_start_turn) if kernel_capabilities is not None else False),
        start_turn_blocked_by=(kernel_capabilities.start_turn_blocked_by if kernel_capabilities is not None else None),
        can_interrupt_active_turn=(bool(kernel_capabilities.can_interrupt_active_turn) if kernel_capabilities is not None else False),
        # can_continue means "launch a fresh managed process from this
        # transcript" — a CLOSED-session operation by definition. Do NOT gate it
        # on lifecycle_closed; that defeated the whole resume feature (the button
        # vanished the moment the session closed). It is already self-gated by
        # requiring a native continue target (resolve_native_continue_target):
        # managed sessions need proven managed-control history, and unmanaged
        # sessions need a provider alias + transcript + closed state to be
        # explicitly adoptable.
        can_continue=bool(can_continue),
        continue_targets=continue_targets or [],
        attach_images=_attach_images_capability(capability_flags, live_control_available=effective_live_control),
    )


def _runtime_lifecycle_state(*, runtime_display, runtime_facts) -> str:
    runtime_facts_lifecycle = getattr(runtime_facts, "lifecycle", None)
    lifecycle = str(getattr(runtime_facts_lifecycle, "state", "") or "").strip()
    if lifecycle:
        return lifecycle
    if runtime_display is not None:
        return str(getattr(runtime_display, "lifecycle", "") or "").strip()
    return ""


def _runtime_host_state(*, runtime_display, runtime_facts) -> str | None:
    runtime_facts_host = getattr(runtime_facts, "host", None)
    facts_host_state = str(getattr(runtime_facts_host, "state", "") or "").strip()
    if facts_host_state:
        return facts_host_state
    if runtime_display is not None:
        return str(getattr(runtime_display, "host_state", "") or "").strip() or None
    return None


def _runtime_control_unavailable(runtime_facts) -> bool:
    control = getattr(runtime_facts, "control", None)
    state = str(getattr(control, "state", "") or "").strip().lower()
    reason = str(getattr(control, "reason", "") or "").strip().lower()
    if state in {"offline", "degraded"}:
        return True
    if state == "unknown" and reason in {
        "bridge_unavailable",
        "detached",
        "host_offline",
        "lease_stale",
        "missing_from_snapshot",
        "thread_subscription_failed",
        "unknown_control_state",
    }:
        return True
    return False


def _attach_images_capability(capability_flags, *, live_control_available: bool | None = None) -> bool:
    """True when this session can accept image attachments.

    Gated on (a) the session having live control and (b) the underlying
    transport being codex_app_server. The engine-side LocalImage helper
    only knows how to thread attachments into Codex turns today.
    """
    transport = getattr(capability_flags, "managed_transport", None)
    if transport is None:
        return False
    transport_value = getattr(transport, "value", str(transport))
    if transport_value != "codex_app_server":
        return False
    live = bool(capability_flags.live_control_available) if live_control_available is None else bool(live_control_available)
    return live


def _native_continue_target(db, session: AgentSession) -> SessionContinueTarget | None:
    resolution = resolve_native_continue_target(db, session)
    if resolution is None:
        return None
    return SessionContinueTarget(
        provider=session.provider,
        device_id=session.device_id,
        cwd=session.cwd,
        carry_context="native",
        native_resume_available=True,
        adoption_mode=resolution.adoption_mode,
    )


def _provider_label(session: AgentSession | None) -> str | None:
    provider = str(getattr(session, "provider", "") or "").strip().lower()
    if provider == "gemini":
        provider = "antigravity"
    if not provider:
        return None
    labels = {
        "claude": "Claude",
        "codex": "Codex",
        "antigravity": "Antigravity",
    }
    return labels.get(provider, provider[:1].upper() + provider[1:])


def _runtime_is_executing(*, runtime_display, runtime_facts) -> bool:
    if runtime_display is not None:
        return bool(getattr(runtime_display, "is_executing", False))
    phase = getattr(runtime_facts, "phase", None)
    phase_kind = str(getattr(phase, "kind", "") or "").strip()
    return phase_kind in {"thinking", "running", "blocked", "stalled"}


def derive_session_liveness_facts(
    *,
    runtime_overlay: SessionRuntimeView | None,
    capability_flags,
    last_activity_at: datetime | None,
    binding_overlay=None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
    control_overlay=None,
    now: datetime | None = None,
):
    """Internal-only liveness-facts dataclass used to gate timeline_card and capabilities.

    Liveness facts are not part of the public response contract; clients consume
    ``runtime_display`` and ``timeline_card`` instead.
    """
    has_liveness_evidence = (
        runtime_overlay is not None
        or control_overlay is not None
        or binding_overlay is not None
        or binding_host_state is not None
        or binding_terminal_reason is not None
    )
    if not has_liveness_evidence:
        return None
    return build_session_liveness_facts(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        last_activity_at=last_activity_at,
        binding_overlay=binding_overlay,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
        control_overlay=control_overlay,
        now=now,
    )


def build_session_timeline_card_response(
    *,
    runtime_view: SessionRuntimeView | None,
    runtime_display: SessionRuntimeDisplayResponse,
    session_state: SessionStateFacts | None = None,
) -> TimelineCardPresentationResponse:
    """Derive the timeline card entirely from the public runtime_display projection.

    The runtime_view supplies the observation timestamps (presence_updated_at,
    last_progress_at, last_live_at). All semantic axes — control_path,
    lifecycle, state, tone, signal_tier — come from runtime_display, which is
    the single source of presentation truth.
    """
    if session_state is not None:
        primary = session_state.presentation.primary
        status = TimelineStatusPresentationResponse(
            label=primary.label if primary is not None else "No live signal",
            tone=primary.tone if primary is not None else "inactive",
            seen_at=primary.observed_at if primary is not None else None,
            seen_at_prefix="Closed" if primary is not None and primary.key == "closed" else "Updated",
        )
        return TimelineCardPresentationResponse(
            ownership=TimelineBadgePresentationResponse(
                label="Managed" if session_state.control.ownership == "owned" else "Unmanaged",
                tone="neutral",
            ),
            status=status,
            border_tone=status.tone,
        )

    ownership = TimelineBadgePresentationResponse(
        label="Managed" if runtime_display.control_path == ControlPath.MANAGED else "Unmanaged",
        tone="neutral",
    )
    status = _timeline_status_from_display(runtime_display, runtime_view=runtime_view)
    return TimelineCardPresentationResponse(
        ownership=ownership,
        status=status,
        border_tone=status.tone,
    )


def build_compat_runtime_display_response(
    *,
    session_state: SessionStateFacts,
    pause_request: dict[str, Any] | None,
    now: datetime,
) -> SessionRuntimeDisplayResponse:
    """Project deprecated display aliases from canonical facts only."""

    primary = session_state.presentation.primary
    activity = session_state.activity
    primary_key = primary.key if primary is not None else ""
    state_map = {
        "thinking": PresenceState.THINKING,
        "executing": PresenceState.RUNNING,
        "idle": PresenceState.IDLE,
        "blocked": PresenceState.BLOCKED,
        "stalled": PresenceState.STALLED,
        "needs_answer": PresenceState.NEEDS_USER,
        "needs_approval": PresenceState.NEEDS_USER,
    }
    tone_map = {
        "thinking": Tone.THINKING,
        "executing": Tone.RUNNING,
        "idle": Tone.IDLE,
        "blocked": Tone.BLOCKED,
        "stalled": Tone.STALLED,
        "needs_answer": Tone.BLOCKED,
        "needs_approval": Tone.BLOCKED,
        "closed": Tone.CLOSED,
        "ended": Tone.CLOSED,
        "starting": Tone.ACTIVE,
    }
    state = state_map.get(primary_key)
    closed = session_state.disposition.state == "closed"
    valid_until = normalize_utc(activity.valid_until)
    activity_current = valid_until is None or valid_until > now
    has_activity = activity.state != "unknown" and activity_current
    host_state = HostState(session_state.host.state)
    terminal_reason = None
    if closed and session_state.disposition.close_reason in {item.value for item in TerminalReason}:
        terminal_reason = TerminalReason(session_state.disposition.close_reason)
    pause_projection = None
    if pause_request is not None:
        try:
            pause_projection = SessionPauseRequestProjectionResponse.model_validate(pause_request)
        except ValueError:
            pause_projection = None
    return SessionRuntimeDisplayResponse(
        truth_tier=(
            TruthTier.MANAGED_LOCAL
            if has_activity and session_state.mode == "helm"
            else TruthTier.FRESH
            if has_activity
            else TruthTier.NONE
        ),
        signal_tier=SignalTier.PHASE_SIGNAL if has_activity else SignalTier.NONE,
        state=state,
        tone=tone_map.get(primary_key, Tone.INACTIVE),
        headline=primary.label if primary is not None else "Inactive",
        detail=(
            (pause_projection.summary or pause_projection.title)
            if pause_projection is not None
            else "Waiting for next prompt"
            if primary_key == "idle"
            else None
        ),
        phase_label=primary.label if primary is not None else "Inactive",
        compact_tool_label=activity.tool,
        is_live=activity.state in {"thinking", "executing"} and activity_current,
        is_executing=activity.state in {"thinking", "executing"} and activity_current,
        needs_attention=session_state.pending_interaction is not None,
        is_idle=closed or activity.state == "quiescent",
        is_stalled=activity.state == "stalled",
        is_managed_local_truth=session_state.mode == "helm",
        has_signal=primary is not None,
        control_path=(ControlPath.MANAGED if session_state.control.ownership == "owned" else ControlPath.UNMANAGED),
        activity_recency=ActivityRecency.LIVE if has_activity else ActivityRecency.NONE,
        lifecycle=Lifecycle.CLOSED if closed else Lifecycle.OPEN,
        host_state=host_state,
        terminal_reason=terminal_reason,
        pause_request=pause_projection,
    )


def project_compat_capabilities_from_state(
    capabilities: SessionCapabilitiesResponse,
    session_state: SessionStateFacts,
) -> SessionCapabilitiesResponse:
    """Keep old capability aliases read-only and derived from action facts."""

    actions = session_state.control.actions
    access = session_state.presentation.access
    access_key = access.key if access is not None else None
    console = session_state.mode == "console"
    send_available = (actions.start_turn if console else actions.send_input).state == "available"
    if console and session_state.disposition.state == "closed":
        send_available = False
    start_turn_blocked_by = capabilities.start_turn_blocked_by
    if console and actions.start_turn.state != "available":
        candidate = actions.start_turn.reason
        start_turn_blocked_by = (
            candidate
            if candidate in {"session_closed", "machine_offline", "adapter_unavailable", "execution_target_missing"}
            else "adapter_unavailable"
        )
    reattach_available = actions.reattach.state == "available"
    owned = session_state.control.ownership == "owned"
    compatibility_label = access.label if access is not None else "Read only"
    if access is None and session_state.presentation.primary is not None:
        if session_state.presentation.primary.key in {"starting", "launch_failed"}:
            compatibility_label = session_state.presentation.primary.label
    if console and send_available:
        compatibility_label = "Send"
    return capabilities.model_copy(
        update={
            "live_control_available": False if console else send_available,
            "host_reattach_available": reattach_available,
            "reply_to_live_session_available": send_available,
            "can_queue_next_input": send_available,
            "display_label": compatibility_label,
            "display_detail": (
                "Messages start or queue a turn on the selected machine." if console and send_available else capabilities.display_detail
            ),
            "display_tone": "success" if console and send_available else access.tone if access is not None else "neutral",
            "input_mode": "console" if console and send_available else "live" if send_available else ("offline" if owned else "read_only"),
            "composer_enabled": send_available,
            "control_label": (
                "console"
                if console
                else "live"
                if access_key == "live_control"
                else "reattach"
                if access_key == "reattach"
                else "search-only"
                if access_key == "observe_only"
                else "imported"
            ),
            "observe_only": access_key == "observe_only",
            "search_only": access_key == "search_only",
            "can_send_input": send_available,
            "can_start_turn": actions.start_turn.state == "available",
            "start_turn_blocked_by": (
                "session_closed" if console and session_state.disposition.state == "closed" else start_turn_blocked_by
            ),
            "can_interrupt": actions.interrupt.state == "available",
            "can_terminate": actions.terminate.state == "available",
            "can_resume": actions.resume.state == "available" or reattach_available,
        }
    )


def _timeline_status_from_display(
    runtime_display: SessionRuntimeDisplayResponse,
    *,
    runtime_view: SessionRuntimeView | None,
) -> TimelineStatusPresentationResponse:
    state = runtime_display.state.value if runtime_display.state is not None else None
    presence_at = normalize_utc(runtime_view.presence_updated_at) if runtime_view is not None else None
    progress_at = normalize_utc(runtime_view.last_progress_at) if runtime_view is not None else None
    last_live_at = normalize_utc(runtime_view.last_live_at) if runtime_view is not None else None

    if runtime_display.pause_request is not None and runtime_display.pause_request.status == "pending":
        return TimelineStatusPresentationResponse(
            label="Needs answer",
            tone="blocked",
            seen_at=presence_at or runtime_display.pause_request.last_seen_at,
            seen_at_prefix="Updated",
        )
    if runtime_display.lifecycle == Lifecycle.CLOSED:
        return TimelineStatusPresentationResponse(
            label="Closed",
            tone="closed",
            seen_at=progress_at or last_live_at,
            seen_at_prefix="Closed",
        )
    if state in {"thinking", "running", "idle", "needs_user", "blocked", "stalled"}:
        return TimelineStatusPresentationResponse(
            label=_phase_status_label(state, runtime_display.compact_tool_label),
            tone=_phase_tone(state),
            seen_at=presence_at,
            seen_at_prefix="Updated",
        )
    if (
        runtime_display.signal_tier == SignalTier.PROCESS_BINDING
        and runtime_display.host_state == HostState.ONLINE
        and runtime_display.lifecycle == Lifecycle.OPEN
    ):
        return TimelineStatusPresentationResponse(
            label="Running",
            tone="inactive",
            seen_at=progress_at or last_live_at,
            seen_at_prefix="Verified",
        )
    last_signal = presence_at or last_live_at
    return TimelineStatusPresentationResponse(
        label="No live signal",
        tone="inactive",
        seen_at=last_signal,
        seen_at_prefix="Last signal" if last_signal is not None else "Checked",
    )


def _phase_status_label(kind: str, compact_tool: str | None) -> str:
    phase = "idle" if kind == "needs_user" else kind.replace("_", " ").replace("-", " ")
    if compact_tool and kind == "running":
        return f"Using {compact_tool}"
    if compact_tool and kind == "blocked":
        return f"{_title_case_words(phase)} {compact_tool}"
    return _title_case_words(phase)


def _phase_tone(kind: str) -> str:
    if kind in {"thinking", "running", "blocked", "stalled"}:
        return kind
    if kind in {"idle", "needs_user"}:
        return "idle"
    return "inactive"


def _title_case_words(value: str) -> str:
    words = [word for word in value.split() if word]
    out: list[str] = []
    for word in words:
        if len(word) <= 3 and word == word.upper():
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def build_session_control_response(
    session: AgentSession | None,
    *,
    db,
    capability_flags=None,
    control_projection: SessionControlProjection | None = None,
) -> SessionControlResponse | None:
    if session is None:
        return None
    if capability_flags is None:
        raise RuntimeError("capability_flags is required; the kernel adapter must build them")
    control_projection = control_projection or project_session_control_fields(db, session, capabilities=capability_flags)
    source_runner_name = control_projection.source_runner_name
    attach_command = build_attach_command(session) if capability_flags.host_reattach_available else None
    if control_projection.source_runner_id is None and source_runner_name is None and attach_command is None:
        return None
    return SessionControlResponse(
        source_runner_id=control_projection.source_runner_id,
        source_runner_name=source_runner_name,
        attach_command=attach_command,
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionControlResponse(BaseModel):
    source_runner_id: Optional[int] = Field(None, description="Runner id for managed local sessions")
    source_runner_name: Optional[str] = Field(None, description="Runner name for managed local sessions")
    attach_command: Optional[str] = Field(None, description="Local reattach command for managed-local sessions")


class SessionContinueTarget(BaseModel):
    """Compact native continuation target exposed to web/iOS clients."""

    provider: str = Field(..., description="Provider that can resume this target")
    device_id: str | None = Field(None, description="Recorded source device id for the session")
    cwd: str | None = Field(None, description="Recorded working directory for the session")
    carry_context: Literal["native"] = Field("native", description="Continuation context strategy")
    native_resume_available: bool = Field(True, description="True when provider-native resume data exists")
    adoption_mode: Literal["managed_resume", "adopt_unmanaged"] = Field(
        "managed_resume",
        description=(
            "managed_resume: re-launch an already-managed session. "
            "adopt_unmanaged: explicitly bring an imported/raw transcript under "
            "Longhouse management by launching a fresh managed process."
        ),
    )


class SessionCapabilitiesResponse(BaseModel):
    live_control_available: bool = Field(False, description="True when Longhouse can inject into the live session now")
    host_reattach_available: bool = Field(False, description="True when this session can be resumed from its host terminal")
    reply_to_live_session_available: bool = Field(
        False,
        description="True when operator flows may send a direct reply into the live session",
    )
    can_queue_next_input: bool = Field(
        False,
        description="True when the user can queue input to auto-send at the next safe turn boundary",
    )
    can_steer_active_turn: bool = Field(
        False,
        description="True when mid-turn steer is likely to land; the active turn may still end before the call arrives",
    )
    display_label: str = Field("Read only", description="User-facing capability label")
    display_detail: str = Field(
        "This imported session is searchable, but Longhouse cannot steer it.",
        description="User-facing capability explanation",
    )
    display_tone: str = Field("neutral", description="Stable capability tone for clients")
    input_mode: Literal["live", "console", "offline", "read_only"] = Field(
        "read_only",
        description="Canonical input/composer availability state for clients",
    )
    default_input_intent: Literal["auto", "steer", "queue", "none"] = Field(
        "none",
        description="Default POST input intent clients should use for the primary send action",
    )
    composer_enabled: bool = Field(False, description="True when clients should render an enabled composer")
    composer_placeholder: str = Field("Type a message...", description="Default composer placeholder text")
    composer_disabled_reason: Optional[str] = Field(None, description="User-facing reason the composer is disabled")
    send_disabled_reason: Optional[SendDisabledReason] = Field(
        None,
        description="Stable reason code when the primary send action is disabled",
    )
    control_label: Optional[Literal["live", "console", "reattach", "search-only", "imported"]] = Field(
        None,
        description="Kernel-projected control bucket for this session",
    )
    observe_only: bool = Field(
        False,
        description="True when Longhouse can read transcript output but cannot steer this session",
    )
    search_only: bool = Field(
        False,
        description="True when this session is an imported transcript with no active control plane",
    )
    staleness_reason: Optional[str] = Field(
        None,
        description=("When live_control_available is False, why: e.g. no_run, connection_released, process_ended, imported_only"),
    )
    can_send_input: bool = Field(False, description="Kernel: connection grants send-input capability and is currently live")
    can_interrupt: bool = Field(False, description="Kernel: connection grants interrupt capability and is currently live")
    can_terminate: bool = Field(False, description="Kernel: connection grants terminate capability and is currently live")
    can_tail_output: bool = Field(False, description="Kernel: connection grants output tailing capability")
    can_resume: bool = Field(False, description="Kernel: connection can be resumed (live or reattach)")
    turn_state: Literal["idle", "queued", "starting", "active", "draining"] = Field(
        "idle",
        description="Durable Console turn state; independent of provider process identity",
    )
    can_start_turn: bool = Field(False, description="True when Console can accept a normal message now")
    start_turn_blocked_by: Optional[Literal["session_closed", "machine_offline", "adapter_unavailable", "execution_target_missing"]] = (
        Field(None, description="Stable reason Console cannot accept a normal message")
    )
    can_interrupt_active_turn: bool = Field(False, description="True when the active Console turn can be interrupted")
    attach_images: bool = Field(
        False,
        description="True when the session can accept image attachments on input (codex_app_server only)",
    )
    can_continue: bool = Field(
        False,
        description="True when Longhouse has a native continuation target for this session",
    )
    continue_targets: list[SessionContinueTarget] = Field(
        default_factory=list,
        description="Compact continuation targets available to clients",
    )


class SessionPauseQuestionOptionResponse(BaseModel):
    label: str = Field(..., description="User-facing option label")
    description: Optional[str] = Field(None, description="Optional explanatory text for the option")
    value: Optional[str] = Field(None, description="Provider-native option value when distinct from label")


class SessionPauseQuestionResponse(BaseModel):
    id: str = Field(..., description="Stable question identifier")
    header: Optional[str] = Field(None, description="Short question header")
    question: str = Field(..., description="Question text")
    multi_select: bool = Field(False, description="True when multiple options may be selected")
    options: list[SessionPauseQuestionOptionResponse] = Field(default_factory=list, description="Provider-native answer options")


class SessionPauseRequestProjectionResponse(UTCBaseModel):
    id: str = Field(..., description="Pause request UUID")
    session_id: str = Field(..., description="Session UUID")
    runtime_key: str = Field(..., description="Runtime key that emitted this request")
    kind: Literal["structured_question", "permission_prompt", "plan_approval"] = Field(..., description="Pause request kind")
    status: Literal["pending", "resolved", "rejected", "failed", "expired"] = Field(..., description="Pause lifecycle status")
    provider: str = Field(..., description="Provider that emitted the request")
    can_respond: bool = Field(..., description="True when Longhouse can answer through a provider-native path")
    title: Optional[str] = Field(None, description="Short title for the request")
    summary: Optional[str] = Field(None, description="Short user-facing detail for the request")
    tool_name: Optional[str] = Field(None, description="Provider tool/dialog name when known")
    questions: list[SessionPauseQuestionResponse] = Field(default_factory=list, description="Structured questions to render")
    occurred_at: Optional[datetime] = Field(None, description="When the provider emitted the request")
    last_seen_at: Optional[datetime] = Field(None, description="When Longhouse last observed the request")
    resolved_at: Optional[datetime] = Field(None, description="When the request resolved")
    expires_at: Optional[datetime] = Field(None, description="Optional provider/request expiry")


class SessionRuntimeDisplayResponse(BaseModel):
    truth_tier: TruthTier = Field(..., description="Runtime truth tier")
    signal_tier: SignalTier = Field(..., description="Strongest source signal tier")
    state: Optional[PresenceState] = Field(..., description="Canonical presence state, or null when unknown")
    tone: Tone = Field(..., description="Stable visual tone for clients")
    headline: str = Field(..., description="Primary user-facing runtime label")
    detail: Optional[str] = Field(..., description="Secondary user-facing runtime label, or null")
    phase_label: str = Field(..., description="Compact phase label for cards and strips")
    compact_tool_label: Optional[str] = Field(..., description="Normalized tool label for display, or null")
    is_live: bool = Field(..., description="True when the session is actively executing")
    is_executing: bool = Field(..., description="True when the agent is thinking or running a tool")
    needs_attention: bool = Field(..., description="True when the user should respond or approve")
    is_idle: bool = Field(..., description="True when the runtime is waiting for another turn")
    is_stalled: bool = Field(..., description="True when a provider explicitly reports stalled state")
    is_managed_local_truth: bool = Field(..., description="True when runtime truth is from a managed-local control path")
    has_signal: bool = Field(..., description="True when clients should render runtime state")
    control_path: ControlPath = Field(..., description="Does Longhouse own a control path?")
    activity_recency: ActivityRecency = Field(..., description="How recently we heard from this session")
    lifecycle: Lifecycle = Field(..., description="Session lifecycle. Closed only with ground truth.")
    host_state: HostState = Field(..., description="Host/machine verifiability")
    terminal_reason: Optional[TerminalReason] = Field(
        ...,
        description="Why the session is closed (when lifecycle=='closed'), else null",
    )
    pause_request: Optional[SessionPauseRequestProjectionResponse] = Field(
        None,
        description="Active structured provider question, when the runtime is waiting for an answer.",
    )


class SessionTranscriptPreviewResponse(UTCBaseModel):
    event_id: int = Field(..., description="AgentEvent id for this preview row")
    text: str = Field(..., description="Transcript preview text from the event ledger")
    event_origin: str = Field(..., description="Event origin: durable|live_provisional")
    timestamp: Optional[datetime] = Field(None, description="Event timestamp used for transcript ordering")
    is_provisional: bool = Field(..., description="True when the preview is from an active provisional event")
    is_complete: bool = Field(
        False,
        description="True when the provider bridge reported this provisional turn complete",
    )
    content_cursor: Optional[str] = Field(None, description="Monotonic live snapshot cursor for provisional previews")
    is_stale: bool = Field(False, description="True when the provisional preview is too old to render as live output")
    stale_reason: Optional[Literal["freshness_window_expired", "missing_preview_timestamp", "superseded_by_durable"]] = Field(
        None,
        description="Why a provisional preview is stale, when known.",
    )


class TimelineBadgePresentationResponse(UTCBaseModel):
    label: str = Field(..., description="Stable user-facing badge label")
    tone: str = Field(..., description="Stable visual tone token for clients")


class TimelineStatusPresentationResponse(TimelineBadgePresentationResponse):
    seen_at: Optional[datetime] = Field(None, description="Signal timestamp for stale status copy")
    seen_at_prefix: str = Field(..., description="Server-owned word that qualifies the status timestamp")


class TimelineCardPresentationResponse(UTCBaseModel):
    ownership: TimelineBadgePresentationResponse = Field(..., description="Managed/unmanaged badge")
    status: TimelineStatusPresentationResponse = Field(..., description="Primary timeline status badge")
    border_tone: str = Field("inactive", description="Stable tone token for the card edge/outline")


class SessionSharerResponse(UTCBaseModel):
    """Public-safe attribution for the user who shared this session link.

    Resolved server-side from a ``?shared_by=<user_id>`` query param. The pill
    on the session header is the only consumer; the same field doubles as
    "who is the owner of this session" in single-tenant deployments.
    """

    id: int = Field(..., description="Sharing user id")
    display_name: Optional[str] = Field(None, description="Display name (null falls back to email local on the client)")


class SessionResponse(UTCBaseModel):
    """Response for a single session."""

    id: str = Field(..., description="Session UUID")
    origin_kind: Optional[str] = Field(None, description="Canonical session origin: console or imported provider transcript.")
    provider: str = Field(..., description="AI provider")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device ID")
    environment: Optional[str] = Field(None, description="Environment (production, development, test, e2e, automation)")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    user_messages: int = Field(..., description="User message count")
    assistant_messages: int = Field(..., description="Assistant message count")
    tool_calls: int = Field(..., description="Tool call count")
    last_activity_at: Optional[datetime] = Field(None, description="Most recent transcript activity timestamp")
    timeline_anchor_at: Optional[datetime] = Field(None, description="Recency anchor used for timeline ordering")
    runtime_phase: Optional[str] = Field(None, description="Canonical runtime phase")
    phase_started_at: Optional[datetime] = Field(None, description="When the current runtime phase began")
    last_progress_at: Optional[datetime] = Field(None, description="Most recent progress signal timestamp")
    runtime_source: Optional[str] = Field(None, description="Materialized runtime source: semantic|progress|fallback")
    terminal_state: Optional[str] = Field(None, description="Terminal runtime state when known")
    runtime_version: Optional[int] = Field(None, description="Monotonic runtime version for patch ordering")
    status: Optional[str] = Field(None, description="Derived runtime status (working, active, idle, completed)")
    presence_state: Optional[str] = Field(None, description="Fresh presence signal when available")
    presence_tool: Optional[str] = Field(None, description="Tool currently executing (when applicable)")
    presence_updated_at: Optional[datetime] = Field(None, description="When presence was last signalled")
    last_live_at: Optional[datetime] = Field(None, description="Most recent live-signal timestamp")
    display_phase: Optional[str] = Field(None, description="User-facing runtime phase label")
    active_tool: Optional[str] = Field(None, description="Active tool label for runtime display")
    confidence: Optional[str] = Field(None, description="Runtime confidence: live|stale")
    summary: Optional[str] = Field(None, description="Session summary")
    summary_title: Optional[str] = Field(None, description="Short session title (drifts as transcript grows)")
    anchor_title: Optional[str] = Field(None, description="Frozen, write-once headline; stable across the session's life")
    timeline_title: Optional[str] = Field(
        None,
        description=(
            "Resolved headline a client should render: frozen anchor_title, else ready "
            "summary_title, else sanitized first message, else 'Summarizing…'/structured fallback. "
            "Always non-empty. Clients render this verbatim — no client-side fallback ladder."
        ),
    )
    title_state: Optional[str] = Field(
        None,
        description="AI-title lifecycle: awaiting_input|pending|degraded|ready.",
    )
    title_source: Optional[str] = Field(
        None,
        description="Title provenance: ai|prompt|project. Non-ai sources are fallback context.",
    )
    summary_status: Optional[str] = Field(
        None,
        description=(
            "Honest summarization state: ready (summary present), pending (task queued/running), "
            "failed (terminal — won't auto-retry), unavailable (no task / too little content). "
            "Tiebreaker: ready > pending > failed > unavailable."
        ),
    )
    first_user_message: Optional[str] = Field(None, description="First user message (truncated)")
    match_event_id: Optional[int] = Field(None, description="Matching event id for search queries")
    match_snippet: Optional[str] = Field(None, description="Snippet of matching content")
    match_role: Optional[str] = Field(None, description="Role for matching event")
    match_score: Optional[float] = Field(None, description="Semantic similarity score (0-1) when result is from vector search")
    thread_root_session_id: str = Field(..., description="Logical thread root session UUID")
    thread_head_session_id: str = Field(..., description="Current writable head session UUID")
    thread_continuation_count: int = Field(..., description="Number of concrete continuations in this logical thread")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(
        None,
        description="Kernel branch kind for non-root threads; null for root threads",
    )
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label")
    home_label: Optional[str] = Field(None, description="User-facing home label, e.g. On this Mac|Hosted|Moved to cloud")
    branched_from_event_id: Optional[int] = Field(None, description="Event id where this continuation branched")
    is_writable_head: bool = Field(False, description="True when this session is the current writable head")
    is_sidechain: bool = Field(False, description="True when session is a Task sub-agent (not human-initiated)")
    control: Optional[SessionControlResponse] = Field(None, description="Host-control and managed-launch debugging detail")
    capabilities: SessionCapabilitiesResponse = Field(..., description="Canonical session capability flags")
    session_state: SessionStateFacts = Field(..., description="Versioned orthogonal session facts and presentation")
    runtime_display: SessionRuntimeDisplayResponse = Field(..., description="Server-derived display state for clients")
    transcript_preview: Optional[SessionTranscriptPreviewResponse] = Field(
        None,
        description="Latest renderable transcript preview sourced from the event ledger.",
    )
    timeline_card: TimelineCardPresentationResponse = Field(
        ...,
        description="Server-derived timeline-card presentation",
    )
    loop_mode: SessionLoopMode = Field(SessionLoopMode.ASSIST, description="Session loop mode: assist|autopilot")
    user_state: str = Field("active", description="User classification: active|parked|snoozed|archived")
    launch_state: Optional[RemoteLaunchLifecycleState] = Field(
        None,
        description=(
            "Remote-launch lifecycle: launching|live|launching_unknown|launch_failed|launch_orphaned; null when there is no launch attempt"
        ),
    )
    execution_lifetime: Optional[RemoteExecutionLifetime] = Field(
        None,
        description="Remote launch execution lifetime: one_shot|live_control; null when there is no launch attempt",
    )
    launch_error_code: Optional[RemoteLaunchErrorCode] = Field(
        None,
        description="Remote-launch error code when launch_state=launch_failed/launch_orphaned",
    )
    launch_error_message: Optional[str] = Field(
        None, description="Remote-launch error message when launch_state=launch_failed/launch_orphaned"
    )
    sharer: Optional[SessionSharerResponse] = Field(
        None,
        description=(
            "Attribution for the user whose signed share token or legacy "
            "?shared_by=<id> link surfaced this session. Null when attribution "
            "is absent, the user is gone, or the sharer is the current viewer."
        ),
    )


class SessionSummaryResponse(UTCBaseModel):
    """Response for session summaries (picker UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    duration_minutes: Optional[int] = Field(None, description="Duration in minutes")
    turn_count: int = Field(..., description="Number of user messages (exchanges)")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_ai_message: Optional[str] = Field(None, description="Last assistant message (truncated)")


class SessionsSummaryResponse(BaseModel):
    """Response for session summary list."""

    sessions: List[SessionSummaryResponse]
    total: int


class SessionsListResponse(BaseModel):
    """Response for session list."""

    sessions: List[SessionResponse]
    total: int
    has_real_sessions: bool = Field(
        True,
        description=("True if any non-demo sessions exist (device_id != 'demo-mac'). False means only demo-seeded data is present."),
    )


class SessionThreadResponse(BaseModel):
    """Response for a logical thread and its concrete continuations."""

    root_session_id: str
    head_session_id: str
    sessions: List[SessionResponse]


class StartupContextItemResponse(UTCBaseModel):
    """One recent session summary for startup continuity injection."""

    session_id: str = Field(..., description="Session UUID")
    thread_root_session_id: str = Field(..., description="Logical thread root UUID")
    provider: str = Field(..., description="Session provider")
    started_at: datetime = Field(..., description="Session start time")
    age: str = Field(..., description="Human-readable recency label")
    summary_title: str = Field(..., description="Short session title")
    summary: str = Field(..., description="Sanitized summary text")


class StartupContextResponse(BaseModel):
    """Response envelope for startup continuity context."""

    project: str = Field(..., description="Project label used for lookup")
    session_count: int = Field(..., description="Number of sessions included in the context")
    items: List[StartupContextItemResponse] = Field(..., description="Recent project sessions used for continuity")
    startup_context: Optional[str] = Field(None, description="Rendered context block for provider hook injection")


class SessionPreviewMessage(UTCBaseModel):
    """Preview message entry for session picker."""

    role: str = Field(..., description="Message role")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")


class SessionPreviewResponse(BaseModel):
    """Response for session preview endpoint."""

    id: str = Field(..., description="Session UUID")
    messages: List[SessionPreviewMessage] = Field(..., description="Recent messages")
    total_messages: int = Field(..., description="Total message count")


class ActiveSessionResponse(UTCBaseModel):
    """Response for active session summary (Live Sessions UI)."""

    id: str = Field(..., description="Session UUID")
    project: Optional[str] = Field(None, description="Project name")
    provider: str = Field(..., description="AI provider")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_branch: Optional[str] = Field(None, description="Git branch")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    last_activity_at: datetime = Field(..., description="Most recent transcript activity timestamp")
    timeline_anchor_at: datetime = Field(..., description="Recency anchor used for live ordering")
    runtime_phase: Optional[str] = Field(None, description="Canonical runtime phase")
    phase_started_at: Optional[datetime] = Field(None, description="When the current runtime phase began")
    last_progress_at: Optional[datetime] = Field(None, description="Most recent progress signal timestamp")
    runtime_source: Optional[str] = Field(None, description="Materialized runtime source: semantic|progress|fallback")
    terminal_state: Optional[str] = Field(None, description="Terminal runtime state when known")
    runtime_version: Optional[int] = Field(None, description="Monotonic runtime version for patch ordering")
    status: str = Field(..., description="Session status (working, active, idle, completed)")
    attention: str = Field(..., description="Attention level (auto by default)")
    duration_minutes: int = Field(..., description="Duration in minutes")
    last_user_message: Optional[str] = Field(None, description="Last user message (truncated)")
    last_assistant_message: Optional[str] = Field(None, description="Last assistant message (truncated)")
    message_count: int = Field(..., description="Total user + assistant messages")
    tool_calls: int = Field(..., description="Tool call count")
    presence_state: Optional[str] = Field(None, description="Real-time state: thinking|running|idle|needs_user|blocked")
    presence_tool: Optional[str] = Field(None, description="Tool currently executing (when state=running or blocked)")
    presence_updated_at: Optional[datetime] = Field(None, description="When presence was last signalled")
    last_live_at: Optional[datetime] = Field(None, description="Most recent live-signal timestamp")
    display_phase: Optional[str] = Field(None, description="User-facing runtime phase label")
    active_tool: Optional[str] = Field(None, description="Active tool label for runtime display")
    confidence: Optional[str] = Field(None, description="Runtime confidence: live|stale")
    user_state: str = Field("active", description="User classification: active|parked|snoozed|archived")
    home_label: Optional[str] = Field(None, description="User-facing home label, e.g. On this Mac|Hosted|Moved to cloud")
    control: Optional[SessionControlResponse] = Field(None, description="Host-control and managed-launch debugging detail")
    capabilities: SessionCapabilitiesResponse = Field(..., description="Canonical session capability flags")
    session_state: SessionStateFacts = Field(..., description="Versioned orthogonal session facts and presentation")
    runtime_display: SessionRuntimeDisplayResponse = Field(..., description="Server-derived display state for clients")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.ASSIST, description="Session loop mode: assist|autopilot")


class ActiveSessionsResponse(UTCBaseModel):
    """Response for active session list."""

    sessions: List[ActiveSessionResponse]
    total: int
    last_refresh: datetime


class WallSessionResponse(UTCBaseModel):
    """A session's raw signal for the wall view. Schema-on-read: raw timestamps,
    no status bucketing. The consuming agent or UI decides relevance."""

    session_id: str
    device_name: Optional[str] = None
    device_id: Optional[str] = None
    cwd: Optional[str] = None
    git_repo: Optional[str] = None
    git_branch: Optional[str] = None
    project: Optional[str] = None
    provider: str
    summary_title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    last_user_message_at: Optional[datetime] = None
    last_tool_call_at: Optional[datetime] = None
    has_live_presence: bool = False
    presence_state: Optional[str] = None
    kernel_control_label: Optional[Literal["live", "reattach", "search-only", "imported"]] = Field(
        None,
        description="Raw kernel-projected control bucket. Not runtime/offline clamped.",
    )
    kernel_live_control_available: bool = Field(False, description="Raw kernel live-control bit before runtime/offline clamping.")
    kernel_host_reattach_available: bool = Field(False, description="Raw kernel host-reattach bit before lifecycle clamping.")
    kernel_observe_only: bool = Field(False, description="Raw kernel bit: Longhouse can observe output but not steer.")
    kernel_search_only: bool = Field(False, description="Raw kernel bit: imported/search-only transcript.")
    kernel_staleness_reason: Optional[str] = Field(None, description="Raw kernel reason live control is unavailable, when known.")
    pending_inbound_messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0


class WallResponse(UTCBaseModel):
    """Wall query response — sessions indexed by raw signal."""

    sessions: List[WallSessionResponse]
    total: int


class InputOriginResponse(BaseModel):
    """Semantic origin for a user-authored transcript event."""

    authored_via: Literal["longhouse", "terminal"] = Field(
        ...,
        description="Where this user input was authored: longhouse|terminal",
    )
    session_input_id: Optional[int] = Field(None, description="SessionInput row when authored through Longhouse")
    client_request_id: Optional[str] = Field(
        None,
        description="Client idempotency key when supplied by the Longhouse client",
    )


class ToolCallState(str, Enum):
    """Per-event projection of tool-call lifecycle for assistant tool events.

    Computed server-side from event pairings + session lifecycle. Clients must
    not re-derive this; the server is authoritative.

    - ``running``: assistant tool call is awaiting its result and the session
      is still active and recent.
    - ``completed``: a paired tool result has been observed.
    - ``dropped``: the result will never arrive (session is closed, or the
      call is older than the dropped-tool age threshold).
    """

    RUNNING = "running"
    COMPLETED = "completed"
    DROPPED = "dropped"


class EventMediaRefResponse(UTCBaseModel):
    """Projected media reference for an event."""

    sha256: str = Field(..., description="Content-addressed media sha256")
    media_state: str = Field(..., description="Media lifecycle state: pending|present|failed")
    mime_type: Optional[str] = Field(None, description="Stored media MIME type when present")
    byte_size: Optional[int] = Field(None, description="Stored media byte size when present")
    blob_url: str = Field(..., description="Browser/API blob URL")
    thumb_url: Optional[str] = Field(None, description="Browser/API thumbnail URL when a thumbnail exists")
    source_path: Optional[str] = Field(None, description="Provider source path that contained the media reference")
    source_offset: Optional[int] = Field(None, description="Provider source byte offset for the media reference")
    json_pointer: Optional[str] = Field(None, description="JSON pointer to the redacted media field when known")
    original_kind: str = Field(..., description="Original media source kind")


class EventResponse(UTCBaseModel):
    """Response for a single event."""

    id: int | str = Field(..., description="Stable legacy or storage-v2 event ID")
    role: str = Field(..., description="Message role")
    content_text: Optional[str] = Field(None, description="Message content")
    raw_content_text: Optional[str] = Field(
        None,
        description="Raw provider content when it differs from display content",
    )
    input_origin: Optional[InputOriginResponse] = Field(
        None,
        description="Semantic origin for user-authored input events",
    )
    tool_name: Optional[str] = Field(None, description="Tool name")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool input")
    tool_output_text: Optional[str] = Field(None, description="Tool output")
    tool_output_truncated: bool = Field(False, description="True when tool_output_text was shortened for this response")
    tool_output_original_chars: Optional[int] = Field(None, description="Original tool output length when truncated")
    tool_call_id: Optional[str] = Field(None, description="Cross-provider call/result linkage ID")
    timestamp: datetime = Field(..., description="Event timestamp")
    in_active_context: bool = Field(
        True,
        description="True when event is inside the current active model context boundary",
    )
    branch_id: Optional[int] = Field(None, description="Session branch ID for rewind-aware projections")
    is_head_branch: bool = Field(True, description="True when event belongs to the active head branch")
    event_origin: str = Field("durable", description="Event origin: durable|live_provisional")
    provisional_state: Optional[str] = Field(None, description="Provisional lifecycle state when event_origin=live_provisional")
    provisional_cursor: Optional[str] = Field(None, description="Monotonic live snapshot cursor for provisional events")
    provisional_complete: bool = Field(False, description="True when the provider bridge reported the provisional turn complete")
    reconciled_event_id: Optional[int] = Field(None, description="Durable event id that replaced this provisional event")
    tool_call_state: Optional[ToolCallState] = Field(
        None,
        description=(
            "Lifecycle of an assistant tool call: running|completed|dropped. "
            "Set only on assistant events that have tool_name. Server-authoritative."
        ),
    )
    media_refs: List[EventMediaRefResponse] = Field(
        default_factory=list,
        description="Content-addressed media objects referenced by this event",
    )


class TranscriptActionResponse(UTCBaseModel):
    """Provider-neutral lifecycle/control action projected into a transcript."""

    id: str = Field(..., description="Stable action id within the projection")
    kind: str = Field(..., description="Action kind, e.g. turn_interrupted")
    provider: Optional[str] = Field(None, description="Provider that emitted the action evidence")
    source: str = Field("unknown", description="Action source: user|remote_control|provider|system|unknown")
    provider_reason: Optional[str] = Field(None, description="Provider-specific reason or compatibility marker")
    event_id: Optional[int] = Field(None, description="Backing event id when the action projects from an event row")


class EventsListResponse(BaseModel):
    """Response for events list."""

    events: List[EventResponse]
    total: int
    branch_mode: str = Field("head", description="Branch projection mode: head|all")
    abandoned_events: int = Field(0, description="Events excluded from head projection due to rewind branches")
    generation_id: Optional[str] = Field(None, description="Storage-v2 render generation for cursor validation")
    next_cursor: Optional[str] = Field(None, description="Exclusive cursor for the next storage-v2 page")
    has_more: bool = Field(False, description="Whether another storage-v2 page is available")


class SessionTurnTimingResponse(UTCBaseModel):
    """Derived durations computed from canonical turn timestamps."""

    submit_to_send_ms: Optional[int] = Field(None, description="send_accepted_at - user_submitted_at")
    submit_to_active_ms: Optional[int] = Field(None, description="active_phase_observed_at - user_submitted_at")
    submit_to_terminal_ms: Optional[int] = Field(None, description="terminal_at - user_submitted_at")
    active_to_terminal_ms: Optional[int] = Field(None, description="terminal_at - active_phase_observed_at")
    terminal_to_durable_ms: Optional[int] = Field(None, description="durable_at - terminal_at")
    total_turn_time_ms: Optional[int] = Field(
        None,
        description="Best available completion time: (durable_at or terminal_at) - user_submitted_at",
    )


class SessionTurnResponse(UTCBaseModel):
    """Canonical public timing fields for one session turn."""

    id: int = Field(..., description="Turn integer id")
    session_id: str = Field(..., description="Owning session UUID")
    request_id: Optional[str] = Field(
        None,
        description=("Transport request id when available, otherwise a synthetic canonical id for reconstructed native turns"),
    )
    session_input_id: Optional[int] = Field(
        None,
        description="SessionInput row that authored this turn, when any",
    )
    state: str = Field(..., description="created|send_accepted|active|terminal|durable|failed")
    terminal_phase: Optional[str] = Field(None, description="Observed terminal phase when known")
    error_code: Optional[str] = Field(None, description="Canonical irrecoverable error code when failed")
    user_event_id: Optional[int] = Field(None, description="Triggering durable user event id")
    durable_assistant_event_id: Optional[int] = Field(None, description="Durable assistant event id that closed the turn")
    baseline_event_id: Optional[int] = Field(None, description="Latest durable event id observed before the turn began")
    baseline_observation_cursor: Optional[int] = Field(None, description="Latest runtime observation cursor before the turn began")
    user_submitted_at: datetime = Field(..., description="When the user prompt was accepted as a turn")
    send_accepted_at: Optional[datetime] = Field(None, description="When transport accepted the prompt send")
    active_phase_observed_at: Optional[datetime] = Field(None, description="When Longhouse first observed active runtime work")
    terminal_at: Optional[datetime] = Field(None, description="When the turn reached terminal phase")
    durable_at: Optional[datetime] = Field(None, description="When transcript durability was established")
    created_at: Optional[datetime] = Field(None, description="Row creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Row update timestamp")
    timing: SessionTurnTimingResponse = Field(
        ...,
        description="Derived read-time durations between canonical turn milestones",
    )


class SessionTurnsListResponse(BaseModel):
    """Response for a stable per-session turn listing."""

    turns: List[SessionTurnResponse]
    total: int


class SessionTurnEnvelopeResponse(BaseModel):
    """Envelope for turn detail responses."""

    turn: SessionTurnResponse


class SessionProjectionItemResponse(UTCBaseModel):
    """One stitched item in a selected session's projected lineage path."""

    kind: str = Field(..., description="Projection item kind: event|seam|action")
    session_id: str = Field(..., description="Concrete session UUID for this item")
    timestamp: datetime = Field(..., description="Timestamp used for item ordering and display")
    event: Optional[EventResponse] = Field(None, description="Present when kind=event")
    action: Optional[TranscriptActionResponse] = Field(None, description="Present when kind=action")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID for seams")
    continuation_kind: Optional[str] = Field(None, description="Kernel branch kind for seam items")
    origin_label: Optional[str] = Field(None, description="Origin label for seam items")
    parent_origin_label: Optional[str] = Field(None, description="Origin label for the parent segment")
    parent_continuation_kind: Optional[str] = Field(None, description="Kernel branch kind for the parent segment")
    branched_from_event_id: Optional[int] = Field(None, description="Event id where the child continuation branched")


class SessionProjectionResponse(BaseModel):
    """Response for a stitched lineage-path projection."""

    root_session_id: str
    focus_session_id: str
    head_session_id: str
    path_session_ids: List[str]
    items: List[SessionProjectionItemResponse]
    total: int
    page_offset: int = Field(0, description="Offset of the first item in this page within the full projection")
    branch_mode: str = Field("head", description="Branch projection mode: head|all")
    abandoned_events: int = Field(0, description="Events excluded from head projection due to rewind branches")
    generation_id: Optional[str] = Field(None, description="Storage-v2 render generation for cursor validation")
    next_cursor: Optional[str] = Field(None, description="Exclusive cursor for the next storage-v2 page")
    has_more: bool = Field(False, description="Whether another storage-v2 page is available")


class SessionWorkspaceRevisionResponse(BaseModel):
    """Durable fingerprint for session viewport-visible state."""

    latest_event_id: int = Field(0, description="Latest durable event id included in the viewport signature")
    latest_session_updated_at: Optional[datetime] = Field(None, description="Latest session row update in the viewport path")
    latest_runtime_signal_at: Optional[datetime] = Field(None, description="Latest runtime-state update in the viewport path")
    runtime_version_sum: int = Field(0, description="Sum of runtime versions in the viewport path")
    pause_request_count: int = Field(0, description="Active pause requests included in the viewport signature")
    pause_request_fingerprint: Optional[str] = Field(None, description="Hash of active pause-request viewport state")
    managed_control_count: int = Field(0, description="Managed control connections included in the viewport signature")
    managed_control_fingerprint: Optional[str] = Field(None, description="Hash of managed-control viewport state")
    live_preview_updated_at: Optional[datetime] = Field(None, description="Latest live preview update in the viewport path")
    thread_session_count: int = Field(0, description="Number of sessions in the viewport path")
    fingerprint: str = Field(..., description="Hash of the complete durable viewport signature")


class SessionWorkspaceResponse(BaseModel):
    """Response for the primary session workspace bootstrap payload."""

    session: SessionResponse = Field(..., description="Focused session metadata")
    thread: SessionThreadResponse = Field(..., description="Logical thread continuations for the focused session")
    projection: SessionProjectionResponse = Field(..., description="First page of the stitched lineage projection")
    workspace_revision: SessionWorkspaceRevisionResponse = Field(..., description="Durable viewport freshness revision")
    control_only: bool = Field(
        False,
        description="True when the session has a live managed control path but no transcript source yet.",
    )


class SessionMobileTailResponse(BaseModel):
    """Small bootstrap payload for mobile session reads."""

    session: SessionResponse = Field(..., description="Focused session metadata")
    projection: SessionProjectionResponse = Field(..., description="Tail page of the stitched lineage projection")
    snapshot_event_id: Optional[int] = Field(None, description="Latest durable event id used to anchor older-page fetches")
    workspace_revision: SessionWorkspaceRevisionResponse = Field(..., description="Durable viewport freshness revision")


class IngestResponse(BaseModel):
    """Response for ingest endpoint."""

    session_id: str
    events_inserted: int
    events_skipped: int
    session_created: bool


class FiltersResponse(BaseModel):
    """Response for filters endpoint."""

    projects: List[str]
    providers: List[str]
    machines: List[str] = []


class DemoSeedResponse(BaseModel):
    """Response for demo session seeding."""

    seeded: bool
    sessions_created: int
    sessions_failed: int = 0
    sessions_deleted: int = 0


class SessionActionRequest(BaseModel):
    action: str = Field(..., description="park | snooze | archive | resume")


class SessionActionResponse(BaseModel):
    session_id: str
    user_state: str


class SessionLoopModeRequest(BaseModel):
    loop_mode: SessionLoopMode = Field(..., description="assist | autopilot")


class SessionLoopModeResponse(BaseModel):
    session_id: str
    loop_mode: SessionLoopMode


class SessionNotificationWatchRequest(BaseModel):
    notification_muted: bool = Field(..., description="When true, suppress Tier 1/2 pushes for this session.")


class SessionNotificationWatchResponse(BaseModel):
    session_id: str
    notification_muted: bool


class BackfillSummariesResponse(BaseModel):
    """Response for summary backfill endpoint."""

    status: str = Field(..., description="'started', 'already_running', or 'nothing_to_do'")
    total: int = Field(0, description="Total sessions to process")
    message: str = Field("", description="Human-readable status message")


class BackfillProgressResponse(BaseModel):
    """Response for backfill progress check."""

    running: bool
    backfilled: int = 0
    skipped: int = 0
    errors: int = 0
    remaining: int = 0
    total: int = 0


class BackfillEmbeddingsResponse(BaseModel):
    """Response for embedding backfill endpoint."""

    status: str = Field(..., description="'started', 'already_running', or 'nothing_to_do'")
    total: int = Field(0, description="Total sessions to process")
    message: str = Field("", description="Human-readable status message")


class BackfillEmbeddingsProgressResponse(BaseModel):
    """Response for embedding backfill progress check."""

    running: bool
    embedded: int = 0
    skipped: int = 0
    errors: int = 0
    remaining: int = 0
    total: int = 0


class MediaBackfillInlineDataUrlsResponse(BaseModel):
    """Response for guarded legacy inline media backfill."""

    dry_run: bool
    scanned_source_lines: int = 0
    candidate_refs: int = 0
    decoded_bytes: int = 0
    stored_objects: int = 0
    refs_upserted: int = 0
    skipped_existing_refs: int = 0
    skipped_budget: int = 0
    skipped_disk_floor: int = 0
    rejected: int = 0
    last_source_line_id: Optional[int] = None
    message: str = ""


class CursorRoleBackfillResponse(BaseModel):
    """Response for one batch of Cursor user-event role re-classification."""

    dry_run: bool
    scanned: int = 0
    re_roleed: int = 0
    unwrapped: int = 0
    last_id: Optional[int] = None
    message: str = ""


class IngestHealthResponse(UTCBaseModel):
    status: str  # "ok" | "stale" | "unknown"
    last_session_at: Optional[datetime] = None
    gap_hours: Optional[float] = None
    threshold_hours: float
    session_count: int
    media_repair_refs: int = 0
    media_repair_bytes: int = 0


class UsageStatsByProvider(BaseModel):
    provider: str
    sessions: int
    messages: int


class UsageStatsResponse(BaseModel):
    total_sessions: int
    total_messages: int
    date_range: Dict[str, str]
    by_provider: List[UsageStatsByProvider]


class SemanticSearchResponse(BaseModel):
    """Response for semantic search."""

    sessions: List[SessionResponse]
    total: int
    has_real_sessions: bool = True


class RecallMatch(BaseModel):
    """A single recall match with context."""

    session_id: str
    chunk_index: int
    score: float
    chunk_id: Optional[int] = None
    chunk_uid: Optional[str] = None
    parent_chunk_id: Optional[int] = None
    context_chunk_id: Optional[int] = None
    chunk_kind: Optional[str] = None
    context_text: Optional[str] = None
    intent: Optional[str] = None
    evidence: Optional[str] = None
    structured_hits: List[str] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    event_index_start: Optional[int] = None
    event_index_end: Optional[int] = None
    total_events: int = 0
    context: List[Dict[str, Any]] = Field(default_factory=list)
    match_event_id: Optional[int] = None


class RecallResponse(BaseModel):
    """Response for recall endpoint."""

    matches: List[RecallMatch]
    total: int


class CleanupRequest(BaseModel):
    """Request for test cleanup."""

    project_patterns: List[str] = Field(
        ...,
        description="LIKE patterns to match (e.g., 'test-%', 'ratelimit-%')",
    )


class CleanupResponse(BaseModel):
    """Response for test cleanup."""

    deleted: int


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def normalize_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_thread_meta(store: AgentsStore, session: AgentSession, thread_cache: Dict[str, tuple[str, int]]) -> tuple[str, int]:
    root_id = str(session.id)
    cached = thread_cache.get(root_id)
    if cached is not None:
        return cached

    batched = store.batch_thread_meta([session])
    meta = batched.get(root_id, (str(session.id), 1))
    thread_cache[root_id] = meta
    return meta


def build_session_response(
    store: AgentsStore,
    session: AgentSession,
    *,
    thread_cache: Dict[str, tuple[str, int]] | None = None,
    last_activity_at: datetime | None = None,
    runtime_overlay: SessionRuntimeView | None = None,
    first_user_message: str | None = None,
    match_event_id: int | None = None,
    match_snippet: str | None = None,
    match_role: str | None = None,
    match_score: float | None = None,
    binding_overlay=None,
    transcript_preview: TranscriptPreview | None = None,
    owner_id: int | None = None,
    summary_status: str | None = None,
    control_overlay=None,
    kernel_capabilities=None,
    has_pending_response_turn: bool = False,
    pause_request: dict[str, Any] | None = None,
    archive_state: str = "current",
    launch_attempt: SessionLaunchAttempt | None | object = _LAUNCH_ATTEMPT_MISSING,
    launch_readiness: LiveLaunchReadinessView | None = None,
    sharer: SessionSharerResponse | None = None,
) -> SessionResponse:
    cache = thread_cache if thread_cache is not None else {}
    thread_head_session_id, thread_continuation_count = get_thread_meta(store, session, cache)
    kernel_projection = project_session_kernel_fields(store.db, session, capabilities=kernel_capabilities)
    resolved_kernel_capabilities = kernel_projection.capabilities
    capability_flags = resolved_kernel_capabilities
    is_engine_control_online = engine_control_online(session, owner_id)
    current_now = datetime.now(timezone.utc)
    display_last_activity_at = last_activity_at or session.ended_at or session.started_at
    display_runtime_overlay = runtime_overlay or build_fallback_runtime_view(
        session=session,
        last_activity_at=display_last_activity_at,
        now=current_now,
    )
    include_runtime = should_include_runtime_view(session=session, runtime_view=runtime_overlay)
    is_engine_session_attached = is_engine_control_online and engine_session_control_attached(
        session,
        runtime_overlay,
        control_overlay=control_overlay,
        now=current_now,
    )
    binding_host_state = None
    binding_terminal_reason = None
    if binding_overlay is not None:
        binding_host_state = binding_overlay.host_state
        binding_terminal_reason = binding_overlay.terminal_reason
    if is_engine_session_attached:
        binding_host_state = "online"
        control_overlay = engine_channel_control_overlay(session, seen_at=current_now)
    elif (
        capability_flags.live_control_available or capability_flags.host_reattach_available
    ) and kernel_projection.control.source_runner_id is not None:
        binding_host_state = managed_runner_host_state(store.db, session) or binding_host_state
        if binding_host_state == "online" and control_overlay is None:
            control_overlay = live_transport_control_overlay(
                session,
                source=CONTROL_SOURCE_RUNNER_CONNECTION,
                seen_at=current_now,
            )
    elif (
        capability_flags.live_control_available
        and not binding_host_state
        and capability_flags.control_plane in _TRUSTED_NON_RUNNER_CONTROL_PLANES
    ):
        # Kernel attests control is live on a direct machine control plane
        # (engine channel / direct process). There is no Runner row to consult,
        # so trust the kernel rather than letting an absent binding signal
        # demote the session to "unknown".
        binding_host_state = "online"
    transcript_preview_response = build_session_transcript_preview_response(
        transcript_preview,
        last_activity_at=last_activity_at,
        now=current_now,
    )
    has_visible_transcript_preview = bool(
        transcript_preview_response is not None and transcript_preview_response.text.strip() and not transcript_preview_response.is_stale
    )
    runtime_facts = derive_session_liveness_facts(
        runtime_overlay=runtime_overlay,
        capability_flags=capability_flags,
        last_activity_at=last_activity_at,
        binding_overlay=binding_overlay,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
        control_overlay=control_overlay,
    )
    # The kernel ``capability_flags`` is already the truth.
    effective_capability_flags = capability_flags
    if launch_readiness is not None:
        effective_launch_attempt = None
    elif launch_attempt is _LAUNCH_ATTEMPT_MISSING:
        effective_launch_attempt = _latest_launch_attempt(store.db, session.id)
    else:
        effective_launch_attempt = launch_attempt
    archive_launch_lifecycle = None if launch_readiness is not None else project_remote_launch_lifecycle(effective_launch_attempt)
    launch_state = launch_readiness.launch_state if launch_readiness is not None else None
    execution_lifetime = launch_readiness.execution_lifetime if launch_readiness is not None else None
    launch_error_code = launch_readiness.launch_error_code if launch_readiness is not None else None
    launch_error_message = launch_readiness.launch_error_message if launch_readiness is not None else None
    if archive_launch_lifecycle is not None:
        launch_state = archive_launch_lifecycle.state
        execution_lifetime = archive_launch_lifecycle.execution_lifetime
        launch_error_code = archive_launch_lifecycle.error_code
        launch_error_message = archive_launch_lifecycle.error_message
    continue_target = _native_continue_target(store.db, session)
    continue_targets = [continue_target] if continue_target is not None else []
    lineage_projection = kernel_projection.lineage
    title_state, title_source = resolve_title_provenance(
        anchor_title=session.anchor_title,
        first_user_message=first_user_message,
        user_messages=session.user_messages,
        title_retry_at=getattr(session, "title_retry_at", None),
    )
    from zerg.services.session_preferences import load_session_preferences

    preferences = load_session_preferences(session.id, standalone_session=session)
    session_state = build_session_state_facts(
        session=session,
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        liveness=runtime_facts,
        pause_request=pause_request,
        launch_state=launch_state,
        launch_error_code=launch_error_code,
        launch_error_message=launch_error_message,
        execution_lifetime=execution_lifetime,
        last_activity_at=display_last_activity_at,
        has_visible_transcript_preview=has_visible_transcript_preview,
        has_pending_response_turn=has_pending_response_turn,
        user_messages=session.user_messages or 0,
        assistant_messages=session.assistant_messages or 0,
        archive_state=archive_state,
        now=current_now,
    )
    runtime_display = build_compat_runtime_display_response(
        session_state=session_state,
        pause_request=pause_request,
        now=current_now,
    )
    response_capabilities = project_compat_capabilities_from_state(
        build_session_capabilities_response(
            session=session,
            capability_flags=capability_flags,
            runtime_display=runtime_display,
            runtime_facts=runtime_facts,
            kernel_capabilities=resolved_kernel_capabilities,
            can_continue=continue_target is not None,
            continue_targets=continue_targets,
            launch_lifecycle=archive_launch_lifecycle,
        ),
        session_state,
    )
    return SessionResponse(
        id=str(session.id),
        provider=session.provider,
        project=session.project,
        device_id=session.device_id,
        environment=session.environment,
        cwd=session.cwd,
        git_repo=session.git_repo,
        git_branch=session.git_branch,
        started_at=session.started_at,
        ended_at=session.ended_at,
        user_messages=session.user_messages or 0,
        assistant_messages=session.assistant_messages or 0,
        tool_calls=session.tool_calls or 0,
        last_activity_at=last_activity_at,
        timeline_anchor_at=(runtime_overlay.timeline_anchor_at if runtime_overlay is not None else last_activity_at),
        runtime_phase=(runtime_overlay.runtime_phase if runtime_overlay is not None else None),
        phase_started_at=(runtime_overlay.phase_started_at if runtime_overlay is not None else None),
        last_progress_at=(runtime_overlay.last_progress_at if runtime_overlay is not None else None),
        runtime_source=(runtime_overlay.runtime_source if runtime_overlay is not None else None),
        terminal_state=(runtime_overlay.terminal_state if runtime_overlay is not None else None),
        runtime_version=(runtime_overlay.runtime_version if runtime_overlay is not None else None),
        status=(runtime_overlay.status if include_runtime else None),
        presence_state=(runtime_overlay.presence_state if include_runtime else None),
        presence_tool=(runtime_overlay.presence_tool if include_runtime else None),
        presence_updated_at=(runtime_overlay.presence_updated_at if include_runtime else None),
        last_live_at=(runtime_overlay.last_live_at if include_runtime else None),
        display_phase=(runtime_overlay.display_phase if include_runtime else None),
        active_tool=(runtime_overlay.active_tool if include_runtime else None),
        confidence=(runtime_overlay.confidence if include_runtime else None),
        summary=session.summary,
        summary_title=session.summary_title,
        anchor_title=session.anchor_title,
        timeline_title=resolve_timeline_title(
            anchor_title=session.anchor_title,
            summary_title=session.summary_title,
            summary_status=summary_status,
            first_user_message=first_user_message,
            project=session.project,
            git_branch=session.git_branch,
        ),
        title_state=title_state,
        title_source=title_source,
        summary_status=summary_status,
        first_user_message=first_user_message,
        match_event_id=match_event_id,
        match_snippet=match_snippet,
        match_role=match_role,
        match_score=match_score,
        thread_root_session_id=lineage_projection.thread_root_session_id,
        thread_head_session_id=thread_head_session_id,
        thread_continuation_count=thread_continuation_count,
        continued_from_session_id=lineage_projection.continued_from_session_id,
        continuation_kind=lineage_projection.continuation_kind,
        origin_label=lineage_projection.origin_label,
        home_label=capability_flags.home_label,
        branched_from_event_id=lineage_projection.branched_from_event_id,
        is_writable_head=lineage_projection.is_writable_head,
        is_sidechain=lineage_projection.is_sidechain,
        control=build_session_control_response(
            session,
            db=store.db,
            capability_flags=effective_capability_flags,
            control_projection=kernel_projection.control,
        ),
        capabilities=response_capabilities,
        session_state=session_state,
        runtime_display=runtime_display,
        transcript_preview=transcript_preview_response,
        timeline_card=build_session_timeline_card_response(
            runtime_view=display_runtime_overlay,
            runtime_display=runtime_display,
            session_state=session_state,
        ),
        loop_mode=_coerce_session_loop_mode(preferences.loop_mode),
        user_state=preferences.user_state,
        launch_state=launch_state,
        execution_lifetime=execution_lifetime,
        launch_error_code=launch_error_code,
        launch_error_message=launch_error_message,
        sharer=sharer,
    )


def build_session_transcript_preview_response(
    preview: TranscriptPreview | None,
    *,
    last_activity_at: datetime | None = None,
    now: datetime | None = None,
) -> SessionTranscriptPreviewResponse | None:
    if preview is None:
        return None
    preview_at = normalize_utc(preview.timestamp)
    now_utc = normalize_utc(now) or datetime.now(timezone.utc)
    if preview.provisional_complete:
        max_age = PROVISIONAL_TRANSCRIPT_COMPLETE_FRESHNESS
    else:
        max_age = PROVISIONAL_TRANSCRIPT_PARTIAL_FRESHNESS
    durable_activity_at = normalize_utc(last_activity_at)
    is_stale = False
    stale_reason = None
    if preview_at is None:
        is_stale = True
        stale_reason = "missing_preview_timestamp"
    elif durable_activity_at is not None and durable_activity_at > preview_at:
        is_stale = True
        stale_reason = "superseded_by_durable"
    elif preview.event_origin == "live_provisional" and now_utc - preview_at > max_age:
        is_stale = True
        stale_reason = "freshness_window_expired"
    return SessionTranscriptPreviewResponse(
        event_id=preview.event_id,
        text=preview.text,
        event_origin=preview.event_origin,
        timestamp=preview.timestamp,
        is_provisional=preview.event_origin == "live_provisional",
        is_complete=preview.provisional_complete,
        content_cursor=preview.provisional_cursor,
        is_stale=is_stale,
        stale_reason=stale_reason,
    )


def _latest_launch_attempt(db, session_id) -> SessionLaunchAttempt | None:
    return (
        db.query(SessionLaunchAttempt)
        .filter(SessionLaunchAttempt.session_id == session_id)
        .order_by(SessionLaunchAttempt.created_at.desc(), SessionLaunchAttempt.id.desc())
        .first()
    )


def latest_launch_attempts(db, session_ids) -> dict:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionLaunchAttempt)
        .filter(SessionLaunchAttempt.session_id.in_(session_ids))
        .order_by(
            SessionLaunchAttempt.session_id,
            SessionLaunchAttempt.created_at.desc(),
            SessionLaunchAttempt.id.desc(),
        )
        .all()
    )
    result = {}
    for attempt in rows:
        result.setdefault(attempt.session_id, attempt)
    return result


def latest_live_launch_readiness(session_ids, *, now: datetime | None = None) -> dict[UUID, LiveLaunchReadinessView]:
    from zerg import database as database_module

    if not session_ids or not database_module.live_store_configured():
        return {}
    if database_module.live_catalog_enabled():
        from zerg.services.catalog_facts import hydrate_catalog_row
        from zerg.services.catalog_facts import session_facts_map

        cutoff = normalize_utc(now) or datetime.now(timezone.utc)
        facts_by_session = session_facts_map([str(session_id) for session_id in session_ids])
        result: dict[UUID, LiveLaunchReadinessView] = {}
        for session_id, facts in facts_by_session.items():
            row = hydrate_catalog_row(LiveLaunchReadiness, facts.get("readiness"))
            if row is None:
                continue
            expires_at = normalize_utc(row.expires_at)
            if expires_at is not None and expires_at <= cutoff:
                continue
            result[UUID(session_id)] = project_live_launch_readiness(row)
        return result
    live_session_factory = database_module.get_live_session_factory()
    if live_session_factory is None:
        return {}
    with live_session_factory() as live_db:
        return query_live_launch_readiness_map(live_db, session_ids, now=now)


def build_live_launch_placeholder_response(
    launch_readiness: LiveLaunchReadinessView,
    *,
    now: datetime | None = None,
    transcript_preview: TranscriptPreview | None = None,
    sharer: SessionSharerResponse | None = None,
) -> SessionResponse:
    """Build a read-only first-paint session response before archive convergence."""

    current_now = normalize_utc(now) or datetime.now(timezone.utc)
    started_at = normalize_utc(launch_readiness.created_at) or normalize_utc(launch_readiness.updated_at) or current_now
    provider = (launch_readiness.provider or "unknown").strip() or "unknown"
    provider_label = {
        "claude": "Claude",
        "codex": "Codex",
        "opencode": "OpenCode",
    }.get(provider.lower(), provider[:1].upper() + provider[1:] if provider else "session")
    machine_label = (launch_readiness.device_id or "").strip() or "the machine"
    project = (launch_readiness.project or "").strip() or None
    title = project or f"{provider} launch"
    session_id = str(launch_readiness.session_id)
    capability_label = "Launching"
    capability_detail = f"Setting up {provider_label} on {machine_label}."
    composer_disabled_reason = f"Setting up {provider_label}."
    user_state = "active"
    if launch_readiness.launch_state == "live":
        capability_detail = f"Connecting to {provider_label} on {machine_label}."
        composer_disabled_reason = f"Connecting to {provider_label}."
    elif launch_readiness.launch_state in {"launch_failed", "launch_orphaned"}:
        capability_label = "Launch failed"
        capability_detail = launch_readiness.launch_error_message or "The session did not start."
        composer_disabled_reason = "Launch failed."
        user_state = "archived"
    capabilities = SessionCapabilitiesResponse(
        live_control_available=False,
        host_reattach_available=False,
        reply_to_live_session_available=False,
        can_queue_next_input=False,
        can_steer_active_turn=False,
        display_label=capability_label,
        display_detail=capability_detail,
        display_tone="accent",
        input_mode="read_only",
        default_input_intent="none",
        composer_enabled=False,
        composer_disabled_reason=composer_disabled_reason,
        send_disabled_reason="read_only",
        control_label="search-only",
        observe_only=True,
        search_only=False,
        staleness_reason="launch_pending",
        can_send_input=False,
        can_interrupt=False,
        can_terminate=False,
        can_tail_output=False,
        can_resume=False,
        attach_images=False,
        can_continue=False,
        continue_targets=[],
    )
    transcript_preview_response = build_session_transcript_preview_response(
        transcript_preview,
        last_activity_at=started_at,
        now=current_now,
    )
    placeholder_kernel_capabilities = KernelSessionCapabilities(
        session_id=session_id,
        thread_id=None,
        run_id=None,
        connection_id=None,
        control_plane=None,
        connection_state=None,
        control_label="imported",
        live_control_available=False,
        host_reattach_available=False,
        observe_only=False,
        search_only=False,
        can_send_input=False,
        can_interrupt=False,
        can_terminate=False,
        can_tail_output=False,
        can_resume=False,
        staleness_reason="launch_pending",
    )
    placeholder_liveness = build_session_liveness_facts(
        runtime_view=None,
        capabilities=placeholder_kernel_capabilities,
        last_activity_at=started_at,
        now=current_now,
    )
    session_state = build_session_state_facts(
        session=SimpleNamespace(
            started_at=started_at,
            ended_at=None,
            launch_surface="web",
            transcript_revision=0,
        ),
        runtime_view=None,
        capabilities=placeholder_kernel_capabilities,
        liveness=placeholder_liveness,
        launch_state=launch_readiness.launch_state,
        launch_error_code=launch_readiness.launch_error_code,
        launch_error_message=launch_readiness.launch_error_message,
        execution_lifetime=launch_readiness.execution_lifetime,
        last_activity_at=started_at,
        archive_state="pending",
        now=current_now,
    )
    runtime_display = build_compat_runtime_display_response(
        session_state=session_state,
        pause_request=None,
        now=current_now,
    )
    capabilities = project_compat_capabilities_from_state(capabilities, session_state)
    return SessionResponse(
        id=session_id,
        provider=provider,
        project=project,
        device_id=launch_readiness.device_id,
        environment="development",
        cwd=None,
        git_repo=None,
        git_branch=None,
        started_at=started_at,
        ended_at=None,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        last_activity_at=started_at,
        timeline_anchor_at=normalize_utc(launch_readiness.updated_at) or started_at,
        runtime_phase=runtime_display.state.value if runtime_display.state is not None else None,
        phase_started_at=started_at,
        last_progress_at=normalize_utc(launch_readiness.updated_at) or started_at,
        runtime_source="live_launch_readiness",
        terminal_state=None,
        runtime_version=None,
        status=("working" if runtime_display.is_executing else "idle" if runtime_display.is_idle else None),
        presence_state=runtime_display.state.value if runtime_display.state is not None else None,
        presence_tool=None,
        presence_updated_at=normalize_utc(launch_readiness.updated_at) or started_at,
        last_live_at=normalize_utc(launch_readiness.updated_at) or started_at,
        display_phase=runtime_display.phase_label,
        active_tool=runtime_display.compact_tool_label,
        confidence="live" if runtime_display.activity_recency == ActivityRecency.LIVE else None,
        summary=None,
        summary_title=None,
        anchor_title=None,
        timeline_title=title,
        summary_status="unavailable",
        first_user_message=None,
        match_event_id=None,
        match_snippet=None,
        match_role=None,
        match_score=None,
        thread_root_session_id=session_id,
        thread_head_session_id=session_id,
        thread_continuation_count=0,
        continued_from_session_id=None,
        continuation_kind=None,
        origin_label="Longhouse launch",
        home_label=launch_readiness.machine_id or launch_readiness.device_id,
        branched_from_event_id=None,
        is_writable_head=True,
        is_sidechain=False,
        control=SessionControlResponse(),
        capabilities=capabilities,
        session_state=session_state,
        runtime_display=runtime_display,
        transcript_preview=transcript_preview_response,
        timeline_card=build_session_timeline_card_response(
            runtime_view=None,
            runtime_display=runtime_display,
            session_state=session_state,
        ),
        loop_mode=SessionLoopMode.ASSIST,
        user_state=user_state,
        launch_state=launch_readiness.launch_state,
        execution_lifetime=launch_readiness.execution_lifetime,
        launch_error_code=launch_readiness.launch_error_code,
        launch_error_message=launch_readiness.launch_error_message,
        sharer=sharer,
    )


def build_active_session_response(
    store: AgentsStore,
    session: AgentSession,
    *,
    last_activity_at: datetime,
    runtime_overlay: SessionRuntimeView,
    last_user_message: str | None,
    last_assistant_message: str | None,
    attention: str,
    now: datetime,
    binding_overlay=None,
    control_overlay=None,
    kernel_capabilities=None,
    pause_request: dict[str, Any] | None = None,
) -> ActiveSessionResponse:
    kernel_projection = project_session_kernel_fields(store.db, session, capabilities=kernel_capabilities)
    resolved_kernel_capabilities = kernel_projection.capabilities
    capability_flags = resolved_kernel_capabilities
    _started = (
        session.started_at.replace(tzinfo=timezone.utc) if session.started_at and session.started_at.tzinfo is None else session.started_at
    )
    _ended = session.ended_at.replace(tzinfo=timezone.utc) if session.ended_at and session.ended_at.tzinfo is None else session.ended_at
    end_time = _ended or now
    duration_minutes = int((end_time - _started).total_seconds() / 60) if _started else 0
    message_count = (session.user_messages or 0) + (session.assistant_messages or 0)
    binding_host_state = binding_overlay.host_state if binding_overlay is not None else None
    binding_terminal_reason = binding_overlay.terminal_reason if binding_overlay is not None else None
    current_now = datetime.now(timezone.utc)
    if capability_flags.live_control_available or capability_flags.host_reattach_available:
        binding_host_state = managed_runner_host_state(store.db, session) or binding_host_state
        if binding_host_state == "online" and control_overlay is None:
            control_overlay = live_transport_control_overlay(
                session,
                source=CONTROL_SOURCE_RUNNER_CONNECTION,
                seen_at=current_now,
            )
    runtime_facts = derive_session_liveness_facts(
        runtime_overlay=runtime_overlay,
        capability_flags=capability_flags,
        last_activity_at=last_activity_at,
        binding_overlay=binding_overlay,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
        control_overlay=control_overlay,
        now=now,
    )
    # The kernel ``capability_flags`` is already the truth.
    effective_capability_flags = capability_flags
    continue_target = _native_continue_target(store.db, session)
    continue_targets = [continue_target] if continue_target is not None else []

    from zerg.services.session_preferences import load_session_preferences

    preferences = load_session_preferences(session.id, standalone_session=session)
    session_state = build_session_state_facts(
        session=session,
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        liveness=runtime_facts,
        pause_request=pause_request,
        last_activity_at=last_activity_at,
        user_messages=int(session.user_messages or 0),
        assistant_messages=int(session.assistant_messages or 0),
        now=now,
    )
    runtime_display = build_compat_runtime_display_response(
        session_state=session_state,
        pause_request=pause_request,
        now=now,
    )
    response_capabilities = project_compat_capabilities_from_state(
        build_session_capabilities_response(
            session=session,
            capability_flags=capability_flags,
            runtime_display=runtime_display,
            runtime_facts=runtime_facts,
            kernel_capabilities=resolved_kernel_capabilities,
            can_continue=continue_target is not None,
            continue_targets=continue_targets,
        ),
        session_state,
    )
    return ActiveSessionResponse(
        id=str(session.id),
        project=session.project,
        provider=session.provider,
        cwd=session.cwd,
        git_branch=session.git_branch,
        started_at=session.started_at,
        ended_at=session.ended_at,
        last_activity_at=last_activity_at,
        timeline_anchor_at=runtime_overlay.timeline_anchor_at,
        runtime_phase=runtime_overlay.runtime_phase,
        phase_started_at=runtime_overlay.phase_started_at,
        last_progress_at=runtime_overlay.last_progress_at,
        runtime_source=runtime_overlay.runtime_source,
        terminal_state=runtime_overlay.terminal_state,
        runtime_version=runtime_overlay.runtime_version,
        status=runtime_overlay.status,
        attention=attention,
        duration_minutes=duration_minutes,
        last_user_message=last_user_message,
        last_assistant_message=last_assistant_message,
        message_count=message_count,
        tool_calls=session.tool_calls or 0,
        presence_state=runtime_overlay.presence_state,
        presence_tool=runtime_overlay.presence_tool,
        presence_updated_at=runtime_overlay.presence_updated_at,
        last_live_at=runtime_overlay.last_live_at,
        display_phase=runtime_overlay.display_phase,
        active_tool=runtime_overlay.active_tool,
        confidence=runtime_overlay.confidence,
        user_state=preferences.user_state,
        home_label=capability_flags.home_label,
        control=build_session_control_response(
            session,
            db=store.db,
            capability_flags=effective_capability_flags,
            control_projection=kernel_projection.control,
        ),
        capabilities=response_capabilities,
        session_state=session_state,
        runtime_display=runtime_display,
        loop_mode=_coerce_session_loop_mode(preferences.loop_mode),
    )


def build_event_response(
    store: AgentsStore,
    event: AgentEvent,
    *,
    boundary: int | None,
    head_branch_id: int | None,
    input_origin_map: dict[int, InputOriginResponse | None] | None = None,
    tool_call_state_map: dict[int, ToolCallState] | None = None,
    media_ref_map: dict[int, list[EventMediaRefResponse]] | None = None,
    mobile_payload: bool = False,
) -> EventResponse:
    content_text = event.content_text
    raw_content_text = None
    if event.role == "user" and content_text is not None:
        display_text = strip_claude_channel_wrapper(content_text)
        if display_text != content_text:
            content_text = display_text
            raw_content_text = event.content_text
    is_head_branch = head_branch_id is None or event.branch_id in {None, head_branch_id}
    tool_output_text = event.tool_output_text
    tool_output_truncated = False
    tool_output_original_chars: int | None = None
    if mobile_payload and tool_output_text is not None and len(tool_output_text) > MOBILE_TOOL_OUTPUT_MAX_CHARS:
        tool_output_original_chars = len(tool_output_text)
        tool_output_text = tool_output_text[:MOBILE_TOOL_OUTPUT_MAX_CHARS]
        tool_output_truncated = True

    tool_call_state = tool_call_state_map.get(int(event.id)) if tool_call_state_map is not None else None

    return EventResponse(
        id=event.id,
        role=event.role,
        content_text=content_text,
        raw_content_text=raw_content_text,
        input_origin=(_event_input_origin_response(store, event, input_origin_map=input_origin_map) if is_head_branch else None),
        tool_name=event.tool_name,
        tool_input_json=event.tool_input_json,
        tool_output_text=tool_output_text,
        tool_output_truncated=tool_output_truncated,
        tool_output_original_chars=tool_output_original_chars,
        tool_call_id=event.tool_call_id,
        timestamp=event.timestamp,
        in_active_context=store.is_event_in_active_context(event, boundary) if boundary is not None else True,
        branch_id=event.branch_id,
        is_head_branch=is_head_branch,
        event_origin=event.event_origin or "durable",
        provisional_state=event.provisional_state,
        provisional_cursor=event.provisional_cursor,
        provisional_complete=bool(event.provisional_complete),
        reconciled_event_id=event.reconciled_event_id,
        tool_call_state=tool_call_state,
        media_refs=media_ref_map.get(int(event.id), []) if media_ref_map is not None else [],
    )


def build_event_media_ref_map(db, events: list[AgentEvent]) -> dict[int, list[EventMediaRefResponse]]:
    """Return media refs keyed by AgentEvent id for a projected event page."""

    if not events:
        return {}

    event_by_id = {int(event.id): event for event in events if getattr(event, "id", None) is not None}
    event_ids = set(event_by_id)
    # Map each source coordinate to the events projected from it, remembering each
    # event's semantic identity (event_hash). One provider source line can project
    # into several events at the same byte offset in two distinct ways:
    #   1. Same logical event repeated -- re-ingestion duplicates on one branch, or
    #      branch-copies of an identical line across branches (same event_hash).
    #   2. Genuinely different events from one line -- e.g. assistant text plus a
    #      tool-use call, or several tool-result events (different event_hash).
    # A coordinate-only media ref (event_id is NULL, e.g. legacy backfill) may bind
    # to case 1 because every candidate is the same image-bearing line, but must NOT
    # guess in case 2 where the offset is shared by unrelated events.
    events_by_source: dict[tuple[Any, str, int], list[tuple[int, Optional[str]]]] = {}
    for event_id, event in event_by_id.items():
        if event.source_path and event.source_offset is not None:
            key = (event.session_id, event.source_path, int(event.source_offset))
            events_by_source.setdefault(key, []).append((event_id, event.event_hash))
    session_ids = {event.session_id for event in events}
    offsets = {int(event.source_offset) for event in events if event.source_offset is not None}
    source_paths = {event.source_path for event in events if event.source_path}
    if not event_ids and not (session_ids and offsets and source_paths):
        return {}

    filters = []
    if event_ids:
        filters.append(SessionMediaRef.event_id.in_(event_ids))
    if session_ids and offsets and source_paths:
        filters.append(
            and_(
                SessionMediaRef.session_id.in_(session_ids),
                SessionMediaRef.source_offset.in_(offsets),
                SessionMediaRef.source_path.in_(source_paths),
            )
        )
    rows = (
        db.query(SessionMediaRef, MediaObject)
        .outerjoin(MediaObject, MediaObject.sha256 == SessionMediaRef.media_sha256)
        .filter(or_(*filters))
        .order_by(SessionMediaRef.id.asc())
        .all()
    )

    result: dict[int, list[EventMediaRefResponse]] = {}
    seen: set[tuple[int, str, int]] = set()
    for ref, media in rows:
        matched_ids: list[int] = []
        if ref.event_id is not None and int(ref.event_id) in event_by_id:
            matched_ids.append(int(ref.event_id))
        elif ref.source_path and ref.source_offset is not None:
            candidates = events_by_source.get((ref.session_id, ref.source_path, int(ref.source_offset)), [])
            # Bind when every candidate event shares one semantic identity (the same
            # image-bearing line, duplicated or branch-copied). Refuse to guess when
            # the coordinate is shared by distinct events. A lone candidate with a
            # NULL event_hash still binds -- it is unambiguous by count.
            distinct_hashes = {event_hash for _eid, event_hash in candidates}
            if len(candidates) == 1 or len(distinct_hashes) == 1:
                matched_ids.extend(event_id for event_id, _hash in candidates)

        for event_id in sorted(set(matched_ids)):
            key = (event_id, ref.media_sha256, int(ref.id))
            if key in seen:
                continue
            seen.add(key)
            result.setdefault(event_id, []).append(_event_media_ref_response(ref, media))
    return result


def _event_media_ref_response(ref: SessionMediaRef, media: MediaObject | None) -> EventMediaRefResponse:
    return EventMediaRefResponse(
        sha256=ref.media_sha256,
        media_state=ref.media_state,
        mime_type=media.mime_type if media is not None else None,
        byte_size=int(media.byte_size) if media is not None and media.byte_size is not None else None,
        blob_url=f"/api/media/{ref.media_sha256}/blob",
        thumb_url=f"/api/media/{ref.media_sha256}/thumb" if media is not None and media.thumbnail_sha256 else None,
        source_path=ref.source_path,
        source_offset=int(ref.source_offset) if ref.source_offset is not None else None,
        json_pointer=ref.json_pointer,
        original_kind=ref.original_kind,
    )


def is_session_closed(session: AgentSession) -> bool:
    """Whether the durable session itself was explicitly closed.

    ``ended_at`` is historical run/transcript evidence and is intentionally not
    disposition truth: managed sessions can end a process and later resume the
    same session.  Only an explicit session-level terminal state closes it.
    """
    if normalize_utc(getattr(session, "closed_at", None)) is not None:
        return True
    terminal_state = (getattr(session, "terminal_state", "") or "").strip().lower()
    return terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES


def build_tool_call_state_map(
    events: list[AgentEvent],
    *,
    session_closed: bool,
    now: datetime | None = None,
) -> dict[int, ToolCallState]:
    """Project per-event tool-call lifecycle from a list of events.

    Returns a map of assistant-tool-call event_id → ToolCallState. Result events
    and non-tool events are not included; clients consume the call event's
    tool_call_state. Pairing mirrors the iOS/web rule: by tool_call_id when
    present, FIFO otherwise. A call without a paired result is "dropped" if
    the session is closed (lifecycle terminal or ended_at stamped) or the call
    is older than ``DROPPED_TOOL_AGE``; otherwise "running". A paired call is
    "completed". The events list must include the assistant tool-call rows being
    rendered plus any matching result rows needed to classify those calls; it
    should not be the full session ledger on first-paint paths.
    """
    now_utc = normalize_utc(now) or datetime.now(timezone.utc)
    threshold = now_utc - DROPPED_TOOL_AGE

    by_call_id: dict[str, AgentEvent] = {}
    fifo_pending: list[AgentEvent] = []
    paired_call_ids: set[int] = set()

    for event in events:
        role = (event.role or "").strip().lower()
        if role == "assistant" and event.tool_name:
            if event.tool_call_id:
                by_call_id[event.tool_call_id] = event
            else:
                fifo_pending.append(event)
        elif role == "tool":
            matched: AgentEvent | None = None
            if event.tool_call_id:
                matched = by_call_id.pop(event.tool_call_id, None)
            elif fifo_pending:
                matched = fifo_pending.pop(0)
            if matched is not None:
                paired_call_ids.add(int(matched.id))

    result: dict[int, ToolCallState] = {}
    for event in events:
        role = (event.role or "").strip().lower()
        if role != "assistant" or not event.tool_name:
            continue
        if int(event.id) in paired_call_ids:
            result[int(event.id)] = ToolCallState.COMPLETED
            continue
        if session_closed:
            result[int(event.id)] = ToolCallState.DROPPED
            continue
        call_at = normalize_utc(event.timestamp)
        if call_at is not None and call_at < threshold:
            result[int(event.id)] = ToolCallState.DROPPED
        else:
            result[int(event.id)] = ToolCallState.RUNNING
    return result


def _event_input_origin_response(
    store: AgentsStore,
    event: AgentEvent,
    *,
    input_origin_map: dict[int, InputOriginResponse | None] | None,
) -> InputOriginResponse | None:
    if input_origin_map is not None:
        return input_origin_map.get(int(event.id))
    return build_event_input_origin_map(store, [event]).get(int(event.id))


def build_event_input_origin_map(store: AgentsStore, events: list[AgentEvent]) -> dict[int, InputOriginResponse | None]:
    user_events = {int(event.id): event for event in events if str(getattr(event, "role", "") or "").strip().lower() == "user"}
    origins: dict[int, InputOriginResponse | None] = {event_id: InputOriginResponse(authored_via="terminal") for event_id in user_events}
    if not user_events:
        return origins

    session_ids = {event.session_id for event in user_events.values()}
    turns = (
        store.db.query(SessionTurn, SessionInput)
        .outerjoin(
            SessionInput,
            and_(
                SessionInput.id == SessionTurn.session_input_id,
                SessionInput.session_id == SessionTurn.session_id,
            ),
        )
        .filter(
            SessionTurn.user_event_id.in_(list(user_events)),
            SessionTurn.session_id.in_({event.session_id for event in user_events.values()}),
        )
        .order_by(SessionTurn.id.asc())
        .all()
    )
    seen_event_ids: set[int] = set()
    for turn, session_input in turns:
        user_event_id = int(getattr(turn, "user_event_id", 0) or 0)
        if user_event_id in seen_event_ids or user_event_id not in user_events:
            continue
        seen_event_ids.add(user_event_id)
        if getattr(turn, "session_input_id", None) is None:
            continue
        if session_input is None:
            logger.warning(
                "Session turn %s links missing SessionInput %s for user event %s",
                getattr(turn, "id", None),
                getattr(turn, "session_input_id", None),
                user_event_id,
            )
            continue
        origins[user_event_id] = InputOriginResponse(
            authored_via="longhouse",
            session_input_id=int(session_input.id),
            client_request_id=session_input.client_request_id,
        )

    unclaimed_user_events = {event_id: event for event_id, event in user_events.items() if event_id not in seen_event_ids}
    if not unclaimed_user_events:
        return origins

    pending_turns = (
        store.db.query(SessionTurn, SessionInput)
        .join(
            SessionInput,
            and_(
                SessionInput.id == SessionTurn.session_input_id,
                SessionInput.session_id == SessionTurn.session_id,
            ),
        )
        .filter(
            SessionTurn.session_id.in_(session_ids),
            SessionTurn.user_event_id.is_(None),
            SessionTurn.session_input_id.isnot(None),
            SessionTurn.expected_user_text_hash.isnot(None),
            SessionTurn.state != "failed",
        )
        .order_by(SessionTurn.id.asc())
        .all()
    )
    if not pending_turns:
        return origins

    matches_by_turn_id: dict[int, list[int]] = {}
    turn_rows: dict[int, tuple[SessionTurn, SessionInput]] = {}
    matched_turns_by_event_id: dict[int, list[int]] = {}
    for turn, session_input in pending_turns:
        turn_id = int(getattr(turn, "id", 0) or 0)
        expected_hash = str(getattr(turn, "expected_user_text_hash", "") or "")
        if turn_id <= 0 or not expected_hash:
            continue
        baseline_event_id = int(getattr(turn, "baseline_event_id", 0) or 0)
        candidate_ids: list[int] = []
        for event_id, event in unclaimed_user_events.items():
            if event.session_id != turn.session_id:
                continue
            if baseline_event_id > 0 and event_id <= baseline_event_id:
                continue
            content_text = str(getattr(event, "content_text", "") or "")
            normalized_user_text = strip_claude_channel_wrapper(content_text)
            if hash_user_text(normalized_user_text) != expected_hash:
                continue
            candidate_ids.append(event_id)
            matched_turns_by_event_id.setdefault(event_id, []).append(turn_id)
        if candidate_ids:
            matches_by_turn_id[turn_id] = candidate_ids
            turn_rows[turn_id] = (turn, session_input)

    for turn_id, candidate_ids in matches_by_turn_id.items():
        if len(candidate_ids) != 1:
            continue
        event_id = candidate_ids[0]
        if len(matched_turns_by_event_id.get(event_id, [])) != 1:
            continue
        _, session_input = turn_rows[turn_id]
        origins[event_id] = InputOriginResponse(
            authored_via="longhouse",
            session_input_id=int(session_input.id),
            client_request_id=session_input.client_request_id,
        )
    return origins


def build_event_input_origin_response(store: AgentsStore, event: AgentEvent) -> InputOriginResponse | None:
    if str(getattr(event, "role", "") or "").strip().lower() != "user":
        return None
    return build_event_input_origin_map(store, [event]).get(int(event.id))


def build_session_turn_response(turn: SessionTurn) -> SessionTurnResponse:
    timing = build_session_turn_timing_response(turn)
    return SessionTurnResponse(
        id=int(turn.id),
        session_id=str(turn.session_id),
        request_id=turn.request_id,
        session_input_id=turn.session_input_id,
        state=turn.state,
        terminal_phase=turn.terminal_phase,
        error_code=turn.error_code,
        user_event_id=turn.user_event_id,
        durable_assistant_event_id=turn.durable_assistant_event_id,
        baseline_event_id=turn.baseline_event_id,
        baseline_observation_cursor=turn.baseline_observation_cursor,
        user_submitted_at=turn.user_submitted_at,
        send_accepted_at=turn.send_accepted_at,
        active_phase_observed_at=turn.active_phase_observed_at,
        terminal_at=turn.terminal_at,
        durable_at=turn.durable_at,
        created_at=turn.created_at,
        updated_at=turn.updated_at,
        timing=timing,
    )


def build_session_turn_timing_response(turn: SessionTurn) -> SessionTurnTimingResponse:
    user_submitted_at = normalize_utc(turn.user_submitted_at)
    send_accepted_at = normalize_utc(turn.send_accepted_at)
    active_phase_observed_at = normalize_utc(turn.active_phase_observed_at)
    terminal_at = normalize_utc(turn.terminal_at)
    durable_at = normalize_utc(turn.durable_at)
    completed_at = durable_at or terminal_at

    return SessionTurnTimingResponse(
        submit_to_send_ms=_duration_ms(user_submitted_at, send_accepted_at),
        submit_to_active_ms=_duration_ms(user_submitted_at, active_phase_observed_at),
        submit_to_terminal_ms=_duration_ms(user_submitted_at, terminal_at),
        active_to_terminal_ms=_duration_ms(active_phase_observed_at, terminal_at),
        terminal_to_durable_ms=_duration_ms(terminal_at, durable_at),
        total_turn_time_ms=_duration_ms(user_submitted_at, completed_at),
    )


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    # Clamp small ordering/clock skew glitches to 0 so derived durations stay monotonic.
    elapsed_ms = round((end - start).total_seconds() * 1000)
    return max(0, int(elapsed_ms))


def format_age(dt: datetime) -> str:
    """Format a datetime as human-readable relative time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt

    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if weeks == 1:
        return "1w ago"
    return f"{weeks}w ago"
