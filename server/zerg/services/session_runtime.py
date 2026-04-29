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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.metrics import managed_codex_bridge_freshness_total
from zerg.metrics import managed_codex_liveness_invariant_sessions
from zerg.metrics import managed_codex_runtime_events_total
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeEvent
from zerg.models.agents import SessionRuntimeState
from zerg.session_execution_home import ManagedSessionTransport
from zerg.session_execution_home import SessionExecutionHome
from zerg.utils.time import normalize_utc

RuntimeEventKind = Literal["phase_signal", "progress_signal", "terminal_signal", "binding_signal"]
RuntimeEventApplyOutcome = Literal["applied", "ignored", "protected_session_ended"]

PHASE_FRESHNESS = {
    "thinking": timedelta(seconds=90),
    "running": timedelta(minutes=10),
    "idle": timedelta(minutes=10),
    "blocked": timedelta(hours=24),
    "needs_user": timedelta(hours=24),
}
MANAGED_CODEX_FRESHNESS = timedelta(minutes=15)
INFERRED_PROGRESS_WINDOW = timedelta(minutes=5)
LIVE_EXECUTION_PHASES = {"thinking", "running"}
ATTENTION_PHASES = {"blocked", "needs_user"}
KNOWN_PHASES = {"thinking", "running", "blocked", "needs_user", "idle", "finished"}
MANAGED_CODEX_RUNTIME_SOURCES = {"engine_attached_lease", "codex_bridge"}
MANAGED_CODEX_INVARIANTS = ("ended_without_session_ended", "short_freshness")


def _latest_timestamp(*values: datetime | None) -> datetime | None:
    normalized = [normalize_utc(value) for value in values]
    present = [value for value in normalized if value is not None]
    return max(present) if present else None


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


def _is_managed_codex_runtime_event(event: RuntimeEventIngest) -> bool:
    provider = (event.provider or "").strip().lower()
    source = (event.source or "").strip().lower()
    return provider == "codex" and source in MANAGED_CODEX_RUNTIME_SOURCES


def _managed_codex_source_label(source: str | None) -> str:
    normalized = (source or "").strip().lower()
    return normalized if normalized in MANAGED_CODEX_RUNTIME_SOURCES else "other"


