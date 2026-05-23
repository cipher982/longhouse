"""Derived runtime display contract for human clients.

Raw runtime truth lives in ``SessionRuntimeState`` and is materialized as a
``SessionRuntimeView``. This module turns that truth plus capabilities into the
small presentation contract consumed by web and iOS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_runtime import SessionRuntimeView
from zerg.utils.time import normalize_utc

KNOWN_PRESENCE_STATES = {"thinking", "running", "idle", "needs_user", "blocked", "stalled"}
LIVE_EXECUTION_STATES = {"thinking", "running"}
ATTENTION_STATES = {"blocked"}
TRANSCRIPT_SYNC_DISPLAY_WINDOW = timedelta(seconds=30)
TRANSCRIPT_SYNC_STATE = "syncing_transcript"


@dataclass(frozen=True)
class SessionRuntimeDisplay:
    truth_tier: str
    signal_tier: str
    state: str | None
    tone: str
    headline: str
    detail: str | None
    phase_label: str
    compact_tool_label: str | None
    is_live: bool
    is_executing: bool
    needs_attention: bool
    is_idle: bool
    is_stalled: bool
    is_managed_local_truth: bool
    has_signal: bool
    control_path: str  # "managed" | "unmanaged"
    activity_recency: str  # "live" | "recent" | "stale" | "none"
    lifecycle: str  # "open" | "closed" | "unknown"
    host_state: str  # "online" | "stale" | "offline" | "unknown"
    terminal_reason: str | None  # populated when lifecycle == "closed"


def _normalize_presence_state(state: str | None) -> str | None:
    return state if state in KNOWN_PRESENCE_STATES else None


def _normalize_source(source: str | None) -> str | None:
    source = (source or "").strip()
    return source or None


def _has_fresh_signal(
    *,
    confidence: str | None,
    runtime_source: str | None,
    presence_state: str | None,
) -> bool:
    return (
        presence_state is not None
        or (confidence == "live" and runtime_source not in {"progress", "fallback"})
        or runtime_source in {"semantic", "managed_local_transport"}
    )


def _truth_tier(
    *,
    capabilities: KernelSessionCapabilities,
    confidence: str | None,
    runtime_source: str | None,
    presence_state: str | None,
) -> str:
    has_fresh_signal = _has_fresh_signal(
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    )
    is_managed = capabilities.live_control_available or capabilities.host_reattach_available
    if is_managed and has_fresh_signal and confidence != "stale":
        return "managed-local"
    if has_fresh_signal and confidence != "stale":
        return "fresh"
    if confidence == "stale" or runtime_source == "fallback":
        return "stale"
    return "none"


def _has_renderable_signal(
    *,
    truth_tier: str,
    runtime_source: str | None,
    presence_state: str | None,
    process_observed: bool,
    last_live_at: datetime | None,
) -> bool:
    if presence_state is not None or process_observed or last_live_at is not None:
        return True
    if truth_tier in {"fresh", "managed-local"}:
        return True
    return truth_tier == "stale" and runtime_source != "fallback"


def _title_case_words(value: str) -> str:
    words = [word for word in value.split() if word]
    out: list[str] = []
    for word in words:
        if len(word) <= 3 and word == word.upper():
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def compact_runtime_tool_label(tool_name: str | None) -> str | None:
    raw = (tool_name or "").strip()
    if not raw:
        return None

    canonical = raw.split("__")[-1]
    canonical = re.sub(r"^(hatch_|tool_|mcp_)", "", canonical)
    normalized = re.sub(r"[-_.]+", " ", canonical).strip()
    if not normalized:
        return None

    lower = normalized.lower()
    if lower == "codex":
        return "Codex"
    if lower == "claude":
        return "Claude"
    if lower == "gemini":
        return "Gemini"
    if lower == "antigravity":
        return "Antigravity"
    if lower == "default":
        return "Z.ai"
    if lower in {"shell", "bash", "terminal"}:
        return "Shell"
    if lower in {"edit", "write", "patch", "apply patch", "file change", "filechange"}:
        return "Edit"
    return _title_case_words(normalized)


def _phase_label(
    *,
    presence_state: str | None,
    display_phase: str | None,
    compact_tool: str | None,
) -> str:
    if presence_state == "needs_user":
        return "Idle"
    if presence_state == "running" and compact_tool:
        return f"Using {compact_tool}"
    if presence_state == "blocked" and compact_tool:
        return f"Blocked on {compact_tool}"
    return (display_phase or "").strip() or "Inactive"


def _tone(
    *,
    presence_state: str | None,
    process_observed: bool,
    is_idle: bool,
) -> str:
    if presence_state == "stalled":
        return "stalled"
    if presence_state == "blocked":
        return "blocked"
    if presence_state == "needs_user":
        return "idle"
    if presence_state == "running":
        return "running"
    if presence_state == "thinking":
        return "thinking"
    if process_observed:
        return "active"
    if is_idle:
        return "idle"
    return "inactive"


def _outcome_label(
    *,
    presence_state: str | None,
    is_executing: bool,
    needs_attention: bool,
    process_observed: bool,
    status: str | None,
    terminal_state: str | None,
) -> str:
    if is_executing or needs_attention or process_observed:
        return "Active"
    if presence_state in {"idle", "needs_user"}:
        return "Idle"
    if terminal_state or status == "completed":
        return "Completed"
    return "Inactive"


def _managed_copy(
    *,
    presence_state: str | None,
    compact_tool: str | None,
) -> tuple[str, str | None]:
    if presence_state == "thinking":
        return "Working", "Thinking"
    if presence_state == "running":
        return "Working", f"Using {compact_tool}" if compact_tool else "Running"
    if presence_state == "needs_user":
        return "Idle", "Waiting for next prompt"
    if presence_state == "blocked":
        return "Needs permission", f"Approval needed • {compact_tool}" if compact_tool else "Approval needed"
    if presence_state is None:
        return "Not connected", None
    return "Idle", "Waiting for next prompt"


def build_session_runtime_display(
    *,
    runtime_view: SessionRuntimeView,
    capabilities: KernelSessionCapabilities,
    ended_at: datetime | None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
    last_activity_at: datetime | None = None,
    user_messages: int | None = None,
    assistant_messages: int | None = None,
    has_visible_transcript_preview: bool = False,
    has_pending_response_turn: bool = False,
    now: datetime | None = None,
) -> SessionRuntimeDisplay:
    status = runtime_view.status
    confidence = runtime_view.confidence
    runtime_source = _normalize_source(runtime_view.runtime_source)
    presence_state = _normalize_presence_state(runtime_view.presence_state)
    if confidence != "live":
        presence_state = None
    tool_name = runtime_view.active_tool or runtime_view.presence_tool
    compact_tool = compact_runtime_tool_label(tool_name)
    control_path = _derive_control_path(capabilities)
    signal_tier = _derive_signal_tier(
        runtime_view=runtime_view,
        control_path=control_path,
        binding_host_state=binding_host_state,
        binding_terminal_reason=binding_terminal_reason,
    )
    host_state = binding_host_state if binding_host_state else "unknown"
    unmanaged_attention_unverified = control_path == "unmanaged" and presence_state in ATTENTION_STATES and host_state != "online"
    if unmanaged_attention_unverified:
        # A bare imported provider transcript often ends with the assistant
        # handing control back to the user. Without an online machine binding,
        # that is not actionable runtime state, so do not promote it to
        # "Needs you".
        presence_state = None
    truth_tier = _truth_tier(
        capabilities=capabilities,
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    )
    process_observed = (
        control_path == "unmanaged"
        and signal_tier == "process_binding"
        and host_state == "online"
        and binding_terminal_reason is None
        and presence_state is None
    )
    is_executing = presence_state in LIVE_EXECUTION_STATES
    needs_attention = presence_state in ATTENTION_STATES
    is_idle = presence_state in {"idle", "needs_user"}
    if unmanaged_attention_unverified:
        is_idle = True
    display_phase = runtime_view.display_phase
    if confidence != "live" and runtime_source not in {"fallback", "progress"} and runtime_view.terminal_state is None:
        display_phase = "Inactive"
    if unmanaged_attention_unverified:
        display_phase = "Inactive"
    phase_label = _phase_label(
        presence_state=presence_state,
        display_phase=display_phase,
        compact_tool=compact_tool,
    )
    if process_observed and presence_state is None:
        phase_label = "Process running"
    is_managed_session = (
        capabilities.live_control_available or capabilities.host_reattach_available or capabilities.reply_to_live_session_available
    )
    if is_managed_session:
        headline, detail = _managed_copy(
            presence_state=presence_state,
            compact_tool=compact_tool,
        )
    else:
        headline = _outcome_label(
            presence_state=presence_state,
            is_executing=is_executing,
            needs_attention=needs_attention,
            process_observed=process_observed,
            status=status,
            terminal_state=runtime_view.terminal_state,
        )
        detail = None

    has_signal = _has_renderable_signal(
        truth_tier=truth_tier,
        runtime_source=runtime_source,
        presence_state=presence_state,
        process_observed=process_observed,
        last_live_at=runtime_view.last_live_at,
    )

    terminal_state = runtime_view.terminal_state
    is_stalled = presence_state == "stalled"
    if is_stalled:
        phase_label = "Stalled"
        headline = "Stalled"
        detail = "Provider reported stalled"
        is_executing = False
        needs_attention = False
        is_idle = False
    activity_recency = _derive_activity_recency(
        presence_state=presence_state,
        confidence=confidence,
        runtime_source=runtime_source,
        process_observed=process_observed,
        has_signal=has_signal,
    )
    binding_closed = binding_terminal_reason == "process_gone" and control_path == "unmanaged"
    if terminal_state:
        lifecycle = "closed"
        terminal_reason = runtime_view.terminal_reason or _derive_terminal_reason(terminal_state)
    elif binding_closed:
        lifecycle = "closed"
        terminal_reason = binding_terminal_reason
    else:
        lifecycle = "open"
        terminal_reason = None
    if lifecycle == "closed":
        presence_state = None
        headline = "Closed"
        detail = None
        phase_label = "Closed"
        is_executing = False
        needs_attention = False
        is_idle = True
        process_observed = False
        is_stalled = False
    transcript_sync_pending = _transcript_sync_pending(
        control_path=control_path,
        lifecycle=lifecycle,
        presence_state=presence_state,
        runtime_view=runtime_view,
        last_activity_at=last_activity_at,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        has_visible_transcript_preview=has_visible_transcript_preview,
        has_pending_response_turn=has_pending_response_turn,
        now=now,
    )
    if transcript_sync_pending:
        presence_state = TRANSCRIPT_SYNC_STATE
        headline = "Syncing"
        detail = "Waiting for transcript"
        phase_label = "Syncing transcript"
        is_executing = False
        needs_attention = False
        is_idle = False
        process_observed = False
        is_stalled = False
    no_runtime_signal = signal_tier == "none" and presence_state is None and not process_observed
    tone = (
        "active"
        if transcript_sync_pending
        else "inactive"
        if lifecycle == "closed"
        or unmanaged_attention_unverified
        or no_runtime_signal
        or (presence_state is None and confidence == "stale")
        else _tone(
            presence_state=presence_state,
            process_observed=process_observed,
            is_idle=is_idle,
        )
    )
    return SessionRuntimeDisplay(
        truth_tier=truth_tier,
        signal_tier=signal_tier,
        state=presence_state,
        tone=tone,
        headline=headline,
        detail=detail,
        phase_label=phase_label,
        compact_tool_label=compact_tool,
        is_live=is_executing,
        is_executing=is_executing,
        needs_attention=needs_attention,
        is_idle=is_idle,
        is_stalled=is_stalled,
        is_managed_local_truth=truth_tier == "managed-local",
        has_signal=has_signal,
        control_path=control_path,
        activity_recency=activity_recency,
        lifecycle=lifecycle,
        host_state=host_state,
        terminal_reason=terminal_reason,
    )


def _derive_control_path(capabilities: KernelSessionCapabilities) -> str:
    """Durable: does Longhouse own a control path for this session?

    Sourced from the kernel-projected capability flags. A managed
    session whose bridge is offline still projects ``host_reattach``,
    so this stays "managed" and ``host_state=offline`` tells the story
    separately. Legacy ``execution_home`` / ``managed_transport`` are
    no longer authoritative.
    """
    if capabilities.live_control_available or capabilities.host_reattach_available:
        return "managed"
    return "unmanaged"


def _derive_signal_tier(
    *,
    runtime_view: SessionRuntimeView,
    control_path: str,
    binding_host_state: str | None,
    binding_terminal_reason: str | None,
) -> str:
    if control_path == "unmanaged" and binding_terminal_reason in {"process_gone", "host_expired"}:
        return "process_binding"
    if control_path == "unmanaged" and binding_host_state == "online":
        return "process_binding"
    tier = (runtime_view.signal_tier or "").strip()
    if tier in {"phase_signal", "transcript_progress", "none"}:
        return tier
    return "none"


def _derive_activity_recency(
    *,
    presence_state: str | None,
    confidence: str | None,
    runtime_source: str | None,
    process_observed: bool,
    has_signal: bool,
) -> str:
    """How recently did we hear something real from this session?

    - `live`: presence signal within its phase freshness window
    - `recent`: current control path signal without a specific live phase
    - `stale`: had signal once, nothing fresh
    - `none`: never observed activity
    """
    if presence_state is not None and confidence == "live":
        return "live"
    if process_observed:
        return "live"
    if has_signal:
        return "stale"
    if confidence == "stale":
        return "stale"
    return "none"


def _transcript_sync_pending(
    *,
    control_path: str,
    lifecycle: str,
    presence_state: str | None,
    runtime_view: SessionRuntimeView,
    last_activity_at: datetime | None,
    user_messages: int | None,
    assistant_messages: int | None,
    has_visible_transcript_preview: bool,
    has_pending_response_turn: bool,
    now: datetime | None,
) -> bool:
    if control_path != "managed" or lifecycle != "open":
        return False
    if presence_state not in {"idle", "needs_user"}:
        return False
    if has_visible_transcript_preview:
        return False
    if not has_pending_response_turn and int(user_messages or 0) <= int(assistant_messages or 0):
        return False

    signal_at = normalize_utc(runtime_view.presence_updated_at) or normalize_utc(runtime_view.last_live_at)
    if signal_at is None:
        return False

    activity_at = normalize_utc(last_activity_at)
    if activity_at is not None and signal_at < activity_at:
        return False

    now_utc = normalize_utc(now) or datetime.now(timezone.utc)
    return now_utc - signal_at <= TRANSCRIPT_SYNC_DISPLAY_WINDOW


def _derive_terminal_reason(terminal_state: str | None) -> str | None:
    """Normalize the runtime-state terminal_state into a user-facing reason."""
    if not terminal_state:
        return None
    normalized = terminal_state.strip().lower()
    if not normalized:
        return None
    if normalized in {"process_gone", "host_expired", "user_closed"}:
        return normalized
    # Provider terminal values such as "session_ended" and "finished" collapse
    # to provider_signal. Machine-derived terminal values stay explicit.
    return "provider_signal"
