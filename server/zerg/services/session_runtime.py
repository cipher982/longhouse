"""Runtime event ingestion and materialized runtime state.

This module owns the provider-agnostic runtime reducer used by Timeline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.metrics import managed_codex_bridge_freshness_total
from zerg.metrics import managed_codex_liveness_invariant_sessions
from zerg.metrics import managed_codex_runtime_observations_total
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_live_previews import live_preview_candidate_from_runtime_event
from zerg.services.session_live_previews import upsert_session_live_preview
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_observations import record_runtime_observation
from zerg.utils.time import normalize_utc

RuntimeEventKind = Literal["phase_signal", "progress_signal", "terminal_signal", "binding_signal"]
RuntimeEventApplyOutcome = Literal["applied", "ignored", "protected_session_ended", "stored_live_overlay"]

PHASE_FRESHNESS = {
    "thinking": timedelta(seconds=90),
    "running": timedelta(minutes=10),
    "idle": timedelta(minutes=10),
    "blocked": timedelta(hours=24),
    # `needs_user` is often the provider's normal prompt after a response.
    # Without a session-level heartbeat, do not let it imply live control all day.
    "needs_user": timedelta(minutes=10),
}
MANAGED_CODEX_FRESHNESS = timedelta(minutes=15)
# Irreversible session endings. These states stamp AgentSession.ended_at and
# render as closed in liveness facts.
EXPLICIT_CLOSED_TERMINAL_STATES = {"session_ended", "user_closed", "process_gone"}
UNVERIFIED_TERMINAL_STATES = {"host_expired"}
LIVE_EXECUTION_PHASES = {"thinking", "running"}
ATTENTION_PHASES = {"blocked"}
KNOWN_PHASES = {"thinking", "running", "blocked", "needs_user", "idle", "finished"}
MANAGED_SESSION_LEASE_SOURCE = "engine_attached_lease"
MANAGED_CODEX_RUNTIME_SOURCES = {MANAGED_SESSION_LEASE_SOURCE, "codex_bridge", "codex_bridge_live"}
MANAGED_CODEX_INVARIANTS = ("ended_without_session_ended", "short_freshness")


def _latest_timestamp(*values: datetime | None) -> datetime | None:
    normalized = [normalize_utc(value) for value in values]
    present = [value for value in normalized if value is not None]
    return max(present) if present else None


def _payload_timestamp(payload: Mapping[str, Any], key: str) -> datetime | None:
    raw = payload.get(key)
    if isinstance(raw, datetime):
        return normalize_utc(raw)
    if not isinstance(raw, str):
        return None
    try:
        return normalize_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _stale_managed_lease_observed_at(
    event: RuntimeEventIngest,
    latest_phase_signal_at: datetime | None,
) -> datetime | None:
    if latest_phase_signal_at is None:
        return None
    if str(event.source or "").strip() != MANAGED_SESSION_LEASE_SOURCE:
        return None
    lease_observed_at = _payload_timestamp(event.payload or {}, "lease_observed_at") or _payload_timestamp(
        event.payload or {},
        "observed_at",
    )
    if lease_observed_at is None or lease_observed_at >= latest_phase_signal_at:
        return None
    return lease_observed_at


def _managed_lease_refresh_at(event: RuntimeEventIngest, fallback: datetime) -> datetime:
    if str(event.source or "").strip() != MANAGED_SESSION_LEASE_SOURCE:
        return fallback
    return _payload_timestamp(event.payload or {}, "lease_refresh_at") or fallback


def coerce_session_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def runtime_key_for_session(provider: str, session_identifier: str) -> str:
    return f"{(provider or 'unknown').strip() or 'unknown'}:{session_identifier.strip()}"


def phase_freshness_ms(phase: str | None) -> int | None:
    window = PHASE_FRESHNESS.get((phase or "").strip())
    if window is None:
        return None
    return int(window.total_seconds() * 1000)


def _is_managed_codex_runtime_signal(event: RuntimeEventIngest) -> bool:
    provider = (event.provider or "").strip().lower()
    source = (event.source or "").strip().lower()
    return provider == "codex" and source in MANAGED_CODEX_RUNTIME_SOURCES


def _managed_codex_source_label(source: str | None) -> str:
    normalized = (source or "").strip().lower()
    return normalized if normalized in MANAGED_CODEX_RUNTIME_SOURCES else "other"


def _record_managed_codex_runtime_observation(event: RuntimeEventIngest, outcome: str) -> None:
    if not _is_managed_codex_runtime_signal(event):
        return
    managed_codex_runtime_observations_total.labels(
        source=_managed_codex_source_label(event.source),
        kind=event.kind,
        outcome=outcome,
    ).inc()


def _phase_signal_freshness_ms(event: RuntimeEventIngest, phase: str) -> int | None:
    provider = (event.provider or "").strip().lower()
    source = (event.source or "").strip().lower()
    if event.freshness_ms is not None:
        if provider == "codex" and source == "codex_bridge":
            managed_codex_bridge_freshness_total.labels(outcome="explicit_override").inc()
        return event.freshness_ms
    if provider == "codex" and source == "codex_bridge":
        managed_codex_bridge_freshness_total.labels(outcome="managed_budget").inc()
        return int(MANAGED_CODEX_FRESHNESS.total_seconds() * 1000)
    return phase_freshness_ms(phase)


class RuntimeEventIngest(BaseModel):
    runtime_key: str = Field(..., min_length=1, max_length=255)
    session_id: UUID | None = None
    provider: str = Field(..., min_length=1, max_length=64)
    device_id: str | None = Field(None, max_length=255)
    source: str = Field(..., min_length=1, max_length=64)
    kind: RuntimeEventKind
    phase: str | None = Field(None, max_length=32)
    tool_name: str | None = Field(None, max_length=128)
    occurred_at: datetime
    freshness_ms: int | None = Field(None, ge=0, le=7 * 24 * 60 * 60 * 1000)
    dedupe_key: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeEventBatchIngest(BaseModel):
    events: list[RuntimeEventIngest] = Field(..., min_length=1, max_length=128)


class RuntimeEventBatchResult(BaseModel):
    accepted: int
    duplicates: int
    updated_runtime_keys: list[str]


@dataclass(frozen=True)
class SessionRuntimeView:
    signal_tier: str
    runtime_phase: str | None
    phase_started_at: datetime | None
    last_progress_at: datetime | None
    runtime_source: str
    terminal_state: str | None
    terminal_reason: str | None
    terminal_source: str | None
    runtime_version: int
    status: str
    presence_state: str | None
    presence_tool: str | None
    presence_updated_at: datetime | None
    last_live_at: datetime | None
    display_phase: str
    active_tool: str | None
    confidence: str | None
    timeline_anchor_at: datetime
    freshness_expires_at: datetime | None = None


def _confidence_for_state(state: SessionRuntimeState, *, now: datetime) -> str:
    freshness_expires_at = normalize_utc(state.freshness_expires_at)
    if freshness_expires_at is not None and freshness_expires_at > now:
        return "live"
    return "stale"


def _display_phase_for_state(
    *,
    phase: str,
    active_tool: str | None,
    confidence: str,
    terminal_state: str | None,
    status: str,
) -> str:
    if terminal_state is not None or phase == "finished" or status == "completed":
        return "Completed"
    if confidence == "stale" and phase in KNOWN_PHASES:
        return "Inactive"
    if phase == "running":
        return f"Running {active_tool}" if active_tool else "Running"
    if phase == "thinking":
        return "Thinking"
    if phase == "needs_user":
        return "Idle"
    if phase == "blocked":
        return f"Blocked on {active_tool}" if active_tool else "Needs permission"
    if phase == "idle":
        return "Idle"
    return "Inactive"


def _status_for_state(
    *,
    phase: str,
    confidence: str,
    terminal_state: str | None,
    ended_at: datetime | None,
) -> str:
    if terminal_state is not None or phase == "finished":
        return "completed"
    if confidence == "live":
        if phase in LIVE_EXECUTION_PHASES:
            return "working"
        if phase in ATTENTION_PHASES:
            return "active"
        return "idle"
    return "idle"


def _signal_tier_for_state(*, phase_source: str, confidence: str | None) -> str:
    if phase_source == "progress":
        return "transcript_progress"
    if phase_source not in {"fallback", ""}:
        return "phase_signal"
    return "none"


def build_runtime_view(
    *,
    state: SessionRuntimeState,
    session: AgentSession,
    now: datetime,
) -> SessionRuntimeView:
    normalized_now = normalize_utc(now) or datetime.now(timezone.utc)
    confidence = _confidence_for_state(state, now=normalized_now)
    runtime_phase = (state.phase or "idle").strip() or "idle"
    terminal_state = (state.terminal_state or "").strip() or None
    terminal_reason = (state.terminal_reason or "").strip() or None
    terminal_source = (state.terminal_source or "").strip() or None
    phase_source = (state.phase_source or "fallback").strip() or "fallback"
    active_tool = (state.active_tool or "").strip() or None
    timeline_anchor_at = normalize_utc(state.timeline_anchor_at) or normalize_utc(session.started_at) or normalized_now
    status = _status_for_state(
        phase=runtime_phase,
        confidence=confidence,
        terminal_state=terminal_state,
        ended_at=normalize_utc(session.ended_at),
    )
    presence_state: str | None = None
    presence_tool: str | None = None
    presence_updated_at = normalize_utc(state.last_runtime_signal_at)
    last_live_at = normalize_utc(state.last_live_at)
    if phase_source == "progress" and terminal_state is None:
        last_live_at = None

    if confidence == "live" and runtime_phase in KNOWN_PHASES:
        presence_state = runtime_phase
        presence_tool = active_tool if runtime_phase in {"running", "blocked"} else None

    exposed_runtime_phase = runtime_phase
    if confidence != "live" and terminal_state is None and runtime_phase != "finished":
        exposed_runtime_phase = ""
    if phase_source == "progress" and terminal_state is None:
        exposed_runtime_phase = ""

    display_phase = _display_phase_for_state(
        phase=runtime_phase,
        active_tool=active_tool,
        confidence=confidence,
        terminal_state=terminal_state,
        status=status,
    )
    if phase_source == "progress" and confidence == "stale" and terminal_state is None:
        display_phase = "Inactive"

    return SessionRuntimeView(
        signal_tier=_signal_tier_for_state(phase_source=phase_source, confidence=confidence),
        runtime_phase=exposed_runtime_phase or None,
        phase_started_at=normalize_utc(state.phase_started_at),
        last_progress_at=normalize_utc(state.last_progress_at),
        runtime_source=phase_source,
        terminal_state=terminal_state,
        terminal_reason=terminal_reason,
        terminal_source=terminal_source,
        runtime_version=int(state.runtime_version or 0),
        status=status,
        presence_state=presence_state,
        presence_tool=presence_tool,
        presence_updated_at=presence_updated_at,
        last_live_at=last_live_at or presence_updated_at,
        display_phase=display_phase,
        active_tool=active_tool,
        confidence=confidence,
        timeline_anchor_at=timeline_anchor_at,
        freshness_expires_at=normalize_utc(state.freshness_expires_at),
    )


def build_fallback_runtime_view(
    *,
    session: AgentSession,
    last_activity_at: datetime | None,
    now: datetime,
) -> SessionRuntimeView:
    normalized_now = normalize_utc(now) or datetime.now(timezone.utc)
    started_at = normalize_utc(session.started_at) or normalized_now
    last_activity = normalize_utc(last_activity_at) or normalize_utc(session.ended_at)
    progress_at = last_activity or started_at

    explicit_terminal = (getattr(session, "terminal_state", None) or "").strip() or None

    timeline_anchor_at = _latest_timestamp(progress_at, started_at) or normalized_now
    last_live_at: datetime | None = None
    confidence: str | None = None

    if explicit_terminal is not None:
        status = "completed"
        runtime_phase = "finished"
        display_phase_input = runtime_phase
        terminal_state: str | None = explicit_terminal
    else:
        status = "idle"
        runtime_phase = None
        display_phase_input = "idle"
        terminal_state = None

    display_phase = (
        _display_phase_for_state(
            phase=display_phase_input,
            active_tool=None,
            confidence=confidence or "stale",
            terminal_state=terminal_state,
            status=status,
        )
        if terminal_state is not None
        else "Inactive"
    )

    return SessionRuntimeView(
        signal_tier="none",
        runtime_phase=runtime_phase,
        phase_started_at=progress_at,
        last_progress_at=progress_at,
        runtime_source="fallback",
        terminal_state=terminal_state,
        terminal_reason=None,
        terminal_source=None,
        runtime_version=0,
        status=status,
        presence_state=None,
        presence_tool=None,
        presence_updated_at=None,
        last_live_at=last_live_at,
        display_phase=display_phase,
        active_tool=None,
        confidence=confidence,
        timeline_anchor_at=timeline_anchor_at,
        freshness_expires_at=None,
    )


def should_include_runtime_view(
    *,
    session: AgentSession,
    runtime_view: SessionRuntimeView | None,
) -> bool:
    if runtime_view is None:
        return False
    has_explicit_terminal = bool((getattr(session, "terminal_state", None) or "").strip())
    if not has_explicit_terminal:
        return True
    return (
        runtime_view.presence_updated_at is not None
        or runtime_view.last_live_at is not None
        or runtime_view.runtime_source not in {None, "fallback"}
    )


def resolve_runtime_overlay(
    session: AgentSession,
    *,
    last_activity_at: datetime | None,
    runtime_state_map: Mapping[str, SessionRuntimeState],
    now: datetime,
) -> SessionRuntimeView:
    """Runtime overlay sourced exclusively from SessionRuntimeState.

    Every `/api/agents/presence` call emits a RuntimeEventIngest which the
    reducer materializes into SessionRuntimeState — that row is the single
    source of truth for phase, tool, and confidence.
    """
    session_key = str(session.id)
    runtime_state = runtime_state_map.get(session_key)
    if runtime_state is not None:
        return build_runtime_view(
            state=runtime_state,
            session=session,
            now=now,
        )

    return build_fallback_runtime_view(
        session=session,
        last_activity_at=last_activity_at,
        now=now,
    )


def current_presence_state_for_session(
    db: Session,
    session_id: UUID,
    *,
    session: AgentSession | None = None,
    now: datetime | None = None,
) -> str | None:
    target_session = session or db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if target_session is None:
        return None

    runtime_state_map = load_runtime_state_map(db, [session_id])
    runtime_overlay = resolve_runtime_overlay(
        target_session,
        last_activity_at=target_session.last_activity_at,
        runtime_state_map=runtime_state_map,
        now=now or datetime.now(timezone.utc),
    )
    return runtime_overlay.presence_state


def load_runtime_state_map(db: Session, session_ids: list[UUID]) -> dict[str, SessionRuntimeState]:
    if not session_ids:
        return {}

    rows = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id.in_(session_ids))
        .order_by(SessionRuntimeState.updated_at.desc(), SessionRuntimeState.runtime_version.desc())
        .all()
    )

    state_by_session: dict[str, SessionRuntimeState] = {}
    for row in rows:
        if row.session_id is None:
            continue
        key = str(row.session_id)
        state_by_session.setdefault(key, row)
    return state_by_session


def _is_bridge_transcript_event(event: RuntimeEventIngest) -> bool:
    payload = event.payload or {}
    return (
        (event.provider or "").strip().lower() == "codex"
        and (event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


def _managed_codex_session_ids(db: Session) -> list[UUID]:
    # Session-identity-kernel cleanup: ``execution_home`` /
    # ``managed_transport`` were dropped. Use SessionConnection rows on the
    # ``codex_bridge``/``codex_app_server`` control plane to identify managed
    # Codex sessions.
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionThread

    rows = (
        db.query(AgentSession.id)
        .join(SessionThread, SessionThread.id == AgentSession.primary_thread_id)
        .join(SessionRun, SessionRun.thread_id == SessionThread.id)
        .join(SessionConnection, SessionConnection.run_id == SessionRun.id)
        .filter(AgentSession.provider == "codex")
        .filter(SessionConnection.control_plane.in_(["codex_bridge", "codex_app_server"]))
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def managed_codex_liveness_invariant_counts(db: Session) -> dict[str, int]:
    """Return SQL-reconstructable managed Codex liveness invariant counts."""
    managed_session_ids = _managed_codex_session_ids(db)
    if not managed_session_ids:
        return {invariant: 0 for invariant in MANAGED_CODEX_INVARIANTS}

    final_session_ids = {
        row[0]
        for row in db.query(SessionRuntimeState.session_id)
        .filter(SessionRuntimeState.session_id.in_(managed_session_ids))
        .filter(SessionRuntimeState.terminal_state == "session_ended")
        .all()
        if row[0] is not None
    }
    parser_ended_ids = {
        row[0]
        for row in db.query(AgentSession.id)
        .filter(AgentSession.id.in_(managed_session_ids))
        .filter(AgentSession.ended_at.isnot(None))
        .all()
    }
    ended_without_session_ended = len(parser_ended_ids - final_session_ids)

    short_freshness = 0
    states = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id.in_(managed_session_ids))
        .filter(SessionRuntimeState.terminal_state.is_(None))
        .filter(SessionRuntimeState.freshness_expires_at.isnot(None))
        .filter(SessionRuntimeState.last_runtime_signal_at.isnot(None))
        .all()
    )
    latest_managed_observation_by_runtime_key: dict[str, SessionObservation] = {}
    runtime_keys = [state.runtime_key for state in states]
    if runtime_keys:
        latest_managed_observations = (
            db.query(SessionObservation)
            .filter(SessionObservation.runtime_key.in_(runtime_keys))
            .filter(SessionObservation.provider == "codex")
            .filter(SessionObservation.source.in_(MANAGED_CODEX_RUNTIME_SOURCES))
            .filter(SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL)
            .filter(SessionObservation.payload_json.like('%"kind":"phase_signal"%'))
            .order_by(
                SessionObservation.runtime_key.asc(),
                SessionObservation.observed_at.desc(),
                SessionObservation.id.desc(),
            )
            .all()
        )
        for observation in latest_managed_observations:
            latest_managed_observation_by_runtime_key.setdefault(observation.runtime_key, observation)

    for state in states:
        last_signal_at = normalize_utc(state.last_runtime_signal_at)
        freshness_expires_at = normalize_utc(state.freshness_expires_at)
        if last_signal_at is None or freshness_expires_at is None:
            continue
        latest_managed_observation = latest_managed_observation_by_runtime_key.get(state.runtime_key)
        if latest_managed_observation is None:
            continue
        latest_managed_observation_at = normalize_utc(latest_managed_observation.observed_at)
        if latest_managed_observation_at is None or latest_managed_observation_at != last_signal_at:
            continue
        if freshness_expires_at - last_signal_at < MANAGED_CODEX_FRESHNESS:
            short_freshness += 1

    return {
        "ended_without_session_ended": ended_without_session_ended,
        "short_freshness": short_freshness,
    }


def refresh_managed_codex_liveness_metrics(db: Session) -> dict[str, int]:
    counts = managed_codex_liveness_invariant_counts(db)
    for invariant in MANAGED_CODEX_INVARIANTS:
        managed_codex_liveness_invariant_sessions.labels(invariant=invariant).set(counts.get(invariant, 0))
    return counts


def ingest_runtime_events(db: Session, events: list[RuntimeEventIngest]) -> RuntimeEventBatchResult:
    accepted = 0
    duplicates = 0
    updated_runtime_keys: list[str] = []

    for event in events:
        received_at = datetime.now(timezone.utc)
        bridge_transcript_event = _is_bridge_transcript_event(event)
        observation_result = record_runtime_observation(
            db,
            event,
            received_at=received_at,
            load_observation=not bridge_transcript_event,
        )
        if not observation_result.inserted:
            duplicates += 1
            _record_managed_codex_runtime_observation(event, "duplicate")
            continue

        accepted += 1
        if bridge_transcript_event:
            observation_id = f"runtime:{event.source}:{event.dedupe_key}"
            preview_candidate = live_preview_candidate_from_runtime_event(event, observation_id=observation_id)
            if preview_candidate is not None:
                upsert_session_live_preview(db, preview_candidate)
            outcome = "stored_live_overlay"
            _record_managed_codex_runtime_observation(event, outcome)
            if event.runtime_key not in updated_runtime_keys:
                updated_runtime_keys.append(event.runtime_key)
            continue

        if observation_result.observation is None:
            raise RuntimeError("accepted runtime observation was not readable after insert")
        outcome = reduce_runtime_signal_observation(db, observation_result.observation)
        _record_managed_codex_runtime_observation(event, outcome)
        if outcome == "applied" and event.runtime_key not in updated_runtime_keys:
            updated_runtime_keys.append(event.runtime_key)

    return RuntimeEventBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        updated_runtime_keys=updated_runtime_keys,
    )


def runtime_event_from_observation(observation) -> RuntimeEventIngest | None:
    if observation.kind != OBS_KIND_RUNTIME_SIGNAL:
        return None
    payload = _observation_payload(observation)
    kind = str(payload.get("kind") or "").strip()
    valid_kinds = {"phase_signal", "progress_signal", "terminal_signal", "binding_signal"}
    if kind not in valid_kinds:
        raise ValueError(f"runtime_signal observation {observation.observation_id} has invalid kind {kind!r}")
    return RuntimeEventIngest(
        runtime_key=observation.runtime_key
        or runtime_key_for_session(
            observation.provider,
            str(observation.session_id or "unknown"),
        ),
        session_id=observation.session_id,
        provider=observation.provider,
        device_id=observation.device_id,
        source=observation.source,
        kind=kind,  # type: ignore[arg-type]
        phase=_optional_payload_str(payload.get("phase")),
        tool_name=_optional_payload_str(payload.get("tool_name")),
        occurred_at=normalize_utc(observation.observed_at) or datetime.now(timezone.utc),
        freshness_ms=_optional_payload_int(payload.get("freshness_ms")),
        dedupe_key=_dedupe_key_from_observation(observation, kind=kind),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )


def reduce_runtime_signal_observation(db: Session, observation) -> RuntimeEventApplyOutcome:
    event = runtime_event_from_observation(observation)
    if event is None:
        return "ignored"
    return _apply_runtime_event(db, event)


def _observation_payload(observation) -> dict[str, Any]:
    raw = getattr(observation, "payload_json", None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _dedupe_key_from_observation(observation, *, kind: str) -> str:
    payload = _observation_payload(observation)
    dedupe_key = str(payload.get("dedupe_key") or "").strip()
    if dedupe_key:
        return dedupe_key
    source_cursor = str(getattr(observation, "source_cursor", "") or "")
    prefix = f"{kind}:"
    if source_cursor.startswith(prefix) and len(source_cursor) > len(prefix):
        return source_cursor[len(prefix) :]
    observation_id = str(getattr(observation, "observation_id", "") or "")
    source_prefix = f"runtime:{observation.source}:"
    if observation_id.startswith(source_prefix) and len(observation_id) > len(source_prefix):
        return observation_id[len(source_prefix) :]
    raise ValueError(f"runtime_signal observation {observation_id!r} has no reconstructable dedupe key")


def _optional_payload_str(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_payload_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_snapshot(state: SessionRuntimeState | None) -> tuple[Any, ...] | None:
    if state is None:
        return None
    return (
        state.session_id,
        state.provider,
        state.device_id,
        state.phase,
        state.phase_source,
        state.active_tool,
        normalize_utc(state.phase_started_at),
        normalize_utc(state.last_runtime_signal_at),
        normalize_utc(state.last_progress_at),
        normalize_utc(state.last_live_at),
        normalize_utc(state.timeline_anchor_at),
        normalize_utc(state.freshness_expires_at),
        state.terminal_state,
        state.terminal_reason,
        state.terminal_source,
        normalize_utc(state.terminal_at),
    )


def _ensure_state(db: Session, event: RuntimeEventIngest) -> SessionRuntimeState:
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).first()
    if state is not None:
        return state

    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    thread_id = ensure_thread_id_for_session(db, event.session_id) if event.session_id is not None else None
    state = SessionRuntimeState(
        runtime_key=event.runtime_key,
        session_id=event.session_id,
        thread_id=thread_id,
        provider=event.provider,
        device_id=event.device_id,
        phase=(event.phase or "idle").strip() or "idle",
        phase_source="fallback",
        active_tool=None,
        phase_started_at=occurred_at,
        last_runtime_signal_at=None,
        last_progress_at=None,
        last_live_at=None,
        timeline_anchor_at=occurred_at,
        freshness_expires_at=None,
        terminal_state=None,
        terminal_reason=None,
        terminal_source=None,
        terminal_at=None,
        runtime_version=0,
    )
    db.add(state)
    db.flush()
    return state


def _phase_reanchors(prev_phase: str | None, next_phase: str) -> bool:
    if prev_phase is None:
        return True
    if next_phase in ATTENTION_PHASES:
        return prev_phase != next_phase
    return prev_phase not in LIVE_EXECUTION_PHASES and next_phase in LIVE_EXECUTION_PHASES


def _apply_runtime_event(db: Session, event: RuntimeEventIngest) -> RuntimeEventApplyOutcome:
    state = _ensure_state(db, event)
    before = _state_snapshot(state)
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)

    if state.terminal_state == "session_ended":
        incoming_terminal_state = str((event.payload or {}).get("terminal_state") or "").strip()
        if event.kind != "terminal_signal" or incoming_terminal_state != "session_ended":
            return "protected_session_ended"

    if event.session_id is not None and state.session_id != event.session_id:
        state.session_id = event.session_id
        from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

        state.thread_id = ensure_thread_id_for_session(db, event.session_id)
    if event.provider and state.provider != event.provider:
        state.provider = event.provider
    if event.device_id is not None and state.device_id != event.device_id:
        state.device_id = event.device_id

    if event.kind == "phase_signal":
        latest_phase_signal_at = _latest_timestamp(
            state.last_runtime_signal_at,
            state.last_progress_at,
            state.terminal_at,
        )
        next_phase = (event.phase or state.phase or "idle").strip() or "idle"
        if next_phase not in KNOWN_PHASES:
            next_phase = "idle"
        stale_lease_observed_at = _stale_managed_lease_observed_at(event, latest_phase_signal_at)
        next_active_tool = None
        if next_phase in {"running", "blocked"}:
            # Blocked-with-no-tool means "still blocked on the same tool as
            # last time" — keep the prior active_tool instead of dropping
            # it. Running signals always carry the tool explicitly.
            next_active_tool = event.tool_name or (state.active_tool if next_phase == "blocked" else None)
        if latest_phase_signal_at is not None and occurred_at < latest_phase_signal_at:
            if stale_lease_observed_at is None:
                return "ignored"
            current_phase = (state.phase or "idle").strip() or "idle"
            if next_phase != current_phase:
                return "ignored"
            current_active_tool = state.active_tool if next_phase in {"running", "blocked"} else None
            if (next_active_tool or None) != (current_active_tool or None):
                return "ignored"
            refresh_at = _managed_lease_refresh_at(event, occurred_at)
            freshness_ms = _phase_signal_freshness_ms(event, next_phase)
            state.last_live_at = _latest_timestamp(state.last_live_at, refresh_at)
            state.freshness_expires_at = None
            if freshness_ms is not None:
                state.freshness_expires_at = refresh_at + timedelta(milliseconds=freshness_ms)
            event_source = str(event.source or "").strip()
            if event_source in MANAGED_CODEX_RUNTIME_SOURCES:
                state.phase_source = event_source
            state.terminal_state = None
            state.terminal_reason = None
            state.terminal_source = None
            state.terminal_at = None
            if before != _state_snapshot(state):
                state.runtime_version = int(state.runtime_version or 0) + 1
                db.add(state)
                return "applied"
            return "ignored"
        phase_changed = state.phase != next_phase
        tool_phase = next_phase in {"running", "blocked"}
        active_tool_changed = tool_phase and (state.active_tool or None) != (next_active_tool or None)
        if phase_changed or active_tool_changed:
            if phase_changed and _phase_reanchors(state.phase, next_phase):
                state.timeline_anchor_at = occurred_at
            state.phase_started_at = occurred_at
        state.phase = next_phase
        event_source = str(event.source or "").strip()
        state.phase_source = event_source if event_source in MANAGED_CODEX_RUNTIME_SOURCES else "semantic"
        if next_phase in {"running", "blocked"}:
            state.active_tool = next_active_tool
        else:
            state.active_tool = None
        state.last_runtime_signal_at = occurred_at
        freshness_base_at = _managed_lease_refresh_at(event, occurred_at)
        state.last_live_at = _latest_timestamp(occurred_at, freshness_base_at)
        freshness_ms = _phase_signal_freshness_ms(event, next_phase)
        state.freshness_expires_at = None
        if freshness_ms is not None:
            state.freshness_expires_at = freshness_base_at + timedelta(milliseconds=freshness_ms)
        state.terminal_state = None
        state.terminal_reason = None
        state.terminal_source = None
        state.terminal_at = None

    elif event.kind == "progress_signal":
        latest_progress_related_at = _latest_timestamp(
            state.last_progress_at,
            state.last_runtime_signal_at,
            state.terminal_at,
        )
        if latest_progress_related_at is not None and occurred_at < latest_progress_related_at:
            return "ignored"
        state.last_progress_at = occurred_at
        state.timeline_anchor_at = occurred_at
        progress_kind = str((event.payload or {}).get("progress_kind") or "").strip()
        if state.terminal_state is None and state.phase in ATTENTION_PHASES and progress_kind == "transcript_append":
            state.phase = "idle"
            state.active_tool = None
            state.freshness_expires_at = None
            state.phase_started_at = occurred_at
            state.phase_source = "progress"
        if state.terminal_state is not None and (
            normalize_utc(state.terminal_at) is None or occurred_at >= normalize_utc(state.terminal_at)
        ):
            state.terminal_state = None
            state.terminal_reason = None
            state.terminal_source = None
            state.terminal_at = None
            state.phase = "idle"
            state.active_tool = None
            state.freshness_expires_at = None
            state.phase_started_at = occurred_at
            state.phase_source = "progress"
        if state.phase_source in {"fallback", "progress"}:
            state.phase_source = "progress"

    elif event.kind == "terminal_signal":
        terminal_state = str((event.payload or {}).get("terminal_state") or "finished").strip() or "finished"
        latest_terminal_related_at = _latest_timestamp(
            state.last_runtime_signal_at,
            state.last_progress_at,
            state.terminal_at,
        )
        terminal_is_newer_than_event = False
        if latest_terminal_related_at is not None:
            terminal_is_newer_than_event = occurred_at < latest_terminal_related_at
        terminal_is_session_end = terminal_state == "session_ended"
        terminal_superseded = terminal_is_newer_than_event and not terminal_is_session_end
        if terminal_superseded:
            return "ignored"
        terminal_reason = str((event.payload or {}).get("terminal_reason") or "").strip() or None
        if terminal_reason is None and terminal_state in {"process_gone", "host_expired", "user_closed"}:
            terminal_reason = terminal_state
        terminal_source = str((event.payload or {}).get("terminal_source") or event.source or "").strip() or None
        state.phase = "finished"
        state.phase_source = "semantic"
        state.active_tool = None
        state.last_runtime_signal_at = occurred_at
        state.last_live_at = occurred_at
        state.freshness_expires_at = occurred_at
        state.terminal_state = terminal_state
        state.terminal_reason = terminal_reason
        state.terminal_source = terminal_source
        state.terminal_at = occurred_at
        state.timeline_anchor_at = _payload_timestamp(event.payload or {}, "timeline_anchor_at") or occurred_at
        phase_started_at = normalize_utc(state.phase_started_at)
        if phase_started_at is None or phase_started_at < occurred_at:
            state.phase_started_at = occurred_at
        if terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES and event.session_id is not None:
            session = db.query(AgentSession).filter(AgentSession.id == event.session_id).first()
            if session is not None and session.ended_at is None:
                session.ended_at = occurred_at

    elif event.kind == "binding_signal":
        if event.session_id is not None:
            state.session_id = event.session_id

    after = _state_snapshot(state)
    if after != before:
        state.runtime_version = int(state.runtime_version or 0) + 1
        db.flush()
        return "applied"
    return "ignored"
