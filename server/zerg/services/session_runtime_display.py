"""Derived runtime display contract for human clients.

Raw runtime truth lives in ``SessionRuntimeState`` and is materialized as a
``SessionRuntimeView``. This module turns that truth plus capabilities into the
small presentation contract consumed by web and iOS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_runtime import SessionRuntimeView
from zerg.session_execution_home import SessionExecutionHome

KNOWN_PRESENCE_STATES = {"thinking", "running", "idle", "needs_user", "blocked", "stalled"}
LIVE_EXECUTION_STATES = {"thinking", "running"}
ATTENTION_STATES = {"needs_user", "blocked"}
LEGACY_PROGRESS_STATUSES = {"working", "active"}


@dataclass(frozen=True)
class SessionRuntimeDisplay:
    truth_tier: str
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
    heuristic_active: bool
    is_managed_local_truth: bool
    has_signal: bool
    # Phase 2 of session-liveness-honesty: three orthogonal axes that
    # clients render verbatim. See docs/specs/session-liveness-honesty.md.
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


def _is_legacy_progress_status(status: str | None) -> bool:
    return status in LEGACY_PROGRESS_STATUSES


def _is_progress_fallback(
    *,
    status: str | None,
    confidence: str | None,
    runtime_source: str | None,
    presence_state: str | None,
) -> bool:
    if presence_state is not None:
        return False
    return confidence == "inferred" or runtime_source == "progress" or _is_legacy_progress_status(status)


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
    capabilities: SessionCapabilityFlags,
    status: str | None,
    confidence: str | None,
    runtime_source: str | None,
    presence_state: str | None,
) -> str:
    has_fresh_signal = _has_fresh_signal(
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    )
    if capabilities.host_reattach_available and has_fresh_signal and confidence != "stale":
        return "managed-local"
    if has_fresh_signal and confidence != "stale":
        return "fresh"
    if _is_progress_fallback(
        status=status,
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    ):
        return "inferred"
    if confidence == "stale" or runtime_source == "fallback":
        return "stale"
    return "none"


def _has_renderable_signal(
    *,
    truth_tier: str,
    runtime_source: str | None,
    presence_state: str | None,
    heuristic_active: bool,
    last_live_at: datetime | None,
) -> bool:
    if presence_state is not None or heuristic_active or last_live_at is not None:
        return True
    if truth_tier in {"fresh", "managed-local", "inferred"}:
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
    if presence_state == "running" and compact_tool:
        return f"Running {compact_tool}"
    if presence_state == "blocked" and compact_tool:
        return f"Blocked on {compact_tool}"
    return (display_phase or "").strip() or "Recent"


def _tone(
    *,
    presence_state: str | None,
    heuristic_active: bool,
    is_idle: bool,
) -> str:
    if presence_state == "stalled":
        return "stalled"
    if presence_state == "blocked":
        return "blocked"
    if presence_state == "needs_user":
        return "needs-user"
    if presence_state == "running":
        return "running"
    if presence_state == "thinking":
        return "thinking"
    if heuristic_active:
        return "inferred"
    if is_idle:
        return "idle"
    return "inactive"


def _outcome_label(
    *,
    is_executing: bool,
    needs_attention: bool,
    heuristic_active: bool,
    status: str | None,
    terminal_state: str | None,
) -> str:
    # Phase 1 of session-liveness-honesty: do not collapse on `ended_at`
    # alone. Only explicit terminal_state (from real terminal_signal ingest)
    # or runtime-view status=="completed" (which in fallback now requires
    # terminal_state too) means Completed.
    if is_executing or needs_attention or heuristic_active:
        return "Active"
    if terminal_state or status == "completed":
        return "Completed"
    return "Inactive"


def _managed_copy(
    *,
    presence_state: str | None,
    phase_label: str,
    compact_tool: str | None,
    truth_tier: str,
    heuristic_active: bool,
    is_idle: bool,
) -> tuple[str, str | None]:
    if presence_state == "thinking":
        return "Working", "Thinking"
    if presence_state == "running":
        return "Working", f"Running {compact_tool}" if compact_tool else phase_label
    if presence_state == "needs_user":
        return "Waiting for you", "Reply needed"
    if presence_state == "blocked":
        return "Waiting for you", f"Approval needed • {compact_tool}" if compact_tool else "Approval needed"
    if presence_state is None and truth_tier != "managed-local":
        if heuristic_active:
            return "Active", "Last known activity"
        return "Not connected", None
    if presence_state is None and heuristic_active:
        return "Working", phase_label
    if presence_state is None and truth_tier == "managed-local":
        return "Not connected", None
    if presence_state == "idle" or is_idle:
        return "Ready", "Ready for next prompt"
    return "Ready", "Ready for next prompt"


def build_session_runtime_display(
    *,
    runtime_view: SessionRuntimeView,
    capabilities: SessionCapabilityFlags,
    ended_at: datetime | None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
) -> SessionRuntimeDisplay:
    status = runtime_view.status
    confidence = runtime_view.confidence
    runtime_source = _normalize_source(runtime_view.runtime_source)
    runtime_phase = _normalize_presence_state(runtime_view.runtime_phase)
    presence_state = _normalize_presence_state(runtime_view.presence_state)
    tool_name = runtime_view.active_tool or runtime_view.presence_tool
    compact_tool = compact_runtime_tool_label(tool_name)
    truth_tier = _truth_tier(
        capabilities=capabilities,
        status=status,
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    )
    heuristic_active = _is_progress_fallback(
        status=status,
        confidence=confidence,
        runtime_source=runtime_source,
        presence_state=presence_state,
    )
    is_executing = presence_state in LIVE_EXECUTION_STATES
    needs_attention = presence_state in ATTENTION_STATES
    is_idle = presence_state == "idle" or (not is_executing and not needs_attention and not heuristic_active and status == "idle")
    phase_label = _phase_label(
        presence_state=presence_state,
        display_phase=runtime_view.display_phase,
        compact_tool=compact_tool,
    )
    is_managed_session = (
        capabilities.live_control_available or capabilities.host_reattach_available or capabilities.reply_to_live_session_available
    )
    if is_managed_session:
        headline, detail = _managed_copy(
            presence_state=presence_state,
            phase_label=phase_label,
            compact_tool=compact_tool,
            truth_tier=truth_tier,
            heuristic_active=heuristic_active,
            is_idle=is_idle,
        )
    else:
        headline = _outcome_label(
            is_executing=is_executing,
            needs_attention=needs_attention,
            heuristic_active=heuristic_active,
            status=status,
            terminal_state=runtime_view.terminal_state,
        )
        detail = None

    has_signal = _has_renderable_signal(
        truth_tier=truth_tier,
        runtime_source=runtime_source,
        presence_state=presence_state,
        heuristic_active=heuristic_active,
        last_live_at=runtime_view.last_live_at,
    )

    # Phase 2 of session-liveness-honesty: three-axis projection.
    terminal_state = runtime_view.terminal_state
    control_path = _derive_control_path(capabilities)
    is_stalled = (
        control_path == "managed"
        and terminal_state is None
        and runtime_phase in LIVE_EXECUTION_STATES
        and confidence == "stale"
        and not (tool_name or "").strip()
    )
    if is_stalled:
        presence_state = "stalled"
        phase_label = "Stalled"
        headline = "Stalled"
        detail = "No recent managed-session progress"
        is_executing = False
        needs_attention = True
        is_idle = False
    activity_recency = _derive_activity_recency(
        presence_state=presence_state,
        confidence=confidence,
        runtime_source=runtime_source,
        heuristic_active=heuristic_active,
        has_signal=has_signal,
    )
    # Phase 6: machine-agent observed unmanaged binding terminal reasons
    # promote lifecycle to closed without an explicit provider terminal_signal.
    # `process_gone` is confirmed local process disappearance; `host_expired`
    # is a long-unverified host cleanup and must stay distinguishable.
    binding_closed = binding_terminal_reason in {"process_gone", "host_expired"} and control_path == "unmanaged"
    if terminal_state:
        lifecycle = "closed"
        terminal_reason = _derive_terminal_reason(terminal_state)
    elif binding_closed:
        lifecycle = "closed"
        terminal_reason = binding_terminal_reason
    else:
        lifecycle = "open"
        terminal_reason = None
    if lifecycle == "closed":
        presence_state = None
        headline = "Completed"
        detail = None
        phase_label = "Completed"
        is_executing = False
        needs_attention = False
        heuristic_active = False
        is_stalled = False
    # Phase 5c: host_state comes from heartbeat+binding freshness when a
    # binding overlay is supplied. Otherwise we honestly say "unknown".
    host_state = binding_host_state if binding_host_state else "unknown"

    return SessionRuntimeDisplay(
        truth_tier=truth_tier,
        state=presence_state,
        tone=_tone(
            presence_state=presence_state,
            heuristic_active=heuristic_active,
            is_idle=is_idle,
        ),
        headline=headline,
        detail=detail,
        phase_label=phase_label,
        compact_tool_label=compact_tool,
        is_live=is_executing,
        is_executing=is_executing,
        needs_attention=needs_attention,
        is_idle=is_idle,
        is_stalled=is_stalled,
        heuristic_active=heuristic_active,
        is_managed_local_truth=truth_tier == "managed-local",
        has_signal=has_signal,
        control_path=control_path,
        activity_recency=activity_recency,
        lifecycle=lifecycle,
        host_state=host_state,
        terminal_reason=terminal_reason,
    )


_MANAGED_EXECUTION_HOMES = {
    SessionExecutionHome.MANAGED_LOCAL,
    SessionExecutionHome.MANAGED_HOSTED,
    SessionExecutionHome.CLOUD_TAKEOVER,  # legacy managed shape; no new sessions
}


def _derive_control_path(capabilities: SessionCapabilityFlags) -> str:
    """Durable: does Longhouse own a control path for this session?

    Based on execution_home / managed_transport, NOT on whether control is
    currently live. A managed session whose bridge is offline is still
    `managed`, with `host_state=offline` telling the story separately.
    """
    if capabilities.execution_home in _MANAGED_EXECUTION_HOMES:
        return "managed"
    if capabilities.managed_transport is not None:
        return "managed"
    return "unmanaged"


def _derive_activity_recency(
    *,
    presence_state: str | None,
    confidence: str | None,
    runtime_source: str | None,
    heuristic_active: bool,
    has_signal: bool,
) -> str:
    """How recently did we hear something real from this session?

    - `live`: presence signal within its phase freshness window
    - `recent`: inferred/progress within ~5 min
    - `stale`: had signal once, nothing fresh
    - `none`: never observed activity
    """
    if presence_state is not None and confidence == "live":
        return "live"
    if heuristic_active or confidence == "inferred":
        return "recent"
    if has_signal:
        return "stale"
    if confidence == "stale":
        return "stale"
    # "Had signal once" means we have a real progress timestamp. The
    # fallback view sets last_progress_at to a session's started_at when no
    # activity was ever observed, so require something renderable alongside
    # the timestamp before promoting to "stale".
    return "none"


def _derive_terminal_reason(terminal_state: str | None) -> str | None:
    """Normalize the runtime-state terminal_state into a user-facing reason."""
    if not terminal_state:
        return None
    normalized = terminal_state.strip().lower()
    if not normalized:
        return None
    # The reducer stores values like "session_ended", "finished", etc. Today
    # every closure comes from an explicit terminal_signal — classify all of
    # them as provider_signal until process-gone detection lands (Phase 6).
    return "provider_signal"
