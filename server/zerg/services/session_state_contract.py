"""Canonical orthogonal session-state facts and presentation.

This module is the semantic seam between today's legacy SQLite rows and the
future catalogd facts store.  It accepts already-loaded observations and
capabilities, projects one versioned facts object, and derives presentation
without consuming another presentation object.

See ``docs/specs/runtime-display-contract.md``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_liveness_facts import SessionLivenessFacts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runtime import RUN_TERMINAL_STATES
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import compact_runtime_tool_label
from zerg.utils.time import normalize_utc

STATE_CONTRACT_VERSION = 1
PRESENTATION_POLICY_VERSION = 1

PRIMARY_PRESENTATION_KEYS: tuple[str, ...] = (
    "closed",
    "launch_failed",
    "starting",
    "needs_answer",
    "needs_approval",
    "thinking",
    "executing",
    "stalled",
    "blocked",
    "idle",
    "ended",
    "activity_unknown",
)
ACCESS_PRESENTATION_KEYS: tuple[str, ...] = (
    "live_control",
    "reattach",
    "control_degraded",
    "control_disconnected",
    "control_unknown",
    "read_only",
    "observe_only",
    "search_only",
)
TRANSCRIPT_PRESENTATION_KEYS: tuple[str, ...] = ("transcript_lagging",)


def session_state_contract_manifest() -> dict[str, Any]:
    """Return the small stable manifest clients compare during deep health."""

    payload: dict[str, Any] = {
        "state_contract_version": STATE_CONTRACT_VERSION,
        "presentation_policy_version": PRESENTATION_POLICY_VERSION,
        "presentation_keys": {
            "primary": list(PRIMARY_PRESENTATION_KEYS),
            "access": list(ACCESS_PRESENTATION_KEYS),
            "transcript": list(TRANSCRIPT_PRESENTATION_KEYS),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "fingerprint": hashlib.sha256(encoded).hexdigest()}


ActivityState = Literal["thinking", "executing", "quiescent", "blocked", "stalled", "unknown"]
RunLifecycle = Literal["starting", "running", "ended", "unknown"]
ConnectionState = Literal["connected", "degraded", "disconnected", "unknown", "not_applicable"]
ActionState = Literal["available", "unavailable", "unknown"]
TranscriptConvergence = Literal["current", "lagging", "unknown"]
SessionMode = Literal["shadow", "helm", "console", "unknown"]

_ENDED_RUNTIME_STATES = {"session_ended", "process_gone", *RUN_TERMINAL_STATES}
_ACTIVITY_MAP: dict[str, ActivityState] = {
    "thinking": "thinking",
    "running": "executing",
    "idle": "quiescent",
    "needs_user": "quiescent",
    "blocked": "blocked",
    "stalled": "stalled",
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class SessionDispositionFacts(_FrozenModel):
    state: Literal["open", "closed"]
    closed_at: datetime | None = None
    close_reason: str | None = None


class SessionRunFacts(_FrozenModel):
    id: str | None = None
    lifecycle: RunLifecycle
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None


class SessionLaunchFacts(_FrozenModel):
    state: Literal["pending", "dispatched", "failed", "adopted", "abandoned"]
    error_code: str | None = None
    error_message: str | None = None


class SessionActivityFacts(_FrozenModel):
    state: ActivityState
    raw_kind: str | None = None
    tool: str | None = None
    source: str | None = None
    observed_at: datetime | None = None
    valid_until: datetime | None = None


class SessionActionAvailability(_FrozenModel):
    state: ActionState
    reason: str | None = None


class SessionControlActions(_FrozenModel):
    start_turn: SessionActionAvailability = Field(
        default_factory=lambda: SessionActionAvailability(state="unavailable", reason="not_console")
    )
    send_input: SessionActionAvailability
    interrupt: SessionActionAvailability
    terminate: SessionActionAvailability
    reattach: SessionActionAvailability
    resume: SessionActionAvailability


class SessionControlFacts(_FrozenModel):
    ownership: Literal["owned", "unowned"]
    connection: ConnectionState
    connection_id: int | str | None = None
    lease_generation: str | None = None
    control_plane: str | None = None
    observed_at: datetime | None = None
    valid_until: datetime | None = None
    actions: SessionControlActions


class SessionPendingInteractionFacts(_FrozenModel):
    id: str
    kind: Literal["question", "permission", "approval"]
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    provider_request_id: str | None = None
    can_respond: bool = False


class SessionTranscriptFacts(_FrozenModel):
    convergence: TranscriptConvergence
    source_revision: int | None = None
    durable_revision: int | None = None
    render_revision: int | None = None
    last_append_at: datetime | None = None
    searchable: bool = False
    live_observation: bool = False


class SessionHostFacts(_FrozenModel):
    state: Literal["online", "stale", "offline", "unknown"]
    observed_at: datetime | None = None


class SessionPresentationLabel(_FrozenModel):
    key: str
    label: str
    tone: str
    observed_at: datetime | None = None


class SessionPresentation(_FrozenModel):
    primary: SessionPresentationLabel | None = None
    access: SessionPresentationLabel | None = None
    transcript: SessionPresentationLabel | None = None


class SessionStateFacts(_FrozenModel):
    state_contract_version: int = Field(STATE_CONTRACT_VERSION, frozen=True)
    presentation_policy_version: int = Field(PRESENTATION_POLICY_VERSION, frozen=True)
    mode: SessionMode
    disposition: SessionDispositionFacts
    launch: SessionLaunchFacts | None = None
    run: SessionRunFacts | None = None
    activity: SessionActivityFacts
    control: SessionControlFacts
    pending_interaction: SessionPendingInteractionFacts | None = None
    transcript: SessionTranscriptFacts
    host: SessionHostFacts
    presentation: SessionPresentation
    commit_seq: int | None = None


def _clean(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _now(value: datetime | None) -> datetime:
    return normalize_utc(value) or datetime.now(timezone.utc)


def _mode(
    *,
    session: Any,
    execution_lifetime: str | None,
    capabilities: KernelSessionCapabilities,
) -> SessionMode:
    raw_surface = getattr(session, "launch_surface", None)
    origin_kind = _clean(getattr(session, "origin_kind", None))
    raw_execution_home = getattr(session, "execution_home", None)
    surface = _clean(getattr(raw_surface, "value", raw_surface))
    execution_home = _clean(getattr(raw_execution_home, "value", raw_execution_home))
    if origin_kind == "console" or execution_lifetime == "one_shot" or surface in {"web", "ios", "api"}:
        return "console"
    if capabilities.observe_only:
        return "shadow"
    if (
        execution_lifetime == "live_control"
        or execution_home == "managed_local"
        or capabilities.control_owned
        or capabilities.live_control_available
        or capabilities.host_reattach_available
    ):
        return "helm"
    if execution_home in {None, "local", "imported", "unmanaged", "unmanaged_local"}:
        return "shadow"
    return "unknown"


def _disposition(*, session: Any, runtime_view: SessionRuntimeView | None) -> SessionDispositionFacts:
    closed_at = normalize_utc(getattr(session, "closed_at", None))
    close_reason = _clean(getattr(session, "close_reason", None))
    if closed_at is not None:
        return SessionDispositionFacts(state="closed", closed_at=closed_at, close_reason=close_reason or "user_closed")
    terminal = _clean(runtime_view.terminal_state if runtime_view is not None else None)
    if terminal == "user_closed":
        terminal_at = normalize_utc(getattr(runtime_view, "timeline_anchor_at", None))
        return SessionDispositionFacts(state="closed", closed_at=terminal_at, close_reason="user_closed")
    return SessionDispositionFacts(state="open")


def _activity(runtime_view: SessionRuntimeView | None) -> SessionActivityFacts:
    if runtime_view is None:
        return SessionActivityFacts(state="unknown")
    raw_kind = _clean(runtime_view.presence_state) or _clean(runtime_view.runtime_phase)
    state = _ACTIVITY_MAP.get(raw_kind or "", "unknown") if runtime_view.confidence == "live" else "unknown"
    source = _clean(runtime_view.runtime_source)
    if source in {"fallback", "progress"}:
        state = "unknown"
    return SessionActivityFacts(
        state=state,
        raw_kind=raw_kind,
        tool=compact_runtime_tool_label(runtime_view.active_tool or runtime_view.presence_tool),
        source=source,
        observed_at=normalize_utc(runtime_view.presence_updated_at or runtime_view.phase_started_at),
        valid_until=normalize_utc(runtime_view.freshness_expires_at),
    )


def _launch(
    *,
    launch_state: str | None,
    error_code: str | None,
    error_message: str | None,
) -> SessionLaunchFacts | None:
    state_map = {
        "launching": "pending",
        "launching_unknown": "dispatched",
        "live": "adopted",
        "launch_failed": "failed",
        "launch_orphaned": "abandoned",
        "pending": "pending",
        "dispatched": "dispatched",
        "adopted": "adopted",
        "failed": "failed",
        "abandoned": "abandoned",
    }
    normalized = state_map.get(_clean(launch_state) or "")
    if normalized is None:
        return None
    return SessionLaunchFacts(
        state=normalized,
        error_code=_clean(error_code),
        error_message=_clean(error_message),
    )


def _run(
    *,
    session: Any,
    runtime_view: SessionRuntimeView | None,
    capabilities: KernelSessionCapabilities,
    liveness: SessionLivenessFacts | None,
    activity: SessionActivityFacts,
    launch: SessionLaunchFacts | None,
) -> SessionRunFacts | None:
    if launch is not None and launch.state in {"pending", "dispatched"} and capabilities.run_id is None:
        return SessionRunFacts(lifecycle="starting", started_at=normalize_utc(getattr(session, "started_at", None)))

    terminal = _clean(runtime_view.terminal_state if runtime_view is not None else None)
    started_at = normalize_utc(capabilities.run_started_at) or normalize_utc(getattr(session, "started_at", None))
    ended_at = normalize_utc(capabilities.run_ended_at)
    run_id = _clean(capabilities.run_id)
    if terminal in _ENDED_RUNTIME_STATES or capabilities.staleness_reason == "process_ended":
        return SessionRunFacts(
            id=run_id,
            lifecycle="ended",
            started_at=started_at,
            ended_at=ended_at,
            end_reason=terminal or _clean(capabilities.run_end_reason) or "process_ended",
        )
    if run_id is None and activity.state == "unknown" and liveness.process.status != "observed":
        return None
    if liveness.process.status == "observed" or activity.state != "unknown":
        lifecycle: RunLifecycle = "running"
    else:
        lifecycle = "unknown"
    return SessionRunFacts(
        id=run_id,
        lifecycle=lifecycle,
        started_at=started_at,
    )


def _action_unavailable_reason(*, ownership: str, connection: ConnectionState, fallback: str | None) -> tuple[ActionState, str]:
    if ownership == "unowned":
        return "unavailable", "observe_only"
    if connection == "unknown":
        return "unknown", "control_freshness_unknown"
    if connection == "degraded":
        return "unavailable", "control_degraded"
    if connection == "disconnected":
        return "unavailable", "control_disconnected"
    return "unavailable", fallback or "not_granted"


def _operation(
    *,
    available: bool,
    ownership: str,
    connection: ConnectionState,
    fallback: str | None,
    allow_disconnected: bool = False,
) -> SessionActionAvailability:
    if available and (connection == "connected" or (allow_disconnected and connection == "disconnected")):
        return SessionActionAvailability(state="available")
    state, reason = _action_unavailable_reason(ownership=ownership, connection=connection, fallback=fallback)
    return SessionActionAvailability(state=state, reason=reason)


def _control(
    *,
    capabilities: KernelSessionCapabilities,
    liveness: SessionLivenessFacts,
    now: datetime,
) -> SessionControlFacts:
    ownership = (
        "owned" if capabilities.control_owned or capabilities.live_control_available or capabilities.host_reattach_available else "unowned"
    )
    observed_at = normalize_utc(liveness.control.last_seen_at)
    valid_until = normalize_utc(liveness.control.expires_at)
    expired = valid_until is not None and valid_until <= now
    raw_connection = _clean(liveness.control.state)
    if ownership == "unowned":
        connection: ConnectionState = "not_applicable"
    elif expired:
        connection = "unknown"
    elif capabilities.live_control_available:
        connection = "degraded" if _clean(capabilities.connection_state) == "degraded" else "connected"
    elif capabilities.host_reattach_available:
        connection = "disconnected"
    elif raw_connection in {"online", "attached"}:
        connection = "connected"
    elif raw_connection == "degraded":
        connection = "degraded"
    elif raw_connection in {"offline", "detached", "released", "ended"}:
        connection = "disconnected"
    else:
        connection = "unknown"

    fallback = _clean(capabilities.staleness_reason)
    reattach_available = bool(capabilities.host_reattach_available and not capabilities.live_control_available)
    start_turn_blocked_by = capabilities.start_turn_blocked_by
    if capabilities.can_start_turn and liveness.host.state in {"offline", "stale"}:
        start_turn_blocked_by = "machine_offline"
    actions = SessionControlActions(
        start_turn=(
            SessionActionAvailability(state="available")
            if capabilities.can_start_turn and start_turn_blocked_by is None
            else SessionActionAvailability(
                state="unavailable",
                reason=start_turn_blocked_by or "not_console",
            )
        ),
        send_input=_operation(
            available=bool(capabilities.can_send_input and capabilities.live_control_available),
            ownership=ownership,
            connection=connection,
            fallback=fallback,
        ),
        interrupt=_operation(
            available=bool(capabilities.can_interrupt and capabilities.live_control_available),
            ownership=ownership,
            connection=connection,
            fallback=fallback,
        ),
        terminate=_operation(
            available=bool(capabilities.can_terminate and capabilities.live_control_available),
            ownership=ownership,
            connection=connection,
            fallback=fallback,
        ),
        reattach=_operation(
            available=reattach_available,
            ownership=ownership,
            connection=connection,
            fallback="already_connected" if capabilities.live_control_available else fallback,
            allow_disconnected=True,
        ),
        resume=_operation(
            available=bool(capabilities.can_resume and (capabilities.live_control_available or reattach_available)),
            ownership=ownership,
            connection=connection,
            fallback=fallback,
            allow_disconnected=True,
        ),
    )
    return SessionControlFacts(
        ownership=ownership,
        connection=connection,
        connection_id=capabilities.adapter_connection_id or capabilities.connection_id,
        lease_generation=capabilities.lease_generation,
        control_plane=_clean(capabilities.control_plane),
        observed_at=observed_at,
        valid_until=valid_until,
        actions=actions,
    )


def _interaction(pause_request: dict[str, Any] | None) -> SessionPendingInteractionFacts | None:
    if not isinstance(pause_request, dict) or _clean(pause_request.get("status")) != "pending":
        return None
    raw_kind = _clean(pause_request.get("kind"))
    kind_map = {
        "structured_question": "question",
        "permission_prompt": "permission",
        "plan_approval": "approval",
    }
    kind = kind_map.get(raw_kind or "")
    if kind is None:
        return None
    interaction_id = _clean(pause_request.get("id") or pause_request.get("request_key") or pause_request.get("provider_request_id"))
    if interaction_id is None:
        return None
    return SessionPendingInteractionFacts(
        id=interaction_id,
        kind=kind,
        opened_at=normalize_utc(pause_request.get("occurred_at") or pause_request.get("created_at")),
        resolved_at=normalize_utc(pause_request.get("resolved_at")),
        provider_request_id=_clean(pause_request.get("provider_request_id")),
        can_respond=bool(pause_request.get("can_respond")),
    )


def _transcript(
    *,
    session: Any,
    last_activity_at: datetime | None,
    has_visible_transcript_preview: bool,
    has_pending_response_turn: bool,
    user_messages: int,
    assistant_messages: int,
    archive_state: str | None,
    live_observation: bool,
    source_revision: int | None = None,
    durable_revision: int | None = None,
    render_revision: int | None = None,
    last_append_at: datetime | None = None,
) -> SessionTranscriptFacts:
    legacy_revision = int(getattr(session, "transcript_revision", 0) or 0)
    normalized_archive = _clean(archive_state)
    lagging = bool(
        normalized_archive == "pending"
        or (not has_visible_transcript_preview and (has_pending_response_turn or user_messages > assistant_messages))
    )
    if lagging:
        convergence: TranscriptConvergence = "lagging"
    elif normalized_archive in {"current", "legacy_hot"}:
        convergence = "current"
    else:
        convergence = "unknown"
    return SessionTranscriptFacts(
        convergence=convergence,
        source_revision=source_revision,
        durable_revision=durable_revision,
        render_revision=render_revision,
        last_append_at=normalize_utc(last_append_at),
        searchable=bool(legacy_revision > 0 or user_messages > 0 or assistant_messages > 0),
        live_observation=live_observation,
    )


def project_pending_interaction_facts(
    pause_request: dict[str, Any] | None,
) -> SessionPendingInteractionFacts | None:
    """Project one durable interaction axis without presentation policy."""

    return _interaction(pause_request)


def project_transcript_facts(
    *,
    session: Any,
    last_activity_at: datetime | None,
    has_visible_transcript_preview: bool = False,
    has_pending_response_turn: bool = False,
    user_messages: int = 0,
    assistant_messages: int = 0,
    archive_state: str | None = None,
    live_observation: bool = False,
    source_revision: int | None = None,
    durable_revision: int | None = None,
    render_revision: int | None = None,
    transcript_last_append_at: datetime | None = None,
) -> SessionTranscriptFacts:
    """Project the current bounded transcript axis from catalog coordinates."""

    return _transcript(
        session=session,
        last_activity_at=last_activity_at,
        has_visible_transcript_preview=has_visible_transcript_preview,
        has_pending_response_turn=has_pending_response_turn,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        archive_state=archive_state,
        live_observation=live_observation,
        source_revision=source_revision,
        durable_revision=durable_revision,
        render_revision=render_revision,
        last_append_at=transcript_last_append_at,
    )


def build_archive_session_state_facts(
    *,
    session: Any,
    capabilities: KernelSessionCapabilities,
    launch_state: str | None = None,
    launch_error_code: str | None = None,
    launch_error_message: str | None = None,
    execution_lifetime: str | None = None,
    last_activity_at: datetime | None = None,
    has_visible_transcript_preview: bool = False,
    has_pending_response_turn: bool = False,
    user_messages: int = 0,
    assistant_messages: int = 0,
    archive_state: str | None = None,
    pause_request: dict[str, Any] | None = None,
) -> SessionStateFacts:
    """Project cold/archive rows without inventing ephemeral runtime truth."""

    unavailable = SessionActionAvailability(state="unavailable", reason="search_only")
    control = SessionControlFacts(
        ownership="unowned",
        connection="not_applicable",
        actions=SessionControlActions(
            start_turn=SessionActionAvailability(state="unavailable", reason="not_console"),
            send_input=unavailable,
            interrupt=unavailable,
            terminate=unavailable,
            reattach=unavailable,
            resume=unavailable,
        ),
    )
    return assemble_session_state_facts(
        mode=_mode(session=session, execution_lifetime=execution_lifetime, capabilities=capabilities),
        disposition=_disposition(session=session, runtime_view=None),
        launch=_launch(
            launch_state=launch_state,
            error_code=launch_error_code,
            error_message=launch_error_message,
        ),
        run=None,
        activity=SessionActivityFacts(state="unknown"),
        control=control,
        pending_interaction=project_pending_interaction_facts(pause_request),
        transcript=_transcript(
            session=session,
            last_activity_at=last_activity_at,
            has_visible_transcript_preview=has_visible_transcript_preview,
            has_pending_response_turn=has_pending_response_turn,
            user_messages=user_messages,
            assistant_messages=assistant_messages,
            archive_state=archive_state,
            live_observation=False,
        ),
        host=SessionHostFacts(state="unknown"),
    )


def assemble_session_state_facts(
    *,
    mode: SessionMode,
    disposition: SessionDispositionFacts,
    launch: SessionLaunchFacts | None,
    run: SessionRunFacts | None,
    activity: SessionActivityFacts,
    control: SessionControlFacts,
    pending_interaction: SessionPendingInteractionFacts | None,
    transcript: SessionTranscriptFacts,
    host: SessionHostFacts,
    commit_seq: int | None = None,
) -> SessionStateFacts:
    """Assemble orthogonal axes and apply the single presentation policy."""

    primary = _primary(
        disposition=disposition,
        launch=launch,
        run=run,
        activity=activity,
        interaction=pending_interaction,
    )
    access = _access(control=control, transcript=transcript)
    transcript_label = (
        SessionPresentationLabel(
            key="transcript_lagging",
            label="Transcript catching up",
            tone="inactive",
            observed_at=transcript.last_append_at,
        )
        if transcript.convergence == "lagging"
        else None
    )
    return SessionStateFacts(
        mode=mode,
        disposition=disposition,
        launch=launch,
        run=run,
        activity=activity,
        control=control,
        pending_interaction=pending_interaction,
        transcript=transcript,
        host=host,
        presentation=SessionPresentation(primary=primary, access=access, transcript=transcript_label),
        commit_seq=commit_seq,
    )


def _primary(
    *,
    disposition: SessionDispositionFacts,
    launch: SessionLaunchFacts | None,
    run: SessionRunFacts | None,
    activity: SessionActivityFacts,
    interaction: SessionPendingInteractionFacts | None,
) -> SessionPresentationLabel | None:
    if disposition.state == "closed":
        return SessionPresentationLabel(key="closed", label="Closed", tone="closed", observed_at=disposition.closed_at)
    if launch is not None and launch.state in {"failed", "abandoned"}:
        return SessionPresentationLabel(key="launch_failed", label="Launch failed", tone="blocked")
    if run is not None and run.lifecycle == "starting":
        return SessionPresentationLabel(key="starting", label="Starting", tone="active", observed_at=run.started_at)
    if interaction is not None:
        if interaction.kind == "question":
            return SessionPresentationLabel(
                key="needs_answer",
                label="Needs answer",
                tone="blocked",
                observed_at=interaction.opened_at,
            )
        return SessionPresentationLabel(
            key="needs_approval",
            label="Needs approval",
            tone="blocked",
            observed_at=interaction.opened_at,
        )
    if activity.state == "thinking":
        return SessionPresentationLabel(key="thinking", label="Thinking", tone="thinking", observed_at=activity.observed_at)
    if activity.state == "executing":
        label = f"Using {activity.tool}" if activity.tool else "Running"
        return SessionPresentationLabel(key="executing", label=label, tone="running", observed_at=activity.observed_at)
    if activity.state == "stalled":
        return SessionPresentationLabel(key="stalled", label="Stalled", tone="stalled", observed_at=activity.observed_at)
    if activity.state == "blocked":
        return SessionPresentationLabel(key="blocked", label="Blocked", tone="blocked", observed_at=activity.observed_at)
    if activity.state == "quiescent":
        return SessionPresentationLabel(key="idle", label="Idle", tone="idle", observed_at=activity.observed_at)
    if run is not None and run.lifecycle == "ended":
        return SessionPresentationLabel(key="ended", label="Ended", tone="closed", observed_at=run.ended_at)
    if run is not None:
        return SessionPresentationLabel(key="activity_unknown", label="Activity unknown", tone="quiet")
    return None


def _access(*, control: SessionControlFacts, transcript: SessionTranscriptFacts) -> SessionPresentationLabel | None:
    live_actions = (
        control.actions.start_turn,
        control.actions.send_input,
        control.actions.interrupt,
        control.actions.terminate,
    )
    if control.ownership == "owned" and any(action.state == "available" for action in live_actions):
        return SessionPresentationLabel(
            key="live_control",
            label="Live control",
            tone="live",
            observed_at=control.observed_at,
        )
    if control.actions.reattach.state == "available":
        return SessionPresentationLabel(
            key="reattach",
            label="Reattach",
            tone="reattach",
            observed_at=control.observed_at,
        )
    if control.connection == "degraded":
        return SessionPresentationLabel(
            key="control_degraded",
            label="Control degraded",
            tone="degraded",
            observed_at=control.observed_at,
        )
    if control.ownership == "owned" and control.connection == "unknown":
        return SessionPresentationLabel(
            key="control_unknown",
            label="Control unknown",
            tone="inactive",
            observed_at=control.observed_at,
        )
    if control.ownership == "owned" and control.connection == "connected":
        return SessionPresentationLabel(
            key="read_only",
            label="Read only",
            tone="neutral",
            observed_at=control.observed_at,
        )
    if control.ownership == "unowned" and transcript.live_observation:
        return SessionPresentationLabel(
            key="observe_only",
            label="Observe only",
            tone="observe",
            observed_at=transcript.last_append_at,
        )
    if transcript.searchable:
        return SessionPresentationLabel(key="search_only", label="Search only", tone="search")
    return None


def build_session_state_facts(
    *,
    session: Any,
    runtime_view: SessionRuntimeView | None,
    capabilities: KernelSessionCapabilities,
    liveness: SessionLivenessFacts | None,
    pause_request: dict[str, Any] | None = None,
    launch_state: str | None = None,
    launch_error_code: str | None = None,
    launch_error_message: str | None = None,
    execution_lifetime: str | None = None,
    last_activity_at: datetime | None = None,
    has_visible_transcript_preview: bool = False,
    has_pending_response_turn: bool = False,
    user_messages: int = 0,
    assistant_messages: int = 0,
    archive_state: str | None = None,
    now: datetime | None = None,
) -> SessionStateFacts:
    """Project the target contract from legacy evidence without serving legacy copy."""

    current_now = _now(now)
    if liveness is None:
        liveness = build_session_liveness_facts(
            runtime_view=runtime_view,
            capabilities=capabilities,
            last_activity_at=last_activity_at,
            now=current_now,
        )
    disposition = _disposition(session=session, runtime_view=runtime_view)
    activity = _activity(runtime_view)
    launch = _launch(
        launch_state=launch_state,
        error_code=launch_error_code,
        error_message=launch_error_message,
    )
    run = _run(
        session=session,
        runtime_view=runtime_view,
        capabilities=capabilities,
        liveness=liveness,
        activity=activity,
        launch=launch,
    )
    control = _control(capabilities=capabilities, liveness=liveness, now=current_now)
    interaction = _interaction(pause_request)
    transcript = _transcript(
        session=session,
        last_activity_at=last_activity_at,
        has_visible_transcript_preview=has_visible_transcript_preview,
        has_pending_response_turn=has_pending_response_turn,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        archive_state=archive_state,
        live_observation=bool(has_visible_transcript_preview or capabilities.observe_only),
    )
    host_state = _clean(liveness.host.state)
    if host_state not in {"online", "stale", "offline"}:
        host_state = "unknown"
    return assemble_session_state_facts(
        mode=_mode(
            session=session,
            execution_lifetime=execution_lifetime,
            capabilities=capabilities,
        ),
        disposition=disposition,
        launch=launch,
        run=run,
        activity=activity,
        control=control,
        pending_interaction=interaction,
        transcript=transcript,
        host=SessionHostFacts(
            state=host_state,
            observed_at=normalize_utc(liveness.host.last_seen_at),
        ),
    )


__all__ = [
    "ACCESS_PRESENTATION_KEYS",
    "PRESENTATION_POLICY_VERSION",
    "PRIMARY_PRESENTATION_KEYS",
    "STATE_CONTRACT_VERSION",
    "TRANSCRIPT_PRESENTATION_KEYS",
    "SessionActionAvailability",
    "SessionActivityFacts",
    "SessionControlFacts",
    "SessionDispositionFacts",
    "SessionLaunchFacts",
    "SessionPendingInteractionFacts",
    "SessionPresentation",
    "SessionPresentationLabel",
    "SessionRunFacts",
    "SessionStateFacts",
    "SessionTranscriptFacts",
    "assemble_session_state_facts",
    "build_session_state_facts",
    "project_pending_interaction_facts",
    "project_transcript_facts",
    "session_state_contract_manifest",
]