def _record_managed_codex_runtime_event(event: RuntimeEventIngest, outcome: str) -> None:
    if not _is_managed_codex_runtime_event(event):
        return
    managed_codex_runtime_events_total.labels(
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
    runtime_phase: str | None
    phase_started_at: datetime | None
    last_progress_at: datetime | None
    runtime_source: str
    terminal_state: str | None
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


def _confidence_for_state(state: SessionRuntimeState, *, now: datetime) -> str:
    freshness_expires_at = normalize_utc(state.freshness_expires_at)
    if freshness_expires_at is not None and freshness_expires_at > now:
        return "live"

    last_progress_at = normalize_utc(state.last_progress_at)
    if state.terminal_state is None and last_progress_at is not None and (now - last_progress_at) <= INFERRED_PROGRESS_WINDOW:
        return "inferred"

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
    if confidence == "inferred":
        return "Recent progress"
    if phase == "running":
        return f"Running {active_tool}" if active_tool else "Running"
    if phase == "thinking":
        return "Thinking"
    if phase == "needs_user":
        return "Needs you"
    if phase == "blocked":
        return f"Blocked on {active_tool}" if active_tool else "Needs permission"
    if phase == "idle":
        return "Idle"
    return "Recent"


def _status_for_state(
    *,
    phase: str,
    confidence: str,
    terminal_state: str | None,
    ended_at: datetime | None,
) -> str:
    # Phase 1 of session-liveness-honesty: do NOT return "completed" just
    # because the session has a non-null `ended_at`. For unmanaged ingest
    # sources, `ended_at` is just the last event timestamp, not a terminal
    # signal. Only `terminal_state` (or phase=="finished", which the reducer
    # only sets alongside a real terminal_state) counts as closed.
    if terminal_state is not None or phase == "finished":
        return "completed"
    if confidence == "inferred":
        return "active"
    if confidence == "live":
        if phase in LIVE_EXECUTION_PHASES:
            return "working"
        if phase in ATTENTION_PHASES:
            return "active"
        return "idle"
    return "idle"


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

    if confidence == "live" and runtime_phase in KNOWN_PHASES:
        presence_state = runtime_phase
        presence_tool = active_tool if runtime_phase in {"running", "blocked"} else None

    exposed_runtime_phase = runtime_phase
    if confidence == "inferred" and phase_source == "progress":
        exposed_runtime_phase = ""

    return SessionRuntimeView(
        runtime_phase=exposed_runtime_phase or None,
        phase_started_at=normalize_utc(state.phase_started_at),
        last_progress_at=normalize_utc(state.last_progress_at),
        runtime_source=phase_source,
        terminal_state=terminal_state,
        runtime_version=int(state.runtime_version or 0),
        status=status,
        presence_state=presence_state,
        presence_tool=presence_tool,
        presence_updated_at=presence_updated_at,
        last_live_at=normalize_utc(state.last_live_at) or normalize_utc(state.last_progress_at) or presence_updated_at,
        display_phase=_display_phase_for_state(
            phase=runtime_phase,
            active_tool=active_tool,
            confidence=confidence,
            terminal_state=terminal_state,
            status=status,
        ),
        active_tool=active_tool,
        confidence=confidence,
        timeline_anchor_at=timeline_anchor_at,
    )


def build_fallback_runtime_view(
    *,
    session: AgentSession,
    last_activity_at: datetime | None,
    now: datetime,
) -> SessionRuntimeView:
    # Phase 1 of session-liveness-honesty: parser-derived `ended_at` is a
    # last-activity timestamp for unmanaged sessions, not a terminal signal.
    # Only promote to terminal when we have explicit truth in
    # `session.terminal_state` (from a real terminal_signal ingest).
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
        terminal_state: str | None = explicit_terminal
    else:
        if (normalized_now - progress_at) <= INFERRED_PROGRESS_WINDOW:
            status = "active"
            confidence = "inferred"
            last_live_at = progress_at
        else:
            status = "idle"
        runtime_phase = "idle"
        terminal_state = None

    return SessionRuntimeView(
        runtime_phase=runtime_phase,
        phase_started_at=progress_at,
        last_progress_at=progress_at,
        runtime_source=("progress" if confidence == "inferred" else "fallback"),
        terminal_state=terminal_state,
        runtime_version=0,
        status=status,
        presence_state=None,
        presence_tool=None,
        presence_updated_at=None,
        last_live_at=last_live_at,
        display_phase=_display_phase_for_state(
            phase=runtime_phase,
            active_tool=None,
            confidence=confidence or "stale",
            terminal_state=terminal_state,
            status=status,
        ),
        active_tool=None,
        confidence=confidence,
        timeline_anchor_at=timeline_anchor_at,
    )


def should_include_runtime_view(
    *,
    session: AgentSession,
    runtime_view: SessionRuntimeView | None,
) -> bool:
    # Phase 1: `ended_at` alone is not a closure signal for unmanaged sessions.
    # Only an explicit terminal_state suppresses the runtime view's activity
    # hints; otherwise keep the view so the client can render "Active
    # (inferred)" rather than falling back to a silent card.
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


def _managed_codex_session_ids(db: Session) -> list[UUID]:
    rows = (
        db.query(AgentSession.id)
        .filter(AgentSession.provider == "codex")
        .filter(
            (AgentSession.execution_home == SessionExecutionHome.MANAGED_LOCAL.value)
            | (AgentSession.managed_transport == ManagedSessionTransport.CODEX_APP_SERVER.value)
        )
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
    latest_managed_event_by_runtime_key: dict[str, SessionRuntimeEvent] = {}
    runtime_keys = [state.runtime_key for state in states]
    if runtime_keys:
        latest_managed_events = (
            db.query(SessionRuntimeEvent)
            .filter(SessionRuntimeEvent.runtime_key.in_(runtime_keys))
            .filter(SessionRuntimeEvent.provider == "codex")
            .filter(SessionRuntimeEvent.source.in_(MANAGED_CODEX_RUNTIME_SOURCES))
            .filter(SessionRuntimeEvent.kind == "phase_signal")
            .order_by(
                SessionRuntimeEvent.runtime_key.asc(),
                SessionRuntimeEvent.occurred_at.desc(),
                SessionRuntimeEvent.id.desc(),
            )
            .all()
        )
        for event in latest_managed_events:
            latest_managed_event_by_runtime_key.setdefault(event.runtime_key, event)

    for state in states:
        last_signal_at = normalize_utc(state.last_runtime_signal_at)
        freshness_expires_at = normalize_utc(state.freshness_expires_at)
        if last_signal_at is None or freshness_expires_at is None:
            continue
        latest_managed_event = latest_managed_event_by_runtime_key.get(state.runtime_key)
        if latest_managed_event is None:
            continue
        latest_managed_event_at = normalize_utc(latest_managed_event.occurred_at)
        if latest_managed_event_at is None or latest_managed_event_at != last_signal_at:
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
        payload_json = json.dumps(event.payload or {}, sort_keys=True, separators=(",", ":"))
        insert_stmt = (
            sqlite_insert(SessionRuntimeEvent)
            .values(
                runtime_key=event.runtime_key,
                session_id=event.session_id,
                provider=event.provider,
                device_id=event.device_id,
                source=event.source,
                kind=event.kind,
                phase=event.phase,
                tool_name=event.tool_name,
                occurred_at=normalize_utc(event.occurred_at),
                freshness_ms=event.freshness_ms,
                dedupe_key=event.dedupe_key,
                payload_json=payload_json,
            )
            .on_conflict_do_nothing(index_elements=["source", "dedupe_key"])
        )
        result = db.execute(insert_stmt)
        if not result.rowcount:
            duplicates += 1
            _record_managed_codex_runtime_event(event, "duplicate")
            continue

        accepted += 1
        outcome = _apply_runtime_event(db, event)
        _record_managed_codex_runtime_event(event, outcome)
        if outcome == "applied" and event.runtime_key not in updated_runtime_keys:
            updated_runtime_keys.append(event.runtime_key)

    return RuntimeEventBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        updated_runtime_keys=updated_runtime_keys,
    )


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
        normalize_utc(state.terminal_at),
    )


