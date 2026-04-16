"""Canonical session-turn timing helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import SessionTurn
from zerg.services.write_serializer import get_write_serializer

SESSION_TURN_SOURCE_MANAGED_LIVE = "managed_live"
SESSION_TURN_SOURCE_IMPORTED_RECONSTRUCTED = "imported_reconstructed"
SESSION_TURN_SOURCE_IMPORTED_PARTIAL = "imported_partial"

SESSION_TURN_CONFIDENCE_EXACT = "exact"
SESSION_TURN_CONFIDENCE_PARTIAL = "partial"
SESSION_TURN_CONFIDENCE_INFERRED = "inferred"

SESSION_TURN_STATE_CREATED = "created"
SESSION_TURN_STATE_SEND_ACCEPTED = "send_accepted"
SESSION_TURN_STATE_ACTIVE = "active"
SESSION_TURN_STATE_TERMINAL = "terminal"
SESSION_TURN_STATE_DURABLE = "durable"
SESSION_TURN_STATE_FAILED = "failed"

T = TypeVar("T")


@dataclass(frozen=True)
class SessionTurnSnapshot:
    id: int
    session_id: UUID
    request_id: str | None
    source_kind: str
    timing_confidence: str
    state: str
    terminal_phase: str | None
    error_code: str | None
    user_event_id: int | None
    durable_assistant_event_id: int | None
    baseline_event_id: int | None
    baseline_runtime_cursor: int | None
    user_submitted_at: datetime
    send_accepted_at: datetime | None
    active_phase_observed_at: datetime | None
    terminal_at: datetime | None
    durable_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_positive_int(value: int | None) -> int | None:
    if value is None:
        return None
    normalized = int(value or 0)
    return normalized if normalized > 0 else None


def _normalize_string(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def run_session_turn_write(
    *,
    db_bind,
    fn: Callable[[Session], T],
) -> T:
    with Session(bind=db_bind) as turn_db:
        result = fn(turn_db)
        turn_db.commit()
        return result


async def execute_session_turn_write(
    *,
    db_bind,
    label: str,
    fn: Callable[[Session], T],
) -> T:
    ws = get_write_serializer()
    if ws.is_configured:
        return await ws.execute(
            lambda _turn_db: run_session_turn_write(db_bind=db_bind, fn=fn),
            label=label,
            auto_commit=False,
        )
    return await asyncio.to_thread(run_session_turn_write, db_bind=db_bind, fn=fn)


def create_session_turn(
    db: Session,
    *,
    session_id: UUID,
    request_id: str | None,
    source_kind: str,
    timing_confidence: str,
    baseline_event_id: int | None = None,
    baseline_runtime_cursor: int | None = None,
    user_submitted_at: datetime | None = None,
) -> SessionTurn:
    normalized_request_id = _normalize_string(request_id)
    if normalized_request_id is not None:
        existing = (
            db.query(SessionTurn)
            .filter(
                SessionTurn.session_id == session_id,
                SessionTurn.request_id == normalized_request_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing

    turn = SessionTurn(
        session_id=session_id,
        request_id=normalized_request_id,
        source_kind=_normalize_string(source_kind) or SESSION_TURN_SOURCE_MANAGED_LIVE,
        timing_confidence=_normalize_string(timing_confidence) or SESSION_TURN_CONFIDENCE_EXACT,
        state=SESSION_TURN_STATE_CREATED,
        baseline_event_id=_normalize_positive_int(baseline_event_id),
        baseline_runtime_cursor=_normalize_positive_int(baseline_runtime_cursor),
        user_submitted_at=_normalize_utc(user_submitted_at) or datetime.now(timezone.utc),
    )
    db.add(turn)
    db.flush()
    return turn


def get_session_turn(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
) -> SessionTurn | None:
    normalized_request_id = _normalize_string(request_id)
    if normalized_request_id is None:
        return None
    return (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.request_id == normalized_request_id,
        )
        .one_or_none()
    )


def get_session_turn_snapshot(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
) -> SessionTurnSnapshot | None:
    with Session(bind=db_bind) as snapshot_db:
        turn = get_session_turn(snapshot_db, session_id=session_id, request_id=request_id)
        if turn is None:
            return None
        return SessionTurnSnapshot(
            id=int(turn.id),
            session_id=session_id,
            request_id=_normalize_string(turn.request_id),
            source_kind=_normalize_string(turn.source_kind) or "",
            timing_confidence=_normalize_string(turn.timing_confidence) or "",
            state=_normalize_string(turn.state) or "",
            terminal_phase=_normalize_string(turn.terminal_phase),
            error_code=_normalize_string(turn.error_code),
            user_event_id=_normalize_positive_int(turn.user_event_id),
            durable_assistant_event_id=_normalize_positive_int(turn.durable_assistant_event_id),
            baseline_event_id=_normalize_positive_int(turn.baseline_event_id),
            baseline_runtime_cursor=_normalize_positive_int(turn.baseline_runtime_cursor),
            user_submitted_at=_normalize_utc(turn.user_submitted_at) or datetime.now(timezone.utc),
            send_accepted_at=_normalize_utc(turn.send_accepted_at),
            active_phase_observed_at=_normalize_utc(turn.active_phase_observed_at),
            terminal_at=_normalize_utc(turn.terminal_at),
            durable_at=_normalize_utc(turn.durable_at),
            created_at=_normalize_utc(turn.created_at),
            updated_at=_normalize_utc(turn.updated_at),
        )


def mark_session_turn_send_accepted(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    accepted_at: datetime | None = None,
    user_event_id: int | None = None,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.send_accepted_at is not None:
        return False
    turn.send_accepted_at = _normalize_utc(accepted_at) or datetime.now(timezone.utc)
    normalized_user_event_id = _normalize_positive_int(user_event_id)
    if normalized_user_event_id is not None and turn.user_event_id is None:
        turn.user_event_id = normalized_user_event_id
    if _normalize_string(turn.state) == SESSION_TURN_STATE_CREATED:
        turn.state = SESSION_TURN_STATE_SEND_ACCEPTED
    return True


def mark_session_turn_active(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    observed_at: datetime | None = None,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.active_phase_observed_at is not None:
        return False
    turn.active_phase_observed_at = _normalize_utc(observed_at) or datetime.now(timezone.utc)
    turn.error_code = None
    if _normalize_string(turn.state) not in {SESSION_TURN_STATE_TERMINAL, SESSION_TURN_STATE_DURABLE}:
        turn.state = SESSION_TURN_STATE_ACTIVE
    return True


def mark_session_turn_terminal(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    phase: str,
    terminal_at: datetime | None = None,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.terminal_at is not None:
        return False
    turn.terminal_phase = _normalize_string(phase)
    turn.terminal_at = _normalize_utc(terminal_at) or datetime.now(timezone.utc)
    turn.error_code = None
    if _normalize_string(turn.state) != SESSION_TURN_STATE_DURABLE:
        turn.state = SESSION_TURN_STATE_TERMINAL
    return True


def mark_session_turn_failed(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    error_code: str,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    normalized_error_code = _normalize_string(error_code)
    if turn is None or normalized_error_code is None:
        return False
    if _normalize_string(turn.state) == SESSION_TURN_STATE_DURABLE:
        return False
    turn.error_code = normalized_error_code
    turn.state = SESSION_TURN_STATE_FAILED
    return True


def maybe_mark_session_turn_durable(
    db: Session,
    *,
    session_id: UUID,
) -> SessionTurn | None:
    pending_turns = (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.send_accepted_at.isnot(None),
            SessionTurn.durable_at.is_(None),
        )
        .order_by(SessionTurn.created_at.asc(), SessionTurn.id.asc())
        .all()
    )
    if not pending_turns:
        return None

    for turn in pending_turns:
        baseline_event_id = _normalize_positive_int(turn.baseline_event_id) or 0
        events = (
            db.query(AgentEvent)
            .filter(
                AgentEvent.session_id == session_id,
                AgentEvent.id > baseline_event_id,
            )
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        match = _match_durable_turn(
            events=events,
            user_event_id=_normalize_positive_int(turn.user_event_id),
        )
        if match is None:
            continue

        user_event, assistant_event = match
        if turn.user_event_id is None:
            turn.user_event_id = int(user_event.id)
        turn.durable_assistant_event_id = int(assistant_event.id)
        turn.durable_at = datetime.now(timezone.utc)
        turn.error_code = None
        turn.state = SESSION_TURN_STATE_DURABLE
        db.flush()
        return turn

    return None


def _match_durable_turn(
    *,
    events: list[AgentEvent],
    user_event_id: int | None,
) -> tuple[AgentEvent, AgentEvent] | None:
    target_user_id = _normalize_positive_int(user_event_id)
    matched_user: AgentEvent | None = None
    last_assistant: AgentEvent | None = None

    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")

        if matched_user is None:
            if role != "user":
                continue
            if target_user_id is not None and int(getattr(event, "id", 0) or 0) != target_user_id:
                continue
            matched_user = event
            continue

        if role == "user":
            if last_assistant is not None:
                return matched_user, last_assistant
            return None

        if role == "assistant" and content_text.strip():
            last_assistant = event

    if matched_user is not None and last_assistant is not None:
        return matched_user, last_assistant
    return None
