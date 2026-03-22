"""Runtime event ingestion and materialized runtime state.

This module owns the provider-agnostic runtime reducer used by Timeline.
"""

from __future__ import annotations

import json
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

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionRuntimeEvent
from zerg.models.agents import SessionRuntimeState

RuntimeEventKind = Literal["phase_signal", "progress_signal", "terminal_signal", "binding_signal"]
RuntimePhase = Literal["thinking", "running", "blocked", "needs_user", "idle", "finished"]

PHASE_FRESHNESS = {
    "thinking": timedelta(seconds=90),
    "running": timedelta(minutes=10),
    "idle": timedelta(minutes=10),
    "blocked": timedelta(hours=24),
    "needs_user": timedelta(hours=24),
}
INFERRED_PROGRESS_WINDOW = timedelta(minutes=5)
LIVE_EXECUTION_PHASES = {"thinking", "running"}
ATTENTION_PHASES = {"blocked", "needs_user"}
KNOWN_PHASES = {"thinking", "running", "blocked", "needs_user", "idle", "finished"}
PRESENCE_STALE_THRESHOLD = timedelta(minutes=10)


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_timestamp(*values: datetime | None) -> datetime | None:
    normalized = [_normalize_utc(value) for value in values]
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
    freshness_expires_at = _normalize_utc(state.freshness_expires_at)
    if freshness_expires_at is not None and freshness_expires_at > now:
        return "live"

    last_progress_at = _normalize_utc(state.last_progress_at)
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
    if ended_at is None:
        return "idle"
    return "completed"


def build_runtime_view(
    *,
    state: SessionRuntimeState,
    session: AgentSession,
    now: datetime,
) -> SessionRuntimeView:
    normalized_now = _normalize_utc(now) or datetime.now(timezone.utc)
    confidence = _confidence_for_state(state, now=normalized_now)
    runtime_phase = (state.phase or "idle").strip() or "idle"
    terminal_state = (state.terminal_state or "").strip() or None
    phase_source = (state.phase_source or "fallback").strip() or "fallback"
    active_tool = (state.active_tool or "").strip() or None
    timeline_anchor_at = _normalize_utc(state.timeline_anchor_at) or _normalize_utc(session.started_at) or normalized_now
    status = _status_for_state(
        phase=runtime_phase,
        confidence=confidence,
        terminal_state=terminal_state,
        ended_at=_normalize_utc(session.ended_at),
    )
    presence_state: str | None = None
    presence_tool: str | None = None
    presence_updated_at = _normalize_utc(state.last_runtime_signal_at)

    if confidence == "live" and runtime_phase in KNOWN_PHASES:
        presence_state = runtime_phase
        presence_tool = active_tool if runtime_phase in {"running", "blocked"} else None

    exposed_runtime_phase = runtime_phase
    if confidence == "inferred" and phase_source == "progress":
        exposed_runtime_phase = ""

    return SessionRuntimeView(
        runtime_phase=exposed_runtime_phase or None,
        phase_started_at=_normalize_utc(state.phase_started_at),
        last_progress_at=_normalize_utc(state.last_progress_at),
        runtime_source=phase_source,
        terminal_state=terminal_state,
        runtime_version=int(state.runtime_version or 0),
        status=status,
        presence_state=presence_state,
        presence_tool=presence_tool,
        presence_updated_at=presence_updated_at,
        last_live_at=_normalize_utc(state.last_live_at) or _normalize_utc(state.last_progress_at) or presence_updated_at,
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
    presence: SessionPresence | None,
    now: datetime,
) -> SessionRuntimeView:
    normalized_now = _normalize_utc(now) or datetime.now(timezone.utc)
    started_at = _normalize_utc(session.started_at) or normalized_now
    ended_at = _normalize_utc(session.ended_at)
    progress_at = _normalize_utc(last_activity_at) or ended_at or started_at
    presence_updated_at = _normalize_utc(presence.updated_at if presence else None)

    presence_state: str | None = None
    presence_tool: str | None = None
    if presence is not None and presence_updated_at is not None and (normalized_now - presence_updated_at) < PRESENCE_STALE_THRESHOLD:
        presence_state = presence.state
        presence_tool = presence.tool_name

    timeline_anchor_at = _latest_timestamp(progress_at, presence_updated_at, started_at) or normalized_now
    last_live_at = presence_updated_at
    confidence: str | None = None

    if presence_state in {"thinking", "running", "needs_user", "blocked"}:
        status = "working" if presence_state in LIVE_EXECUTION_PHASES else "active"
        confidence = "live"
    elif presence_state == "idle":
        status = "idle"
        confidence = "live"
    elif ended_at is None:
        if (normalized_now - progress_at) <= INFERRED_PROGRESS_WINDOW:
            status = "active"
            confidence = "inferred"
            last_live_at = last_live_at or progress_at
        else:
            status = "idle"
            confidence = "stale" if presence_updated_at is not None else None
    else:
        status = "completed"
        confidence = "stale" if presence_updated_at is not None else None

    runtime_phase = presence_state or ("finished" if ended_at is not None else "idle")
    return SessionRuntimeView(
        runtime_phase=runtime_phase,
        phase_started_at=presence_updated_at or progress_at,
        last_progress_at=progress_at,
        runtime_source=("semantic" if presence_state is not None else ("progress" if confidence == "inferred" else "fallback")),
        terminal_state=("finished" if ended_at is not None and status == "completed" else None),
        runtime_version=0,
        status=status,
        presence_state=presence_state,
        presence_tool=presence_tool,
        presence_updated_at=presence_updated_at,
        last_live_at=last_live_at,
        display_phase=_display_phase_for_state(
            phase=runtime_phase,
            active_tool=presence_tool,
            confidence=confidence or "stale",
            terminal_state=("finished" if ended_at is not None and status == "completed" else None),
            status=status,
        ),
        active_tool=presence_tool,
        confidence=confidence,
        timeline_anchor_at=timeline_anchor_at,
    )


