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
from zerg.metrics import managed_codex_runtime_observations_total
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionRuntimeState
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.session_live_previews import live_preview_candidate_from_runtime_event
from zerg.services.session_live_previews import upsert_live_session_live_preview
from zerg.services.session_live_previews import upsert_session_live_preview
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.session_observations import decode_observation_payload_json
from zerg.services.session_observations import record_runtime_observation
from zerg.utils.time import normalize_utc

RuntimeEventKind = Literal[
    "phase_signal",
    "progress_signal",
    "terminal_signal",
    "binding_signal",
    "pause_request",
    "pause_resolution",
]
RuntimeEventApplyOutcome = Literal["applied", "ignored", "protected_session_ended", "stored_live_overlay"]

PHASE_FRESHNESS = {
    "thinking": timedelta(seconds=90),
    "running": timedelta(minutes=10),
    "stalled": timedelta(minutes=10),
    "idle": timedelta(minutes=10),
    "blocked": timedelta(hours=24),
    # `needs_user` is often the provider's normal prompt after a response.
    # Without a session-level heartbeat, do not let it imply live control all day.
    "needs_user": timedelta(minutes=10),
}
MANAGED_CODEX_FRESHNESS = timedelta(minutes=15)
# Only explicit user/admin closure closes the durable session. Provider exit and
# process loss end the current run; they never change session disposition.
EXPLICIT_CLOSED_TERMINAL_STATES = {"user_closed"}
RUN_END_TERMINAL_STATES = {"session_ended", "process_gone"}
RUN_TERMINAL_STATES = {"run_completed", "run_failed", "run_cancelled"}
UNVERIFIED_TERMINAL_STATES = {"host_expired"}
LIVE_EXECUTION_PHASES = {"thinking", "running"}
ATTENTION_PHASES = {"blocked"}
KNOWN_PHASES = {"thinking", "running", "blocked", "stalled", "needs_user", "idle", "finished"}
MANAGED_SESSION_LEASE_SOURCE = "engine_attached_lease"
MANAGED_CODEX_RUNTIME_SOURCES = {MANAGED_SESSION_LEASE_SOURCE, "codex_bridge", "codex_bridge_live", "codex_exec"}


def session_input_block_reason(db: Session, session_id: UUID | None) -> str | None:
    """Return the orthogonal disposition/run reason input cannot target this run."""
    if session_id is None:
        return None
    runtime_state = load_runtime_state_map(db, [session_id]).get(str(session_id))
    terminal_state = str(getattr(runtime_state, "terminal_state", "") or "").strip()
    if terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES:
        return "session_closed"
    if terminal_state in RUN_END_TERMINAL_STATES | RUN_TERMINAL_STATES:
        return "run_ended"
    if terminal_state in {"", "finished"} | UNVERIFIED_TERMINAL_STATES:
        return None
    return "run_ended"


def session_is_closed_for_input(db: Session, session_id: UUID | None) -> bool:
    """Compatibility predicate for whether the current run rejects new input."""
    return session_input_block_reason(db, session_id) is not None


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
    thread_id: UUID | None = None
    run_id: UUID | None = None
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
    if phase == "stalled":
        return "Stalled"
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


def _latest_applied_signal_at(state: Any) -> datetime | None:
    """Latest signal-clock timestamp of any applied signal kind.

    Mirrors the reducer's own recency composite (phase/progress/terminal) so
    the cross-lane merge ranks rows the way a single reducer would.
    """
    return _latest_timestamp(
        normalize_utc(getattr(state, "last_runtime_signal_at", None)),
        normalize_utc(getattr(state, "last_progress_at", None)),
        normalize_utc(getattr(state, "terminal_at", None)),
    )


