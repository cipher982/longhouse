"""Session response models and builders.

Shared data layer for converting ORM session/event objects into API response
models.  Both the ``agents`` and ``timeline`` router families import from here
— no router should ever import response models from another router.
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.agents_store import AgentsStore
from zerg.services.managed_local_transport import build_managed_local_attach_command
from zerg.services.session_capabilities import build_session_capabilities
from zerg.services.session_capabilities import build_session_capability_display
from zerg.services.session_capabilities import project_current_session_capabilities
from zerg.services.session_capabilities import project_current_session_capabilities_from_facts
from zerg.services.session_liveness_facts import build_session_liveness_facts
from zerg.services.session_runner_state import managed_runner_host_state
from zerg.services.session_runtime import SessionLiveTranscriptOverlay
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime import should_include_runtime_view
from zerg.services.session_runtime_display import TERMINAL_DISCONNECTED_REASON
from zerg.services.session_runtime_display import build_session_runtime_display
from zerg.services.session_runtime_display import compact_runtime_tool_label
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_loop_mode import SessionLoopMode
from zerg.session_loop_mode import coerce_session_loop_mode
from zerg.utils.time import UTCBaseModel
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


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
) -> SessionCapabilitiesResponse:
    capability_flags = capability_flags or build_session_capabilities(session)
    if runtime_facts is not None:
        capability_flags = project_current_session_capabilities_from_facts(
            capability_flags,
            liveness_facts=runtime_facts,
        )
    else:
        capability_flags = project_current_session_capabilities(capability_flags, runtime_display=runtime_display)
    host_label = None
    if session is not None:
        host_label = str(getattr(session, "source_runner_name", "") or "").strip() or None
    runtime_facts_lifecycle = getattr(runtime_facts, "lifecycle", None)
    lifecycle = str(getattr(runtime_facts_lifecycle, "state", "") or "").strip()
    if not lifecycle and runtime_display is not None:
        lifecycle = str(getattr(runtime_display, "lifecycle", "") or "").strip()
    capability_display = build_session_capability_display(capability_flags, host_label=host_label, lifecycle=lifecycle)
    return SessionCapabilitiesResponse(
        live_control_available=capability_flags.live_control_available,
        host_reattach_available=capability_flags.host_reattach_available,
        reply_to_live_session_available=capability_flags.reply_to_live_session_available,
        can_queue_next_input=capability_flags.can_queue_next_input,
        can_steer_active_turn=capability_flags.can_steer_active_turn,
        display_label=capability_display.label,
        display_detail=capability_display.detail,
        display_tone=capability_display.tone,
    )


def build_session_runtime_display_response(
    *,
    runtime_overlay: SessionRuntimeView | None,
    capability_flags,
    ended_at: datetime | None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
) -> SessionRuntimeDisplayResponse | None:
    if runtime_overlay is None:
        return None
    display = build_session_runtime_display(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        ended_at=ended_at,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    return SessionRuntimeDisplayResponse(
        truth_tier=display.truth_tier,
        signal_tier=display.signal_tier,
        state=display.state,
        tone=display.tone,
        headline=display.headline,
        detail=display.detail,
        phase_label=display.phase_label,
        compact_tool_label=display.compact_tool_label,
        is_live=display.is_live,
        is_executing=display.is_executing,
        needs_attention=display.needs_attention,
        is_idle=display.is_idle,
        is_stalled=display.is_stalled,
        is_managed_local_truth=display.is_managed_local_truth,
        has_signal=display.has_signal,
        control_path=display.control_path,
        activity_recency=display.activity_recency,
        lifecycle=display.lifecycle,
        host_state=display.host_state,
        terminal_reason=display.terminal_reason,
    )


def _live_transcript_superseded_by_durable_activity(
    overlay: SessionLiveTranscriptOverlay,
    *,
    last_activity_at: datetime | None,
) -> bool:
    if last_activity_at is None:
        return False
    activity_at = normalize_utc(last_activity_at)
    overlay_at = normalize_utc(overlay.occurred_at) or normalize_utc(overlay.received_at)
    if activity_at is None or overlay_at is None:
        return False
    return activity_at > overlay_at


def build_live_transcript_response(
    overlay: SessionLiveTranscriptOverlay | None,
    *,
    last_activity_at: datetime | None = None,
) -> SessionLiveTranscriptResponse | None:
    if overlay is None:
        return None
    if _live_transcript_superseded_by_durable_activity(
        overlay,
        last_activity_at=last_activity_at,
    ):
        return None
    return SessionLiveTranscriptResponse(
        text=overlay.text,
        source=overlay.source,
        received_at=overlay.received_at,
        occurred_at=overlay.occurred_at,
        thread_id=overlay.thread_id,
        turn_id=overlay.turn_id,
        seq=overlay.seq,
        method=overlay.method,
        is_complete=overlay.is_complete,
    )


def build_session_liveness_facts_response(
    *,
    runtime_overlay: SessionRuntimeView | None,
    capability_flags,
    last_activity_at: datetime | None,
    binding_overlay=None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
) -> SessionLivenessFactsResponse | None:
    if runtime_overlay is None:
        return None
    facts = build_session_liveness_facts(
        runtime_view=runtime_overlay,
        capabilities=capability_flags,
        last_activity_at=last_activity_at,
        binding_overlay=binding_overlay,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    return SessionLivenessFactsResponse(
        control_path=facts.control_path,
        process_state=facts.process_state,
        host=HostObservationResponse(
            state=facts.host.state,
            last_seen_at=facts.host.last_seen_at,
            source=facts.host.source,
        ),
        process=ProcessObservationResponse(
            status=facts.process.status,
            pid=facts.process.pid,
            process_start_time=facts.process.process_start_time,
            observed_at=facts.process.observed_at,
            last_seen_at=facts.process.last_seen_at,
            source_mtime=facts.process.source_mtime,
            source_path=facts.process.source_path,
            reason=facts.process.reason,
            source=facts.process.source,
        ),
        phase=PhaseObservationResponse(
            kind=facts.phase.kind,
            tool=facts.phase.tool,
            source=facts.phase.source,
            observed_at=facts.phase.observed_at,
            expires_at=facts.phase.expires_at,
        ),
        activity=ActivityObservationResponse(
            last_transcript_at=facts.activity.last_transcript_at,
            last_runtime_signal_at=facts.activity.last_runtime_signal_at,
            last_progress_at=facts.activity.last_progress_at,
        ),
        lifecycle=LifecycleFactResponse(
            state=facts.lifecycle.state,
            reason=facts.lifecycle.reason,
            observed_at=facts.lifecycle.observed_at,
        ),
    )


def build_session_timeline_card_response(
    *,
    runtime_facts: SessionLivenessFactsResponse | None,
    capability_flags,
) -> TimelineCardPresentationResponse:
    if runtime_facts is not None:
        control_path = runtime_facts.control_path
    else:
        has_managed_control_path = (
            getattr(capability_flags, "live_control_available", False)
            or getattr(capability_flags, "host_reattach_available", False)
            or getattr(capability_flags, "reply_to_live_session_available", False)
        )
        control_path = "managed" if has_managed_control_path else "unmanaged"
    ownership = TimelineBadgePresentationResponse(
        label="Managed" if control_path == "managed" else "Unmanaged",
        tone="neutral",
    )
    status = _timeline_status_from_liveness_facts(runtime_facts)
    return TimelineCardPresentationResponse(
        ownership=ownership,
        status=status,
        border_tone=status.tone if status is not None else "inactive",
    )


def _closed_timeline_status_label(terminal_reason: str | None) -> str:
    if terminal_reason == TERMINAL_DISCONNECTED_REASON:
        return "Terminal disconnected"
    return "Closed"


def _timeline_status_from_liveness_facts(runtime_facts: SessionLivenessFactsResponse | None) -> TimelineStatusPresentationResponse:
    if runtime_facts is None:
        return TimelineStatusPresentationResponse(label="Unknown", tone="inactive", seen_at=None, seen_at_prefix="Checked")

    process_state = str(runtime_facts.process_state or "").strip()
    lifecycle = runtime_facts.lifecycle
    if process_state == "closed" or lifecycle.state == "closed":
        return TimelineStatusPresentationResponse(
            label=_closed_timeline_status_label(lifecycle.reason),
            tone="closed",
            seen_at=lifecycle.observed_at or runtime_facts.phase.observed_at or runtime_facts.activity.last_transcript_at,
            seen_at_prefix="Closed",
        )

    phase = runtime_facts.phase
    phase_kind = str(phase.kind or "").strip()
    if phase_kind:
        return TimelineStatusPresentationResponse(
            label=_phase_status_label(phase_kind, phase.tool),
            tone=_phase_tone(phase_kind),
            seen_at=phase.observed_at,
            seen_at_prefix="Updated",
        )

    if process_state == "running":
        process = runtime_facts.process
        return TimelineStatusPresentationResponse(
            label="Running",
            tone="inactive",
            seen_at=process.observed_at or process.last_seen_at,
            seen_at_prefix="Verified",
        )

    return TimelineStatusPresentationResponse(
        label="Unknown",
        tone="inactive",
        seen_at=None,
        seen_at_prefix="Checked",
    )


def _phase_status_label(kind: str, tool_name: str | None) -> str:
    phase = "idle" if kind == "needs_user" else kind.replace("_", " ").replace("-", " ")
    compact_tool = compact_runtime_tool_label(tool_name)
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
    capability_flags=None,
) -> SessionControlResponse | None:
    if session is None:
        return None
    capability_flags = capability_flags or build_session_capabilities(session)
    source_runner_name = str(getattr(session, "source_runner_name", "") or "").strip() or None
    attach_command = build_attach_command(session) if capability_flags.host_reattach_available else None
    if (
        capability_flags.managed_transport is None
        and getattr(session, "source_runner_id", None) is None
        and source_runner_name is None
        and attach_command is None
    ):
        return None
    return SessionControlResponse(
        managed_transport=capability_flags.managed_transport,
        source_runner_id=getattr(session, "source_runner_id", None),
        source_runner_name=source_runner_name,
        attach_command=attach_command,
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionControlResponse(BaseModel):
    managed_transport: Optional[ManagedSessionTransport] = Field(
        None,
        description="Managed transport when Longhouse owns the session runtime",
    )
    source_runner_id: Optional[int] = Field(None, description="Runner id for managed local sessions")
    source_runner_name: Optional[str] = Field(None, description="Runner name for managed local sessions")
    attach_command: Optional[str] = Field(None, description="Local reattach command for managed-local sessions")


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


class SessionRuntimeDisplayResponse(BaseModel):
    truth_tier: str = Field(..., description="Runtime truth tier: none|stale|fresh|managed-local")
    signal_tier: str = Field(
        "none",
        description="Strongest source signal tier: phase_signal|process_binding|transcript_progress|none",
    )
    state: Optional[str] = Field(None, description="Canonical presence state when known")
    tone: str = Field(..., description="Stable visual tone for clients")
    headline: str = Field(..., description="Primary user-facing runtime label")
    detail: Optional[str] = Field(None, description="Secondary user-facing runtime label")
    phase_label: str = Field(..., description="Compact phase label for cards and strips")
    compact_tool_label: Optional[str] = Field(None, description="Normalized tool label for display")
    is_live: bool = Field(False, description="True when the session is actively executing")
    is_executing: bool = Field(False, description="True when the agent is thinking or running a tool")
    needs_attention: bool = Field(False, description="True when the user should respond or approve")
    is_idle: bool = Field(False, description="True when the runtime is waiting for another turn")
    is_stalled: bool = Field(False, description="True when a provider explicitly reports stalled state")
    is_managed_local_truth: bool = Field(False, description="True when runtime truth is from a managed-local control path")
    has_signal: bool = Field(False, description="True when clients should render runtime state")
    control_path: str = Field(
        "unmanaged",
        description="Does Longhouse own a control path? 'managed' or 'unmanaged'",
    )
    activity_recency: str = Field(
        "none",
        description="How recently we heard from this session: 'live' | 'recent' | 'stale' | 'none'",
    )
    lifecycle: str = Field(
        "open",
        description="Session lifecycle: 'open' | 'closed' | 'unknown'. Closed only with ground truth.",
    )
    host_state: str = Field(
        "unknown",
        description="Host/machine verifiability: 'online' | 'stale' | 'offline' | 'unknown'",
    )
    terminal_reason: Optional[str] = Field(
        None,
        description="Why the session is closed, when lifecycle=='closed'",
    )


class SessionLiveTranscriptResponse(UTCBaseModel):
    text: str = Field(..., description="Latest live transcript text snapshot from a managed provider bridge")
    source: str = Field(..., description="Runtime source for the live transcript overlay")
    received_at: datetime = Field(..., description="When the Runtime Host received this live text snapshot")
    occurred_at: Optional[datetime] = Field(None, description="When the bridge observed this live text snapshot")
    thread_id: Optional[str] = Field(None, description="Provider thread id for the live text snapshot")
    turn_id: Optional[str] = Field(None, description="Provider turn id for the live text snapshot")
    seq: Optional[int] = Field(None, description="Monotonic sequence within the provider turn")
    method: Optional[str] = Field(None, description="Provider notification method that produced the snapshot")
    is_complete: bool = Field(False, description="True when this snapshot is the final live text for the turn")


class HostObservationResponse(UTCBaseModel):
    state: str = Field("unknown", description="Observed host state: online|stale|offline|unknown")
    last_seen_at: Optional[datetime] = Field(None, description="When the host last heartbeated, when known")
    source: Optional[str] = Field(None, description="Observation source, e.g. machine_heartbeat")


class ProcessObservationResponse(UTCBaseModel):
    status: str = Field("unknown", description="Observed process state: observed|not_observed|unknown")
    pid: Optional[int] = Field(None, description="Observed process id, when known")
    process_start_time: Optional[datetime] = Field(None, description="Observed process start time, when known")
    observed_at: Optional[datetime] = Field(None, description="When this process binding was observed")
    last_seen_at: Optional[datetime] = Field(None, description="Server time when this binding was last reported")
    source_mtime: Optional[datetime] = Field(None, description="Transcript mtime seen with the process observation")
    source_path: Optional[str] = Field(None, description="Transcript path tied to this observation")
    reason: Optional[str] = Field(None, description="Why the status is not observed or unknown")
    source: Optional[str] = Field(None, description="Observation source, e.g. machine_process_scan")


class PhaseObservationResponse(UTCBaseModel):
    kind: Optional[str] = Field(None, description="Observed phase kind, when a semantic phase signal exists")
    tool: Optional[str] = Field(None, description="Observed active tool for the phase, when known")
    source: Optional[str] = Field(None, description="Phase observation source")
    observed_at: Optional[datetime] = Field(None, description="When the phase was observed")
    expires_at: Optional[datetime] = Field(None, description="Producer/debouncer freshness budget, not lifecycle truth")


class ActivityObservationResponse(UTCBaseModel):
    last_transcript_at: Optional[datetime] = Field(None, description="Last transcript event/activity timestamp")
    last_runtime_signal_at: Optional[datetime] = Field(None, description="Last semantic runtime signal timestamp")
    last_progress_at: Optional[datetime] = Field(None, description="Last progress-only signal timestamp")


class LifecycleFactResponse(UTCBaseModel):
    state: str = Field("unknown", description="Observed lifecycle state: open|closed|unknown")
    reason: Optional[str] = Field(None, description="Reason for the lifecycle state when known")
    observed_at: Optional[datetime] = Field(None, description="When the lifecycle fact was observed")


class SessionLivenessFactsResponse(UTCBaseModel):
    """Observed facts only.

    This contract is intentionally orthogonal to ``runtime_display``. Clients
    should render these facts with timestamps/sources, not reconcile them with
    display labels or use them as a second display state machine.
    """

    control_path: str = Field(..., description="Does Longhouse own a control path? managed|unmanaged")
    process_state: Literal["running", "closed", "unknown"] = Field(
        ...,
        description="Observed provider-process state",
    )
    host: HostObservationResponse
    process: ProcessObservationResponse
    phase: PhaseObservationResponse
    activity: ActivityObservationResponse
    lifecycle: LifecycleFactResponse


class TimelineBadgePresentationResponse(UTCBaseModel):
    label: str = Field(..., description="Stable user-facing badge label")
    tone: str = Field(..., description="Stable visual tone token for clients")


class TimelineStatusPresentationResponse(TimelineBadgePresentationResponse):
    seen_at: Optional[datetime] = Field(None, description="Signal timestamp for stale status copy")
    seen_at_prefix: str = Field(..., description="Server-owned word that qualifies the status timestamp")


class TimelineCardPresentationResponse(UTCBaseModel):
    ownership: TimelineBadgePresentationResponse = Field(..., description="Managed/unmanaged badge")
    status: Optional[TimelineStatusPresentationResponse] = Field(None, description="Primary timeline status badge")
    border_tone: str = Field("inactive", description="Stable tone token for the card edge/outline")


class SessionResponse(UTCBaseModel):
    """Response for a single session."""

    id: str = Field(..., description="Session UUID")
    provider: str = Field(..., description="AI provider")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device ID")
    environment: Optional[str] = Field(None, description="Environment (production, development, test, e2e, commis)")
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
    summary_title: Optional[str] = Field(None, description="Short session title")
    first_user_message: Optional[str] = Field(None, description="First user message (truncated)")
    match_event_id: Optional[int] = Field(None, description="Matching event id for search queries")
    match_snippet: Optional[str] = Field(None, description="Snippet of matching content")
    match_role: Optional[str] = Field(None, description="Role for matching event")
    match_score: Optional[float] = Field(None, description="Semantic similarity score (0-1) when result is from vector search")
    thread_root_session_id: str = Field(..., description="Logical thread root session UUID")
    thread_head_session_id: str = Field(..., description="Current writable head session UUID")
    thread_continuation_count: int = Field(..., description="Number of concrete continuations in this logical thread")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind: local|cloud|runner")
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label")
    home_label: Optional[str] = Field(None, description="User-facing home label, e.g. On this Mac|Hosted|Moved to cloud")
    branched_from_event_id: Optional[int] = Field(None, description="Event id where this continuation branched")
    is_writable_head: bool = Field(False, description="True when this session is the current writable head")
    is_sidechain: bool = Field(False, description="True when session is a Task sub-agent (not human-initiated)")
    control: Optional[SessionControlResponse] = Field(None, description="Host-control and managed-launch debugging detail")
    capabilities: SessionCapabilitiesResponse = Field(..., description="Canonical session capability flags")
    runtime_display: Optional[SessionRuntimeDisplayResponse] = Field(None, description="Server-derived display state for clients")
    runtime_facts: Optional[SessionLivenessFactsResponse] = Field(None, description="Observed liveness facts with timestamps and sources")
    live_transcript: Optional[SessionLiveTranscriptResponse] = Field(
        None,
        description="Low-latency managed bridge transcript overlay; durable events remain canonical",
    )
    timeline_card: TimelineCardPresentationResponse = Field(..., description="Server-derived timeline-card presentation")
    loop_mode: SessionLoopMode = Field(SessionLoopMode.ASSIST, description="Session loop mode: assist|autopilot")
    user_state: str = Field("active", description="User classification: active|parked|snoozed|archived")


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
        description="True if any non-demo sessions exist (device_id != 'demo-mac'). False means only demo-seeded data is present.",
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
    runtime_display: Optional[SessionRuntimeDisplayResponse] = Field(None, description="Server-derived display state for clients")
    runtime_facts: Optional[SessionLivenessFactsResponse] = Field(None, description="Observed liveness facts with timestamps and sources")
    live_transcript: Optional[SessionLiveTranscriptResponse] = Field(
        None,
        description="Low-latency managed bridge transcript overlay; durable events remain canonical",
    )
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
    pending_inbound_messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0


class WallResponse(UTCBaseModel):
    """Wall query response — sessions indexed by raw signal."""

    sessions: List[WallSessionResponse]
    total: int


class EventResponse(UTCBaseModel):
    """Response for a single event."""

    id: int = Field(..., description="Event ID")
    role: str = Field(..., description="Message role")
    content_text: Optional[str] = Field(None, description="Message content")
    tool_name: Optional[str] = Field(None, description="Tool name")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool input")
    tool_output_text: Optional[str] = Field(None, description="Tool output")
    tool_call_id: Optional[str] = Field(None, description="Cross-provider call/result linkage ID")
    timestamp: datetime = Field(..., description="Event timestamp")
    in_active_context: bool = Field(
        True,
        description="True when event is inside the current active model context boundary",
    )
    branch_id: Optional[int] = Field(None, description="Session branch ID for rewind-aware projections")
    is_head_branch: bool = Field(True, description="True when event belongs to the active head branch")


class EventsListResponse(BaseModel):
    """Response for events list."""

    events: List[EventResponse]
    total: int
    branch_mode: str = Field("head", description="Branch projection mode: head|all")
    abandoned_events: int = Field(0, description="Events excluded from head projection due to rewind branches")


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
    state: str = Field(..., description="created|send_accepted|active|terminal|durable|failed")
    terminal_phase: Optional[str] = Field(None, description="Observed terminal phase when known")
    error_code: Optional[str] = Field(None, description="Canonical irrecoverable error code when failed")
    user_event_id: Optional[int] = Field(None, description="Triggering durable user event id")
    durable_assistant_event_id: Optional[int] = Field(None, description="Durable assistant event id that closed the turn")
    baseline_event_id: Optional[int] = Field(None, description="Latest durable event id observed before the turn began")
    baseline_runtime_cursor: Optional[int] = Field(None, description="Latest runtime cursor observed before the turn began")
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

    kind: str = Field(..., description="Projection item kind: event|seam")
    session_id: str = Field(..., description="Concrete session UUID for this item")
    timestamp: datetime = Field(..., description="Timestamp used for item ordering and display")
    event: Optional[EventResponse] = Field(None, description="Present when kind=event")
    continued_from_session_id: Optional[str] = Field(None, description="Parent continuation session UUID for seams")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind for seam items")
    origin_label: Optional[str] = Field(None, description="Origin label for seam items")
    parent_origin_label: Optional[str] = Field(None, description="Origin label for the parent segment")
    parent_continuation_kind: Optional[str] = Field(None, description="Continuation kind for the parent segment")
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


class SessionWorkspaceResponse(BaseModel):
    """Response for the primary session workspace bootstrap payload."""

    session: SessionResponse = Field(..., description="Focused session metadata")
    thread: SessionThreadResponse = Field(..., description="Logical thread continuations for the focused session")
    projection: SessionProjectionResponse = Field(..., description="First page of the stitched lineage projection")


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


class IngestHealthResponse(UTCBaseModel):
    status: str  # "ok" | "stale" | "unknown"
    last_session_at: Optional[datetime] = None
    gap_hours: Optional[float] = None
    threshold_hours: float
    session_count: int


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
    root_id = str(session.thread_root_session_id or session.id)
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
    live_transcript_overlay: SessionLiveTranscriptOverlay | None = None,
    include_live_transcript: bool = False,
) -> SessionResponse:
    cache = thread_cache if thread_cache is not None else {}
    thread_head_session_id, thread_continuation_count = get_thread_meta(store, session, cache)
    include_runtime = should_include_runtime_view(session=session, runtime_view=runtime_overlay)
    capability_flags = build_session_capabilities(session)
    binding_host_state = None
    binding_terminal_reason = None
    if binding_overlay is not None:
        binding_host_state = binding_overlay.host_state
        binding_terminal_reason = binding_overlay.terminal_reason
    if capability_flags.live_control_available or capability_flags.host_reattach_available:
        binding_host_state = managed_runner_host_state(store.db, session) or binding_host_state
    runtime_display = (
        build_session_runtime_display_response(
            runtime_overlay=runtime_overlay,
            capability_flags=capability_flags,
            ended_at=session.ended_at,
            binding_host_state=binding_host_state,
            binding_terminal_reason=binding_terminal_reason,
        )
        if include_runtime
        else None
    )
    runtime_facts = (
        build_session_liveness_facts_response(
            runtime_overlay=runtime_overlay,
            capability_flags=capability_flags,
            last_activity_at=last_activity_at,
            binding_overlay=binding_overlay,
            binding_host_state=binding_host_state,
            binding_terminal_reason=binding_terminal_reason,
        )
        if runtime_overlay is not None
        else None
    )
    effective_capability_flags = project_current_session_capabilities_from_facts(
        capability_flags,
        liveness_facts=runtime_facts,
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
        first_user_message=first_user_message,
        match_event_id=match_event_id,
        match_snippet=match_snippet,
        match_role=match_role,
        match_score=match_score,
        thread_root_session_id=str(session.thread_root_session_id or session.id),
        thread_head_session_id=thread_head_session_id,
        thread_continuation_count=thread_continuation_count,
        continued_from_session_id=(str(session.continued_from_session_id) if session.continued_from_session_id else None),
        continuation_kind=session.continuation_kind,
        origin_label=session.origin_label,
        home_label=capability_flags.home_label,
        branched_from_event_id=session.branched_from_event_id,
        is_writable_head=bool(session.is_writable_head),
        is_sidechain=bool(session.is_sidechain or False),
        control=build_session_control_response(session, capability_flags=effective_capability_flags),
        capabilities=build_session_capabilities_response(
            session=session,
            capability_flags=capability_flags,
            runtime_display=runtime_display,
            runtime_facts=runtime_facts,
        ),
        runtime_display=runtime_display,
        runtime_facts=runtime_facts,
        live_transcript=(
            build_live_transcript_response(
                live_transcript_overlay,
                last_activity_at=last_activity_at,
            )
            if include_live_transcript
            else None
        ),
        timeline_card=build_session_timeline_card_response(
            runtime_facts=runtime_facts,
            capability_flags=capability_flags,
        ),
        loop_mode=_coerce_session_loop_mode(getattr(session, "loop_mode", None)),
        user_state=session.user_state or "active",
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
    live_transcript_overlay: SessionLiveTranscriptOverlay | None = None,
) -> ActiveSessionResponse:
    capability_flags = build_session_capabilities(session)
    _started = (
        session.started_at.replace(tzinfo=timezone.utc) if session.started_at and session.started_at.tzinfo is None else session.started_at
    )
    _ended = session.ended_at.replace(tzinfo=timezone.utc) if session.ended_at and session.ended_at.tzinfo is None else session.ended_at
    end_time = _ended or now
    duration_minutes = int((end_time - _started).total_seconds() / 60) if _started else 0
    message_count = (session.user_messages or 0) + (session.assistant_messages or 0)
    binding_host_state = binding_overlay.host_state if binding_overlay is not None else None
    binding_terminal_reason = binding_overlay.terminal_reason if binding_overlay is not None else None
    if capability_flags.live_control_available or capability_flags.host_reattach_available:
        binding_host_state = managed_runner_host_state(store.db, session) or binding_host_state
    runtime_display = build_session_runtime_display_response(
        runtime_overlay=runtime_overlay,
        capability_flags=capability_flags,
        ended_at=session.ended_at,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    runtime_facts = build_session_liveness_facts_response(
        runtime_overlay=runtime_overlay,
        capability_flags=capability_flags,
        last_activity_at=last_activity_at,
        binding_overlay=binding_overlay,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    effective_capability_flags = project_current_session_capabilities_from_facts(
        capability_flags,
        liveness_facts=runtime_facts,
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
        user_state=session.user_state or "active",
        home_label=capability_flags.home_label,
        control=build_session_control_response(session, capability_flags=effective_capability_flags),
        capabilities=build_session_capabilities_response(
            session=session,
            capability_flags=capability_flags,
            runtime_display=runtime_display,
            runtime_facts=runtime_facts,
        ),
        runtime_display=runtime_display,
        runtime_facts=runtime_facts,
        live_transcript=build_live_transcript_response(
            live_transcript_overlay,
            last_activity_at=last_activity_at,
        ),
        loop_mode=_coerce_session_loop_mode(getattr(session, "loop_mode", None)),
    )


def build_event_response(
    store: AgentsStore,
    event: AgentEvent,
    *,
    boundary: int | None,
    head_branch_id: int | None,
) -> EventResponse:
    return EventResponse(
        id=event.id,
        role=event.role,
        content_text=event.content_text,
        tool_name=event.tool_name,
        tool_input_json=event.tool_input_json,
        tool_output_text=event.tool_output_text,
        tool_call_id=event.tool_call_id,
        timestamp=event.timestamp,
        in_active_context=store.is_event_in_active_context(event, boundary) if boundary is not None else True,
        branch_id=event.branch_id,
        is_head_branch=(head_branch_id is None or event.branch_id in {None, head_branch_id}),
    )


def build_session_turn_response(turn: SessionTurn) -> SessionTurnResponse:
    timing = build_session_turn_timing_response(turn)
    return SessionTurnResponse(
        id=int(turn.id),
        session_id=str(turn.session_id),
        request_id=turn.request_id,
        state=turn.state,
        terminal_phase=turn.terminal_phase,
        error_code=turn.error_code,
        user_event_id=turn.user_event_id,
        durable_assistant_event_id=turn.durable_assistant_event_id,
        baseline_event_id=turn.baseline_event_id,
        baseline_runtime_cursor=turn.baseline_runtime_cursor,
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
