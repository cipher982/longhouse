"""Canonical session-turn timing helpers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import MachineTransportHealthSummary
from zerg.services.agent_heartbeat_health import load_machine_transport_health_map
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.write_serializer import get_write_serializer
from zerg.utils.time import normalize_utc
from zerg.utils.time import utc_now

logger = logging.getLogger(__name__)

SESSION_TURN_STATE_CREATED = "created"
SESSION_TURN_STATE_SEND_ACCEPTED = "send_accepted"
SESSION_TURN_STATE_ACTIVE = "active"
SESSION_TURN_STATE_TERMINAL = "terminal"
SESSION_TURN_STATE_DURABLE = "durable"
SESSION_TURN_STATE_FAILED = "failed"

SESSION_TURN_ERROR_SEND_FAILED = "send_failed"
SESSION_TURN_ERROR_VERIFICATION_TIMEOUT = "verification_timeout"
SESSION_TURN_ERROR_TURN_TIMEOUT = "turn_timeout"

T = TypeVar("T")


@dataclass(frozen=True)
class SessionTurnSnapshot:
    id: int
    session_id: UUID
    request_id: str | None
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


@dataclass(frozen=True)
class SlowSessionTurnSummary:
    session: AgentSession
    turn: SessionTurn
    completed_at: datetime
    total_turn_time_ms: int
    machine: MachineTransportHealthSummary | None


def hash_user_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def run_session_turn_write(
    *,
    db_bind,
    fn: Callable[[Session], T],
) -> T:
    with Session(bind=db_bind) as turn_db:
        result = fn(turn_db)
        turn_db.commit()
        return result


def run_best_effort_session_turn_write(
    *,
    db_bind,
    label: str,
    fn: Callable[[Session], object],
):
    try:
        with Session(bind=db_bind) as turn_db:
            result = fn(turn_db)
            turn_db.commit()
            return result
    except Exception:
        logger.warning("Session turn write failed for %s", label, exc_info=True)
        return None


async def execute_session_turn_write(
    *,
    db_bind,
    label: str,
    fn: Callable[[Session], T],
) -> T:
    ws = get_write_serializer()
    if ws.is_configured:
        return await ws.execute_with_session_factory(
            make_sessionmaker(db_bind),
            fn,
            label=label,
        )
    return await asyncio.to_thread(run_session_turn_write, db_bind=db_bind, fn=fn)


def create_session_turn(
    db: Session,
    *,
    session_id: UUID,
    request_id: str | None,
    baseline_event_id: int | None = None,
    baseline_runtime_cursor: int | None = None,
    user_submitted_at: datetime | None = None,
    expected_user_text: str | None = None,
) -> SessionTurn:
    if request_id is not None:
        existing = (
            db.query(SessionTurn)
            .filter(
                SessionTurn.session_id == session_id,
                SessionTurn.request_id == request_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing

    turn = SessionTurn(
        session_id=session_id,
        request_id=request_id,
        expected_user_text_hash=hash_user_text(expected_user_text) if expected_user_text else None,
        state=SESSION_TURN_STATE_CREATED,
        baseline_event_id=baseline_event_id if baseline_event_id and baseline_event_id > 0 else None,
        baseline_runtime_cursor=baseline_runtime_cursor if baseline_runtime_cursor and baseline_runtime_cursor > 0 else None,
        user_submitted_at=normalize_utc(user_submitted_at) or datetime.now(timezone.utc),
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
    if not request_id:
        return None
    return (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.request_id == request_id,
        )
        .one_or_none()
    )


def get_session_turn_by_id(
    db: Session,
    *,
    session_id: UUID,
    turn_id: int,
) -> SessionTurn | None:
    if not turn_id or turn_id <= 0:
        return None
    return (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.id == turn_id,
        )
        .one_or_none()
    )


def list_session_turns(
    db: Session,
    *,
    session_id: UUID,
    limit: int = 50,
    offset: int = 0,
    order: str = "asc",
) -> tuple[list[SessionTurn], int]:
    query = db.query(SessionTurn).filter(SessionTurn.session_id == session_id)
    total = query.count()

    order_columns = (
        SessionTurn.user_submitted_at,
        SessionTurn.created_at,
        SessionTurn.id,
    )
    if order == "desc":
        query = query.order_by(*(column.desc() for column in order_columns))
    else:
        query = query.order_by(*(column.asc() for column in order_columns))

    turns = query.offset(max(0, offset)).limit(max(1, limit)).all()
    return turns, total


def list_slow_session_turns(
    db: Session,
    *,
    provider: str | None = None,
    project: str | None = None,
    device_id: str | None = None,
    state: str | None = None,
    machine_status: str | None = None,
    min_total_turn_time_ms: int = 30_000,
    hours_back: int = 24,
    stale_after_seconds: int = DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[SlowSessionTurnSummary], int]:
    submitted_after = utc_now() - timedelta(hours=max(1, hours_back))
    completed_at_expr = func.coalesce(SessionTurn.durable_at, SessionTurn.terminal_at)
    total_turn_time_expr = _completed_turn_time_ms_sql(db)

    query = (
        db.query(SessionTurn, AgentSession)
        .join(AgentSession, AgentSession.id == SessionTurn.session_id)
        .filter(
            AgentSession.managed_transport.isnot(None),
            SessionTurn.user_submitted_at >= submitted_after,
            completed_at_expr.isnot(None),
        )
    )
    if provider:
        query = query.filter(AgentSession.provider == provider)
    if project:
        query = query.filter(AgentSession.project == project)
    if device_id:
        query = query.filter(AgentSession.device_id == device_id)
    if state:
        query = query.filter(SessionTurn.state == state)
    if total_turn_time_expr is not None:
        query = query.filter(total_turn_time_expr >= int(min_total_turn_time_ms))

    rows = query.order_by(completed_at_expr.desc(), SessionTurn.id.desc()).all()
    device_ids = {str(session.device_id).strip() for _, session in rows if str(session.device_id or "").strip()}
    machine_map = load_machine_transport_health_map(
        db,
        device_ids=device_ids,
        stale_after_seconds=stale_after_seconds,
    )

    summaries: list[SlowSessionTurnSummary] = []
    for turn, session in rows:
        completed_at = normalize_utc(turn.durable_at) or normalize_utc(turn.terminal_at)
        total_turn_time_ms = _completed_turn_time_ms(turn)
        if completed_at is None or total_turn_time_ms is None or total_turn_time_ms < min_total_turn_time_ms:
            continue
        machine = machine_map.get(str(session.device_id or "").strip()) if session.device_id else None
        if machine_status and (machine is None or machine.status != machine_status):
            continue
        summaries.append(
            SlowSessionTurnSummary(
                session=session,
                turn=turn,
                completed_at=completed_at,
                total_turn_time_ms=total_turn_time_ms,
                machine=machine,
            )
        )

    summaries.sort(
        key=lambda item: (
            -item.total_turn_time_ms,
            -item.completed_at.timestamp(),
            -int(item.turn.id),
        )
    )
    # total reflects the fully filtered slow-turn set, including current
    # machine-status enrichment that is only available after the heartbeat map
    # join in Python.
    total = len(summaries)
    page = summaries[max(0, offset) : max(0, offset) + max(1, limit)]
    return page, total


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
            request_id=turn.request_id,
            state=turn.state or "",
            terminal_phase=turn.terminal_phase,
            error_code=turn.error_code,
            user_event_id=turn.user_event_id,
            durable_assistant_event_id=turn.durable_assistant_event_id,
            baseline_event_id=turn.baseline_event_id,
            baseline_runtime_cursor=turn.baseline_runtime_cursor,
            user_submitted_at=normalize_utc(turn.user_submitted_at) or datetime.now(timezone.utc),
            send_accepted_at=normalize_utc(turn.send_accepted_at),
            active_phase_observed_at=normalize_utc(turn.active_phase_observed_at),
            terminal_at=normalize_utc(turn.terminal_at),
            durable_at=normalize_utc(turn.durable_at),
            created_at=normalize_utc(turn.created_at),
            updated_at=normalize_utc(turn.updated_at),
        )


def _completed_turn_time_ms(turn: SessionTurn) -> int | None:
    user_submitted_at = normalize_utc(turn.user_submitted_at)
    completed_at = normalize_utc(turn.durable_at) or normalize_utc(turn.terminal_at)
    if user_submitted_at is None or completed_at is None:
        return None
    elapsed_ms = round((completed_at - user_submitted_at).total_seconds() * 1000)
    return max(0, int(elapsed_ms))


def _completed_turn_time_ms_sql(db: Session):
    completed_at_expr = func.coalesce(SessionTurn.durable_at, SessionTurn.terminal_at)
    bind = getattr(db, "bind", None)
    dialect = getattr(bind, "dialect", None)
    dialect_name = str(getattr(dialect, "name", "") or "").lower()
    if dialect_name == "sqlite":
        # julianday() returns days as a float; convert to milliseconds.
        return func.round((func.julianday(completed_at_expr) - func.julianday(SessionTurn.user_submitted_at)) * 86400000)
    if dialect_name == "postgresql":
        return func.floor(func.extract("epoch", completed_at_expr - SessionTurn.user_submitted_at) * 1000)
    # Unknown dialects still stay correct because Python re-checks the
    # threshold for every row after load; this only loses the SQL pre-filter.
    return None


def mark_session_turn_send_accepted(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    accepted_at: datetime | None = None,
    user_event_id: int | None = None,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    if turn is None:
        return False
    if turn.send_accepted_at is not None:
        if turn.user_event_id is None and user_event_id is not None:
            turn.user_event_id = user_event_id
        return True

    turn.send_accepted_at = normalize_utc(accepted_at) or datetime.now(timezone.utc)
    if user_event_id is not None and turn.user_event_id is None:
        turn.user_event_id = user_event_id
    if turn.state == SESSION_TURN_STATE_CREATED:
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
    if turn is None:
        return False
    if turn.active_phase_observed_at is not None:
        return True
    if turn.state in {
        SESSION_TURN_STATE_FAILED,
        SESSION_TURN_STATE_TERMINAL,
        SESSION_TURN_STATE_DURABLE,
    }:
        return True
    turn.active_phase_observed_at = normalize_utc(observed_at) or datetime.now(timezone.utc)
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
    if turn is None:
        return False
    if turn.terminal_at is not None:
        return True
    if turn.state == SESSION_TURN_STATE_DURABLE:
        return True
    turn.terminal_phase = (phase or "").strip() or None
    turn.terminal_at = normalize_utc(terminal_at) or datetime.now(timezone.utc)
    if turn.state != SESSION_TURN_STATE_FAILED:
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
    if turn is None or not error_code:
        return False
    if turn.state in {
        SESSION_TURN_STATE_TERMINAL,
        SESSION_TURN_STATE_DURABLE,
    }:
        return True
    if turn.state == SESSION_TURN_STATE_FAILED:
        if turn.error_code is None:
            turn.error_code = error_code
        return True
    turn.error_code = error_code
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

    for idx, turn in enumerate(pending_turns):
        baseline_event_id = turn.baseline_event_id or 0
        events = (
            db.query(AgentEvent)
            .filter(
                AgentEvent.session_id == session_id,
                AgentEvent.id > baseline_event_id,
            )
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        expected_hash = turn.expected_user_text_hash
        if expected_hash:
            match = _match_durable_turn_by_hash(events=events, expected_user_text_hash=expected_hash)
        else:
            match = _match_durable_turn_by_window(
                events=events,
                user_event_id=turn.user_event_id,
                submitted_after=normalize_utc(turn.user_submitted_at),
                submitted_before=normalize_utc(pending_turns[idx + 1].user_submitted_at) if idx + 1 < len(pending_turns) else None,
            )
        if match is None:
            continue

        user_event, assistant_event = match
        if turn.user_event_id is None:
            turn.user_event_id = int(user_event.id)
        turn.durable_assistant_event_id = int(assistant_event.id)
        turn.durable_at = datetime.now(timezone.utc)
        if turn.error_code:
            logger.info(
                "Session turn %s for session %s became durable after %s",
                str(turn.request_id or ""),
                str(session_id),
                turn.error_code,
            )
            turn.error_code = None
        turn.state = SESSION_TURN_STATE_DURABLE
        db.flush()
        return turn

    return None


def _match_durable_turn_by_hash(
    *,
    events: list[AgentEvent],
    expected_user_text_hash: str,
) -> tuple[AgentEvent, AgentEvent] | None:
    if not expected_user_text_hash:
        return None

    matched_user: AgentEvent | None = None
    last_assistant: AgentEvent | None = None
    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")

        if matched_user is None:
            normalized_user_text = strip_claude_channel_wrapper(content_text)
            if role == "user" and hash_user_text(normalized_user_text) == expected_user_text_hash:
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


def _match_durable_turn_by_window(
    *,
    events: list[AgentEvent],
    user_event_id: int | None,
    submitted_after: datetime | None,
    submitted_before: datetime | None,
) -> tuple[AgentEvent, AgentEvent] | None:
    matched_user: AgentEvent | None = None
    last_assistant: AgentEvent | None = None

    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")

        if matched_user is None:
            if role != "user":
                continue
            if user_event_id is not None and int(getattr(event, "id", 0) or 0) != user_event_id:
                continue
            if user_event_id is None and not _event_in_turn_window(
                event,
                submitted_after=submitted_after,
                submitted_before=submitted_before,
            ):
                continue
            matched_user = event
            continue

        if role == "user":
            if last_assistant is not None:
                return matched_user, last_assistant
            if user_event_id is not None:
                return None
            if not _event_in_turn_window(
                event,
                submitted_after=submitted_after,
                submitted_before=submitted_before,
            ):
                return None
            matched_user = event
            last_assistant = None
            continue

        if role == "assistant" and content_text.strip():
            last_assistant = event

    if matched_user is not None and last_assistant is not None:
        return matched_user, last_assistant
    return None


def _event_in_turn_window(
    event: AgentEvent,
    *,
    submitted_after: datetime | None,
    submitted_before: datetime | None,
) -> bool:
    event_timestamp = normalize_utc(getattr(event, "timestamp", None))
    if event_timestamp is None:
        return True
    if submitted_after is not None and event_timestamp < submitted_after:
        return False
    if submitted_before is not None and event_timestamp >= submitted_before:
        return False
    return True