def _runtime_state_newer_than(candidate: Any, existing: Any | None) -> bool:
    """Pick the runtime row whose latest signal OCCURRED most recently.

    The live and archive lanes run independent reducers, so `updated_at` is
    write-clock, not signal-clock: transcript ingest can stamp an archive row
    (progress-derived idle) in the same instant a fresher live phase_signal
    lands, and a write-clock comparison would let the stale phase win. Compare
    the composite signal clock first; fall back to write clock only when
    neither row has an applied signal.
    """
    if existing is None:
        return True
    candidate_signal_at = _latest_applied_signal_at(candidate)
    existing_signal_at = _latest_applied_signal_at(existing)
    if candidate_signal_at != existing_signal_at:
        if candidate_signal_at is None:
            return False
        if existing_signal_at is None:
            return True
        return candidate_signal_at > existing_signal_at
    # Equal signal clocks: each lane may have applied a different subset of
    # signals for the same instant. A row holding a semantic phase signal is
    # richer truth than a progress-only row.
    candidate_has_phase_signal = normalize_utc(getattr(candidate, "last_runtime_signal_at", None)) is not None
    existing_has_phase_signal = normalize_utc(getattr(existing, "last_runtime_signal_at", None)) is not None
    if candidate_has_phase_signal != existing_has_phase_signal:
        return candidate_has_phase_signal
    candidate_updated_at = normalize_utc(getattr(candidate, "updated_at", None))
    existing_updated_at = normalize_utc(getattr(existing, "updated_at", None))
    if candidate_updated_at != existing_updated_at:
        if candidate_updated_at is None:
            return False
        if existing_updated_at is None:
            return True
        return candidate_updated_at > existing_updated_at
    return int(getattr(candidate, "runtime_version", 0) or 0) > int(getattr(existing, "runtime_version", 0) or 0)


def _load_live_runtime_state_map(session_ids: list[UUID]) -> dict[str, LiveRuntimeState]:
    from zerg.database import live_catalog_enabled
    from zerg.database import live_store_configured

    if not live_store_configured():
        return {}
    if live_catalog_enabled():
        from zerg.services.catalog_facts import hydrate_catalog_row
        from zerg.services.catalog_facts import session_facts_map

        facts_by_session = session_facts_map([str(session_id) for session_id in session_ids])
        result: dict[str, LiveRuntimeState] = {}
        for session_id, facts in facts_by_session.items():
            row = hydrate_catalog_row(LiveRuntimeState, facts.get("runtime"))
            if row is not None:
                result[session_id] = row
        return result
    from zerg.database import get_live_session_factory

    live_session_factory = get_live_session_factory()
    if live_session_factory is None:
        return {}

    with live_session_factory() as live_db:
        rows = (
            live_db.query(LiveRuntimeState)
            .filter(LiveRuntimeState.session_id.in_(session_ids))
            .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
            .all()
        )
        state_by_session: dict[str, LiveRuntimeState] = {}
        for row in rows:
            if row.session_id is None:
                continue
            key = str(row.session_id)
            state_by_session.setdefault(key, row)
        return state_by_session


def load_runtime_state_map(db: Session, session_ids: list[UUID]) -> dict[str, SessionRuntimeState | LiveRuntimeState]:
    if not session_ids:
        return {}
    from zerg.database import live_catalog_enabled

    if live_catalog_enabled():
        return _load_live_runtime_state_map(session_ids)

    rows = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id.in_(session_ids))
        .order_by(SessionRuntimeState.updated_at.desc(), SessionRuntimeState.runtime_version.desc())
        .all()
    )

    state_by_session: dict[str, SessionRuntimeState | LiveRuntimeState] = {}
    for row in rows:
        if row.session_id is None:
            continue
        key = str(row.session_id)
        state_by_session.setdefault(key, row)
    for key, live_row in _load_live_runtime_state_map(session_ids).items():
        if _runtime_state_newer_than(live_row, state_by_session.get(key)):
            state_by_session[key] = live_row
    return state_by_session