def should_include_runtime_view(
    *,
    session: AgentSession,
    runtime_view: SessionRuntimeView | None,
) -> bool:
    return runtime_view is not None and (
        session.ended_at is None
        or runtime_view.presence_updated_at is not None
        or runtime_view.last_live_at is not None
        or runtime_view.runtime_source not in {None, "fallback"}
    )


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
                occurred_at=_normalize_utc(event.occurred_at),
                freshness_ms=event.freshness_ms,
                dedupe_key=event.dedupe_key,
                payload_json=payload_json,
            )
            .on_conflict_do_nothing(index_elements=["source", "dedupe_key"])
        )
        result = db.execute(insert_stmt)
        if not result.rowcount:
            duplicates += 1
            continue

        accepted += 1
        if _apply_runtime_event(db, event) and event.runtime_key not in updated_runtime_keys:
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
        _normalize_utc(state.phase_started_at),
        _normalize_utc(state.last_runtime_signal_at),
        _normalize_utc(state.last_progress_at),
        _normalize_utc(state.last_live_at),
        _normalize_utc(state.timeline_anchor_at),
        _normalize_utc(state.freshness_expires_at),
        state.terminal_state,
        _normalize_utc(state.terminal_at),
    )


def _ensure_state(db: Session, event: RuntimeEventIngest) -> SessionRuntimeState:
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == event.runtime_key).first()
    if state is not None:
        return state

    occurred_at = _normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
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


def _apply_runtime_event(db: Session, event: RuntimeEventIngest) -> bool:
    state = _ensure_state(db, event)
    before = _state_snapshot(state)
    occurred_at = _normalize_utc(event.occurred_at) or datetime.now(timezone.utc)

    if event.session_id is not None and state.session_id != event.session_id:
        state.session_id = event.session_id
    if event.provider and state.provider != event.provider:
        state.provider = event.provider
    if event.device_id is not None and state.device_id != event.device_id:
        state.device_id = event.device_id

    if event.kind == "phase_signal":
        latest_phase_signal_at = _latest_timestamp(state.last_runtime_signal_at, state.terminal_at)
        if latest_phase_signal_at is not None and occurred_at < latest_phase_signal_at:
            return False
        next_phase = (event.phase or state.phase or "idle").strip() or "idle"
        if next_phase not in KNOWN_PHASES:
            next_phase = "idle"
        if state.phase != next_phase:
            if _phase_reanchors(state.phase, next_phase):
                state.timeline_anchor_at = occurred_at
            state.phase_started_at = occurred_at
        state.phase = next_phase
        state.phase_source = "semantic"
        state.active_tool = event.tool_name if next_phase in {"running", "blocked"} else None
        state.last_runtime_signal_at = occurred_at
        state.last_live_at = occurred_at
        freshness_ms = event.freshness_ms or phase_freshness_ms(next_phase)
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
            return False
        state.last_progress_at = occurred_at
        state.last_live_at = occurred_at
        state.timeline_anchor_at = occurred_at
        if state.terminal_state is not None and (
            _normalize_utc(state.terminal_at) is None or occurred_at >= _normalize_utc(state.terminal_at)
        ):
            state.terminal_state = None
            state.terminal_at = None
        freshness_expires_at = _normalize_utc(state.freshness_expires_at)
        if freshness_expires_at is None or freshness_expires_at <= occurred_at or state.phase not in KNOWN_PHASES:
            if state.phase not in ATTENTION_PHASES:
                if state.phase != "running":
                    state.phase = "running"
                    state.phase_started_at = occurred_at
            state.phase_source = "progress"

    elif event.kind == "terminal_signal":
        latest_terminal_related_at = _latest_timestamp(state.last_runtime_signal_at, state.terminal_at)
        if latest_terminal_related_at is not None and occurred_at < latest_terminal_related_at:
            return False
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
        phase_started_at = _normalize_utc(state.phase_started_at)
        if phase_started_at is None or phase_started_at < occurred_at:
            state.phase_started_at = occurred_at

    elif event.kind == "binding_signal":
        if event.session_id is not None:
            state.session_id = event.session_id

    after = _state_snapshot(state)
    if after != before:
        state.runtime_version = int(state.runtime_version or 0) + 1
        db.flush()
        return True
    return False