def _ensure_state(db: Session, event: RuntimeEventIngest) -> SessionRuntimeState:
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).first()
    if state is not None:
        return state

    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    state = SessionRuntimeState(
        runtime_key=event.runtime_key,
        session_id=event.session_id,
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
        if latest_phase_signal_at is not None and occurred_at < latest_phase_signal_at:
            return "ignored"
        next_phase = (event.phase or state.phase or "idle").strip() or "idle"
        if next_phase not in KNOWN_PHASES:
            next_phase = "idle"
        if state.phase != next_phase:
            if _phase_reanchors(state.phase, next_phase):
                state.timeline_anchor_at = occurred_at
            state.phase_started_at = occurred_at
        state.phase = next_phase
        state.phase_source = "semantic"
        if next_phase in {"running", "blocked"}:
            # Blocked-with-no-tool means "still blocked on the same tool as
            # last time" — keep the prior active_tool instead of dropping
            # it. Running signals always carry the tool explicitly.
            state.active_tool = event.tool_name or (state.active_tool if next_phase == "blocked" else None)
        else:
            state.active_tool = None
        state.last_runtime_signal_at = occurred_at
        state.last_live_at = occurred_at
        freshness_ms = _phase_signal_freshness_ms(event, next_phase)
        state.freshness_expires_at = occurred_at + timedelta(milliseconds=freshness_ms) if freshness_ms is not None else None
        state.terminal_state = None
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
        state.last_live_at = occurred_at
        state.timeline_anchor_at = occurred_at
        if state.terminal_state is not None and (
            normalize_utc(state.terminal_at) is None or occurred_at >= normalize_utc(state.terminal_at)
        ):
            state.terminal_state = None
            state.terminal_at = None
        freshness_expires_at = normalize_utc(state.freshness_expires_at)
        if freshness_expires_at is None or freshness_expires_at <= occurred_at or state.phase not in KNOWN_PHASES:
            if state.phase not in ATTENTION_PHASES:
                if state.phase != "running":
                    state.phase = "running"
                    state.phase_started_at = occurred_at
            state.phase_source = "progress"

    elif event.kind == "terminal_signal":
        latest_terminal_related_at = _latest_timestamp(
            state.last_runtime_signal_at,
            state.last_progress_at,
            state.terminal_at,
        )
        if latest_terminal_related_at is not None and occurred_at < latest_terminal_related_at:
            return "ignored"
        terminal_state = str((event.payload or {}).get("terminal_state") or "finished").strip() or "finished"
        state.phase = "finished"
        state.phase_source = "semantic"
        state.active_tool = None
        state.last_runtime_signal_at = occurred_at
        state.last_live_at = occurred_at
        state.freshness_expires_at = occurred_at
        state.terminal_state = terminal_state
        state.terminal_at = occurred_at
        state.timeline_anchor_at = occurred_at
        phase_started_at = normalize_utc(state.phase_started_at)
        if phase_started_at is None or phase_started_at < occurred_at:
            state.phase_started_at = occurred_at
        if terminal_state == "session_ended" and event.session_id is not None:
            session = db.query(AgentSession).filter(AgentSession.id == event.session_id).first()
            if session is not None:
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