def _is_bridge_transcript_event(event: RuntimeEventIngest) -> bool:
    payload = event.payload or {}
    return (
        (event.provider or "").strip().lower() == "codex"
        and (event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


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
            thread_id=event.thread_id,
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


def ingest_live_runtime_events(db: Session, events: list[RuntimeEventIngest]) -> RuntimeEventBatchResult:
    """Materialize runtime state into the hot Live Store without archive side effects.

    The live lane intentionally does not write SessionObservation rows, pause
    requests, notification ledgers, SessionRun, or AgentSession lifecycle
    fields. Those remain archive responsibilities. This reducer exists so
    user-visible phase/control freshness can update before archive storage
    catches up.
    """

    from zerg.services.live_session_state import touch_live_sessions_from_runtime_events

    updated_runtime_keys: list[str] = []
    for event in events:
        preview_candidate = live_preview_candidate_from_runtime_event(
            event,
            observation_id=f"live:{event.source}:{event.dedupe_key}",
        )
        if preview_candidate is not None:
            upsert_live_session_live_preview(db, preview_candidate)
        outcome = _apply_runtime_event(
            db,
            event,
            state_model=LiveRuntimeState,
            archive_side_effects=False,
        )
        _record_managed_codex_runtime_observation(event, f"live_{outcome}")
        if outcome == "applied" and event.runtime_key not in updated_runtime_keys:
            updated_runtime_keys.append(event.runtime_key)

    # Runtime signals are liveness evidence for the active-session candidate
    # index; without this, unmanaged/Shadow sessions that never hold a managed
    # lease vanish from the active list when the Live Store is configured.
    touch_live_sessions_from_runtime_events(db, events)

    return RuntimeEventBatchResult(
        accepted=len(events),
        duplicates=0,
        updated_runtime_keys=updated_runtime_keys,
    )


def runtime_event_from_observation(observation) -> RuntimeEventIngest | None:
    if observation.kind != OBS_KIND_RUNTIME_SIGNAL:
        return None
    payload = _observation_payload(observation)
    kind = str(payload.get("kind") or "").strip()
    valid_kinds = {
        "phase_signal",
        "progress_signal",
        "terminal_signal",
        "binding_signal",
        "pause_request",
        "pause_resolution",
    }
    if kind not in valid_kinds:
        raise ValueError(f"runtime_signal observation {observation.observation_id} has invalid kind {kind!r}")
    return RuntimeEventIngest(
        runtime_key=observation.runtime_key
        or runtime_key_for_session(
            observation.provider,
            str(observation.session_id or "unknown"),
        ),
        session_id=observation.session_id,
        thread_id=observation.thread_id,
        run_id=coerce_session_uuid(payload.get("run_id")),
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
    raw = decode_observation_payload_json(observation)
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


def _exit_status_for_terminal(terminal_state: str, payload: Mapping[str, Any]) -> str:
    explicit = _optional_payload_str(payload.get("exit_status"))
    if explicit:
        return explicit[:64]
    exit_code = _optional_payload_int(payload.get("exit_code"))
    if exit_code is not None:
        return f"exit_{exit_code}"[:64]
    return terminal_state[:64]


def _apply_run_terminal_event(
    db: Session,
    *,
    event: RuntimeEventIngest,
    state: SessionRuntimeState | LiveRuntimeState,
    occurred_at: datetime,
) -> bool:
    run_id = event.run_id or state.run_id
    if run_id is None:
        return False
    live_lane = isinstance(state, LiveRuntimeState)
    run_model = LiveSessionRun if live_lane else SessionRun
    connection_model = LiveSessionConnection if live_lane else SessionConnection
    run = db.query(run_model).filter(run_model.id == str(run_id) if live_lane else run_model.id == run_id).first()
    if run is None:
        return False
    if event.session_id is not None:
        if live_lane:
            owned = (
                db.query(LiveSessionThread.id)
                .filter(LiveSessionThread.id == str(run.thread_id))
                .filter(LiveSessionThread.session_id == str(event.session_id))
                .first()
            )
        else:
            from zerg.models.agents import SessionThread

            owned = (
                db.query(SessionThread.id)
                .filter(SessionThread.id == run.thread_id)
                .filter(SessionThread.session_id == event.session_id)
                .first()
            )
        if owned is None:
            return False
    terminal_state = str((event.payload or {}).get("terminal_state") or "finished").strip() or "finished"
    run_ended_at = normalize_utc(run.ended_at)
    changed = False
    if run_ended_at is None:
        run.ended_at = occurred_at
        run.exit_status = _exit_status_for_terminal(terminal_state, event.payload or {})
        connection_released_at = occurred_at
        changed = True
    else:
        connection_released_at = run_ended_at
    for conn in (
        db.query(connection_model)
        .filter(connection_model.run_id == run.id)
        .filter(connection_model.state.in_(("attached", "degraded", "detached")))
        .all()
    ):
        changed = True
        conn.state = "ended"
        conn.released_at = connection_released_at
        conn.last_health_at = connection_released_at
        conn.can_send_input = 0
        conn.can_interrupt = 0
        conn.can_terminate = 0
        conn.can_tail_output = 0
        conn.can_resume = 0
    return changed


def _state_snapshot(state: SessionRuntimeState | None) -> tuple[Any, ...] | None:
    if state is None:
        return None
    return (
        state.session_id,
        state.thread_id,
        state.run_id,
        state.provider,
        state.device_id,
        state.phase,
        state.phase_source,
        state.active_tool,
        normalize_utc(state.phase_started_at),
        normalize_utc(state.execution_started_at),
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


def _ensure_state(
    db: Session,
    event: RuntimeEventIngest,
    *,
    state_model: type[SessionRuntimeState] | type[LiveRuntimeState] = SessionRuntimeState,
    archive_side_effects: bool = True,
) -> SessionRuntimeState | LiveRuntimeState:
    state = db.query(state_model).filter(state_model.runtime_key == event.runtime_key).first()
    if state is not None:
        return state

    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    thread_id = event.thread_id
    if thread_id is None and archive_side_effects and event.session_id is not None:
        from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

        thread_id = ensure_thread_id_for_session(db, event.session_id)
    state = state_model(
        runtime_key=event.runtime_key,
        session_id=event.session_id,
        thread_id=thread_id,
        run_id=event.run_id,
        provider=event.provider,
        device_id=event.device_id,
        phase=(event.phase or "idle").strip() or "idle",
        phase_source="fallback",
        active_tool=None,
        phase_started_at=occurred_at,
        execution_started_at=occurred_at if (event.phase or "").strip() in LIVE_EXECUTION_PHASES else None,
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


def _apply_runtime_event(
    db: Session,
    event: RuntimeEventIngest,
    *,
    state_model: type[SessionRuntimeState] | type[LiveRuntimeState] = SessionRuntimeState,
    archive_side_effects: bool = True,
) -> RuntimeEventApplyOutcome:
    if event.kind in {"pause_request", "pause_resolution"}:
        if not archive_side_effects:
            from zerg.services.session_pause_requests import build_pause_runtime_projection
            from zerg.services.session_pause_requests import pause_runtime_request_key

            state = _ensure_state(db, event, state_model=state_model, archive_side_effects=False)
            payload = event.payload if isinstance(event.payload, dict) else {}
            occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
            interaction_updated_at = normalize_utc(state.pending_interaction_updated_at)
            if interaction_updated_at is not None and (
                occurred_at < interaction_updated_at or (occurred_at == interaction_updated_at and event.kind == "pause_request")
            ):
                return "ignored"
            if event.kind == "pause_request":
                projection = build_pause_runtime_projection(event)
                state.pending_interaction_id = projection["request_key"]
                state.pending_interaction_kind = str(payload.get("kind") or "structured_question").strip()
                state.pending_interaction_opened_at = occurred_at
                state.pending_interaction_can_respond = int(bool(payload.get("can_respond")))
                state.pending_interaction_projection_json = projection
            else:
                target_id = pause_runtime_request_key(event)
                if state.pending_interaction_id is not None and target_id != state.pending_interaction_id:
                    return "ignored"
                state.pending_interaction_id = None
                state.pending_interaction_kind = None
                state.pending_interaction_opened_at = None
                state.pending_interaction_can_respond = 0
                state.pending_interaction_projection_json = None
            state.pending_interaction_updated_at = occurred_at
            state.runtime_version = int(state.runtime_version or 0) + 1
            state.updated_at = max(normalize_utc(state.updated_at) or occurred_at, occurred_at)
            db.flush()
            return "applied"
        from zerg.services.session_pause_requests import apply_pause_runtime_event

        return "applied" if apply_pause_runtime_event(db, event) else "ignored"

    state = _ensure_state(
        db,
        event,
        state_model=state_model,
        archive_side_effects=archive_side_effects,
    )
    before = _state_snapshot(state)
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    pause_changed = False

    if state.terminal_state == "session_ended":
        incoming_terminal_state = str((event.payload or {}).get("terminal_state") or "").strip()
        if event.kind == "terminal_signal" and incoming_terminal_state in RUN_TERMINAL_STATES:
            changed = _apply_run_terminal_event(db, event=event, state=state, occurred_at=occurred_at)
            if changed:
                db.flush()
                return "applied"
            return "protected_session_ended"
        if event.kind != "terminal_signal" or incoming_terminal_state != "session_ended":
            return "protected_session_ended"

    if event.session_id is not None and state.session_id != event.session_id:
        state.session_id = event.session_id
        if event.thread_id is not None:
            state.thread_id = event.thread_id
        elif archive_side_effects:
            from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

            state.thread_id = ensure_thread_id_for_session(db, event.session_id)
    elif event.thread_id is not None and state.thread_id != event.thread_id:
        state.thread_id = event.thread_id
    if event.run_id is not None and state.run_id != event.run_id:
        state.run_id = event.run_id
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
        # Preserve rolling-producer vocabulary exactly. Unknown provider phases
        # project as activity unknown; they must never be rewritten to idle.
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
        previous_phase = (state.phase or "idle").strip() or "idle"
        state.phase = next_phase
        event_source = str(event.source or "").strip()
        state.phase_source = event_source if event_source in MANAGED_CODEX_RUNTIME_SOURCES else "semantic"
        if next_phase in {"running", "blocked"}:
            state.active_tool = next_active_tool
        else:
            state.active_tool = None
        if next_phase in LIVE_EXECUTION_PHASES:
            if previous_phase not in LIVE_EXECUTION_PHASES or state.execution_started_at is None:
                state.execution_started_at = occurred_at
        elif next_phase not in {"idle", "needs_user"}:
            state.execution_started_at = None
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
        # Event-driven provider phases can close a pending question once the
        # provider continues. Do not replace this with heartbeat-style signals
        # without preserving transcript-derived AskUserQuestion waits.
        if (
            archive_side_effects
            and next_phase in LIVE_EXECUTION_PHASES
            and not bool((event.payload or {}).get("pause_request_still_pending"))
        ):
            from zerg.services.session_pause_requests import resolve_pending_pause_requests_for_runtime

            pause_changed = (
                resolve_pending_pause_requests_for_runtime(
                    db,
                    runtime_key=event.runtime_key,
                    status="resolved",
                    occurred_at=occurred_at,
                    response_text="Provider continued.",
                )
                > 0
            )

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
            state.execution_started_at = None
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
            state.execution_started_at = None
        if state.phase_source in {"fallback", "progress"}:
            state.phase_source = "progress"

    elif event.kind == "terminal_signal":
        terminal_state = str((event.payload or {}).get("terminal_state") or "finished").strip() or "finished"
        existing_terminal_state = str(state.terminal_state or "").strip()
        same_run_terminal_replay = (
            terminal_state in RUN_TERMINAL_STATES
            and existing_terminal_state in RUN_TERMINAL_STATES
            and state.run_id is not None
            and (event.run_id is None or event.run_id == state.run_id)
        )
        if same_run_terminal_replay:
            if not archive_side_effects:
                return "ignored"
            return "applied" if _apply_run_terminal_event(db, event=event, state=state, occurred_at=occurred_at) else "ignored"
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
        state.execution_started_at = None
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
            session_model = AgentSession if archive_side_effects else LiveSessionCatalog
            id_column = session_model.id if archive_side_effects else session_model.session_id
            session_key = event.session_id if archive_side_effects else str(event.session_id)
            session = db.query(session_model).filter(id_column == session_key).first()
            if session is not None and session.closed_at is None:
                session.closed_at = occurred_at
                session.close_reason = terminal_state
        if archive_side_effects and terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES and event.session_id is not None:
            from zerg.services.session_pause_requests import expire_pending_pause_requests_for_session

            pause_changed = (
                expire_pending_pause_requests_for_session(
                    db,
                    session_id=event.session_id,
                    occurred_at=occurred_at,
                    response_text=f"Session ended: {terminal_state}.",
                )
                > 0
            )
        elif archive_side_effects:
            from zerg.services.session_pause_requests import expire_pending_pause_requests_for_runtime

            pause_changed = (
                expire_pending_pause_requests_for_runtime(
                    db,
                    runtime_key=event.runtime_key,
                    occurred_at=occurred_at,
                    response_text=f"Runtime ended: {terminal_state}.",
                )
                > 0
            )
        if terminal_state in RUN_END_TERMINAL_STATES | RUN_TERMINAL_STATES:
            _apply_run_terminal_event(db, event=event, state=state, occurred_at=occurred_at)

    elif event.kind == "binding_signal":
        if event.session_id is not None:
            state.session_id = event.session_id

    after = _state_snapshot(state)
    if after != before:
        state.runtime_version = int(state.runtime_version or 0) + 1
        db.flush()
        return "applied"
    if pause_changed:
        db.flush()
        return "applied"
    return "ignored"
