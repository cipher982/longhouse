"""Observed session liveness facts.

This module is intentionally not a display mapper. It returns what Longhouse
observed, where the observation came from, and when it happened. Clients can
format the facts, but they should not need to infer lifecycle or capability
truth from presentation labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from zerg.services.session_capabilities import SessionCapabilityFlags
from zerg.services.session_runtime import SessionRuntimeView
from zerg.session_execution_home import SessionExecutionHome


@dataclass(frozen=True)
class HostObservation:
    state: str
    last_seen_at: datetime | None = None
    source: str | None = None


@dataclass(frozen=True)
class ProcessObservation:
    status: str
    pid: int | None = None
    process_start_time: datetime | None = None
    observed_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_mtime: datetime | None = None
    source_path: str | None = None
    reason: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class PhaseObservation:
    kind: str | None
    tool: str | None
    source: str | None
    observed_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class ActivityObservation:
    last_transcript_at: datetime | None
    last_runtime_signal_at: datetime | None
    last_progress_at: datetime | None


@dataclass(frozen=True)
class LifecycleFact:
    state: str
    reason: str | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class SessionLivenessFacts:
    control_path: str
    process_state: str
    host: HostObservation
    process: ProcessObservation
    phase: PhaseObservation
    activity: ActivityObservation
    lifecycle: LifecycleFact


_MANAGED_EXECUTION_HOMES = {
    SessionExecutionHome.MANAGED_LOCAL,
    SessionExecutionHome.MANAGED_HOSTED,
    SessionExecutionHome.CLOUD_TAKEOVER,
}

# `process_gone` is an explicit engine-snapshot terminal fact: the process is
# gone, not merely unverifiable.
_EXPLICIT_CLOSED_TERMINAL_STATES = {"session_ended", "finished", "user_closed", "process_gone"}
_UNVERIFIED_TERMINAL_STATES = {"host_expired"}


def _control_path(capabilities: SessionCapabilityFlags) -> str:
    if capabilities.execution_home in _MANAGED_EXECUTION_HOMES:
        return "managed"
    if capabilities.managed_transport is not None:
        return "managed"
    return "unmanaged"


def _normalized(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _explicit_lifecycle(runtime_view: SessionRuntimeView) -> LifecycleFact | None:
    terminal_state = _normalized(runtime_view.terminal_state)
    if terminal_state is None:
        return None
    observed_at = runtime_view.presence_updated_at or runtime_view.last_live_at
    terminal_reason = _normalized(runtime_view.terminal_reason)
    if terminal_state in _EXPLICIT_CLOSED_TERMINAL_STATES:
        return LifecycleFact(state="closed", reason=terminal_reason or terminal_state, observed_at=observed_at)
    if terminal_state in _UNVERIFIED_TERMINAL_STATES:
        return LifecycleFact(state="unknown", reason=terminal_reason or terminal_state, observed_at=observed_at)
    return LifecycleFact(state="closed", reason=terminal_reason or "provider_signal", observed_at=observed_at)


def _phase_observation(runtime_view: SessionRuntimeView) -> PhaseObservation:
    source = _normalized(runtime_view.runtime_source)
    if source in {None, "fallback", "progress"} or runtime_view.confidence != "live":
        return PhaseObservation(
            kind=None,
            tool=None,
            source=source,
            observed_at=None,
            expires_at=None,
        )
    return PhaseObservation(
        kind=_normalized(runtime_view.runtime_phase),
        tool=_normalized(runtime_view.active_tool),
        source=source,
        observed_at=runtime_view.presence_updated_at or runtime_view.phase_started_at,
        expires_at=runtime_view.freshness_expires_at,
    )


def _host_observation(*, binding_overlay, binding_host_state: str | None) -> HostObservation:
    state = _normalized(binding_host_state) or _normalized(getattr(binding_overlay, "host_state", None)) or "unknown"
    return HostObservation(
        state=state,
        last_seen_at=getattr(binding_overlay, "host_last_seen_at", None),
        source=("machine_heartbeat" if state != "unknown" else None),
    )


def _process_observation(
    *,
    control_path: str,
    binding_overlay,
    binding_terminal_reason: str | None,
) -> ProcessObservation:
    reason = _normalized(binding_terminal_reason) or _normalized(getattr(binding_overlay, "terminal_reason", None))
    if binding_overlay is None:
        return ProcessObservation(status="unknown", reason=None, source=None)

    binding_state = _normalized(getattr(binding_overlay, "binding_state", None)) or "observed"
    pid = getattr(binding_overlay, "pid", None)
    process_start_time = getattr(binding_overlay, "process_start_time", None)
    if control_path == "managed":
        status = "unknown"
    elif reason == "process_gone":
        status = "not_observed"
    elif reason == "host_expired":
        status = "unknown"
    elif (
        binding_state == "observed"
        and _normalized(getattr(binding_overlay, "host_state", None)) == "online"
        and pid is not None
        and process_start_time is not None
    ):
        status = "observed"
    else:
        status = "unknown"

    return ProcessObservation(
        status=status,
        pid=pid,
        process_start_time=process_start_time,
        observed_at=getattr(binding_overlay, "observed_at", None),
        last_seen_at=getattr(binding_overlay, "last_seen_at", None),
        source_mtime=getattr(binding_overlay, "source_mtime", None),
        source_path=getattr(binding_overlay, "source_path", None),
        reason=reason,
        source="machine_process_scan",
    )


def _lifecycle(
    *,
    explicit: LifecycleFact | None,
    control_path: str,
    process: ProcessObservation,
    phase: PhaseObservation,
) -> LifecycleFact:
    if explicit is not None:
        return explicit
    if process.status == "observed":
        return LifecycleFact(state="open", reason="process_observed", observed_at=process.observed_at or process.last_seen_at)
    if control_path == "unmanaged" and process.status == "not_observed" and process.reason == "process_gone":
        return LifecycleFact(state="closed", reason="process_gone", observed_at=process.last_seen_at or process.observed_at)
    if phase.kind is not None:
        return LifecycleFact(state="open", reason="phase_observed", observed_at=phase.observed_at)
    return LifecycleFact(state="unknown", reason=None, observed_at=None)


def _process_state(
    *,
    process: ProcessObservation,
    lifecycle: LifecycleFact,
) -> str:
    if lifecycle.state == "closed":
        return "closed"
    if process.status == "observed":
        return "running"
    return "unknown"


def build_session_liveness_facts(
    *,
    runtime_view: SessionRuntimeView,
    capabilities: SessionCapabilityFlags,
    last_activity_at: datetime | None,
    binding_overlay=None,
    binding_host_state: str | None = None,
    binding_terminal_reason: str | None = None,
) -> SessionLivenessFacts:
    control_path = _control_path(capabilities)
    host = _host_observation(binding_overlay=binding_overlay, binding_host_state=binding_host_state)
    process = _process_observation(
        control_path=control_path,
        binding_overlay=binding_overlay,
        binding_terminal_reason=binding_terminal_reason,
    )
    phase = _phase_observation(runtime_view)
    activity = ActivityObservation(
        last_transcript_at=last_activity_at,
        last_runtime_signal_at=runtime_view.presence_updated_at,
        last_progress_at=runtime_view.last_progress_at,
    )
    lifecycle = _lifecycle(
        explicit=_explicit_lifecycle(runtime_view),
        control_path=control_path,
        process=process,
        phase=phase,
    )
    process_state = _process_state(
        process=process,
        lifecycle=lifecycle,
    )
    return SessionLivenessFacts(
        control_path=control_path,
        process_state=process_state,
        host=host,
        process=process,
        phase=phase,
        activity=activity,
        lifecycle=lifecycle,
    )
