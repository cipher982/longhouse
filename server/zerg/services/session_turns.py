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

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionTurn
from zerg.services.agent_heartbeat_health import DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS
from zerg.services.agent_heartbeat_health import MachineTransportHealthSummary
from zerg.services.agent_heartbeat_health import load_machine_transport_health_map
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.services.provisional_events import durable_transcript_event_predicate
from zerg.services.session_observations import OBS_KIND_RUNTIME_SIGNAL
from zerg.services.write_serializer import get_write_serializer
from zerg.utils.time import normalize_utc
from zerg.utils.time import utc_now

logger = logging.getLogger(__name__)

SESSION_TURN_SOURCE_MANAGED_LIVE = "managed_live"
SESSION_TURN_SOURCE_TRANSCRIPT_RECONSTRUCTED = "transcript_reconstructed"
SESSION_TURN_SOURCE_VALUES = {
    SESSION_TURN_SOURCE_MANAGED_LIVE,
    SESSION_TURN_SOURCE_TRANSCRIPT_RECONSTRUCTED,
}

SESSION_TURN_CONFIDENCE_EXACT = "exact"
SESSION_TURN_CONFIDENCE_INFERRED = "inferred"
SESSION_TURN_CONFIDENCE_VALUES = {
    SESSION_TURN_CONFIDENCE_EXACT,
    SESSION_TURN_CONFIDENCE_INFERRED,
}

SESSION_TURN_STATE_CREATED = "created"
SESSION_TURN_STATE_SEND_ACCEPTED = "send_accepted"
SESSION_TURN_STATE_ACTIVE = "active"
SESSION_TURN_STATE_TERMINAL = "terminal"
SESSION_TURN_STATE_DURABLE = "durable"
SESSION_TURN_STATE_FAILED = "failed"
PENDING_RESPONSE_TURN_STATES = frozenset(
    {
        SESSION_TURN_STATE_SEND_ACCEPTED,
        SESSION_TURN_STATE_ACTIVE,
        SESSION_TURN_STATE_TERMINAL,
    }
)

SESSION_TURN_ERROR_SEND_FAILED = "send_failed"
SESSION_TURN_ERROR_VERIFICATION_TIMEOUT = "verification_timeout"
SESSION_TURN_ERROR_TURN_TIMEOUT = "turn_timeout"
SESSION_TURN_RECONSTRUCTED_REQUEST_PREFIX = "native"

RECENT_MANAGED_TURN_MATERIALIZATION_LIMIT = 200
SESSION_TURN_MATERIALIZATION_STALE_AFTER_DAYS = 7

T = TypeVar("T")


@dataclass(frozen=True)
class SessionTurnSnapshot:
    id: int
    session_id: UUID
    request_id: str | None
    session_input_id: int | None
    state: str
    terminal_phase: str | None
    error_code: str | None
    user_event_id: int | None
    durable_assistant_event_id: int | None
    baseline_event_id: int | None
    baseline_observation_cursor: int | None
    user_submitted_at: datetime
    send_accepted_at: datetime | None
    active_phase_observed_at: datetime | None
    terminal_at: datetime | None
    durable_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ManagedCompletedTurnSummary:
    session: AgentSession
    turn: SessionTurn
    completed_at: datetime
    total_turn_time_ms: int
    machine: MachineTransportHealthSummary | None


def hash_user_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _normalize_turn_source_kind(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in SESSION_TURN_SOURCE_VALUES:
        return normalized
    if normalized:
        logger.warning("Unknown session turn source_kind '%s'; defaulting to %s", normalized, SESSION_TURN_SOURCE_MANAGED_LIVE)
    return SESSION_TURN_SOURCE_MANAGED_LIVE


def _normalize_turn_confidence(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in SESSION_TURN_CONFIDENCE_VALUES:
        return normalized
    if normalized:
        logger.warning("Unknown session turn timing_confidence '%s'; defaulting to %s", normalized, SESSION_TURN_CONFIDENCE_EXACT)
    return SESSION_TURN_CONFIDENCE_EXACT


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
    source_kind: str = SESSION_TURN_SOURCE_MANAGED_LIVE,
    timing_confidence: str = SESSION_TURN_CONFIDENCE_EXACT,
    baseline_event_id: int | None = None,
    baseline_observation_cursor: int | None = None,
    user_submitted_at: datetime | None = None,
    expected_user_text: str | None = None,
    session_input_id: int | None = None,
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
            _attach_session_input(existing, session_input_id=session_input_id)
            return existing

    # Phase 2: stamp thread_id on every new turn so Phase 3 can flip
    # session_turns to thread-keyed.
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    thread_id = ensure_thread_id_for_session(db, session_id)
    turn = SessionTurn(
        session_id=session_id,
        thread_id=thread_id,
        request_id=request_id,
        session_input_id=session_input_id,
        source_kind=_normalize_turn_source_kind(source_kind),
        timing_confidence=_normalize_turn_confidence(timing_confidence),
        expected_user_text_hash=hash_user_text(expected_user_text) if expected_user_text else None,
        state=SESSION_TURN_STATE_CREATED,
        baseline_event_id=baseline_event_id if baseline_event_id and baseline_event_id > 0 else None,
        baseline_observation_cursor=baseline_observation_cursor
        if baseline_observation_cursor and baseline_observation_cursor > 0
        else None,
        user_submitted_at=normalize_utc(user_submitted_at) or datetime.now(timezone.utc),
    )
    db.add(turn)
    db.flush()
    return turn


def _attach_session_input(turn: SessionTurn, *, session_input_id: int | None) -> None:
    if session_input_id is None:
        return
    normalized_id = int(session_input_id)
    if normalized_id <= 0:
        return
    existing_id = getattr(turn, "session_input_id", None)
    if existing_id is None:
        turn.session_input_id = normalized_id
        return
    if int(existing_id) != normalized_id:
        logger.warning(
            "Session turn %s for session %s already links SessionInput %s; preserving over conflicting %s",
            getattr(turn, "request_id", None),
            getattr(turn, "session_id", None),
            existing_id,
            normalized_id,
        )


def materialize_managed_transcript_turns(
    db: Session,
    *,
    session_id: UUID,
    incremental: bool = False,
) -> int:
    session = db.query(AgentSession).filter(AgentSession.id == session_id).one_or_none()
    if session is None or not str(getattr(session, "managed_transport", "") or "").strip():
        return 0

    session_last_activity = normalize_utc(getattr(session, "last_activity_at", None)) or normalize_utc(getattr(session, "started_at", None))
    if session_last_activity is not None and session_last_activity < utc_now() - timedelta(
        days=SESSION_TURN_MATERIALIZATION_STALE_AFTER_DAYS
    ):
        has_existing_turn = db.query(SessionTurn.id).filter(SessionTurn.session_id == session_id).limit(1).one_or_none()
        if has_existing_turn is not None:
            return 0

    existing_turns = (
        db.query(SessionTurn)
        .filter(SessionTurn.session_id == session_id)
        .order_by(SessionTurn.user_submitted_at.asc(), SessionTurn.created_at.asc(), SessionTurn.id.asc())
        .all()
    )
    has_pending_request_turn = any(
        str(getattr(turn, "request_id", "") or "").strip()
        and getattr(turn, "send_accepted_at", None) is not None
        and getattr(turn, "durable_at", None) is None
        for turn in existing_turns
    )
    if has_pending_request_turn:
        return 0

    events_query = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).filter(durable_transcript_event_predicate())
    if incremental:
        event_floor = _transcript_materialization_event_floor(existing_turns)
        if event_floor is not None:
            has_new_user_event = (
                db.query(AgentEvent.id)
                .filter(
                    AgentEvent.session_id == session_id,
                    AgentEvent.id > event_floor,
                    AgentEvent.role == "user",
                )
                .filter(durable_transcript_event_predicate())
                .limit(1)
                .one_or_none()
            )
            if has_new_user_event is None:
                return 0
            events_query = events_query.filter(AgentEvent.id > event_floor)

    events = events_query.order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc()).all()
    if not events:
        return 0

    claimed_user_event_ids = {int(turn.user_event_id) for turn in existing_turns if getattr(turn, "user_event_id", None) is not None}
    claimed_assistant_event_ids = {
        int(turn.durable_assistant_event_id) for turn in existing_turns if getattr(turn, "durable_assistant_event_id", None) is not None
    }

    created = 0
    for user_event, assistant_event in _iter_completed_transcript_turn_pairs(events):
        user_event_id = int(getattr(user_event, "id", 0) or 0)
        assistant_event_id = int(getattr(assistant_event, "id", 0) or 0)
        if user_event_id <= 0 or assistant_event_id <= 0:
            continue
        if user_event_id in claimed_user_event_ids or assistant_event_id in claimed_assistant_event_ids:
            continue

        normalized_user_text = strip_claude_channel_wrapper(str(getattr(user_event, "content_text", "") or ""))
        user_submitted_at = normalize_utc(getattr(user_event, "timestamp", None)) or utc_now()
        durable_at = normalize_utc(getattr(assistant_event, "timestamp", None)) or user_submitted_at
        request_id = _reconstructed_turn_request_id(
            user_event_id=user_event_id,
            assistant_event_id=assistant_event_id,
        )
        try:
            with db.begin_nested():
                turn = create_session_turn(
                    db,
                    session_id=session_id,
                    request_id=request_id,
                    source_kind=SESSION_TURN_SOURCE_TRANSCRIPT_RECONSTRUCTED,
                    timing_confidence=SESSION_TURN_CONFIDENCE_INFERRED,
                    user_submitted_at=user_submitted_at,
                    expected_user_text=normalized_user_text or None,
                )
                if turn.user_event_id is None:
                    turn.user_event_id = user_event_id
                if turn.durable_assistant_event_id is None:
                    turn.durable_assistant_event_id = assistant_event_id
                if turn.durable_at is None:
                    turn.durable_at = durable_at
                if turn.state != SESSION_TURN_STATE_DURABLE:
                    turn.state = SESSION_TURN_STATE_DURABLE
                if turn.error_code:
                    turn.error_code = None
        except IntegrityError:
            turn = get_session_turn(db, session_id=session_id, request_id=request_id)
            if turn is None:
                raise
        claimed_user_event_ids.add(user_event_id)
        claimed_assistant_event_ids.add(assistant_event_id)
        if turn.user_event_id == user_event_id and turn.durable_assistant_event_id == assistant_event_id:
            created += 1

    return created


def _transcript_materialization_event_floor(existing_turns: list[SessionTurn]) -> int | None:
    assistant_event_ids = [
        int(event_id)
        for event_id in (getattr(turn, "durable_assistant_event_id", None) for turn in existing_turns)
        if event_id is not None and int(event_id) > 0
    ]
    if not assistant_event_ids:
        return None
    return max(assistant_event_ids)


def materialize_recent_managed_transcript_turns(
    db: Session,
    *,
    provider: str | None = None,
    project: str | None = None,
    device_id: str | None = None,
    hours_back: int = 24,
    session_limit: int = RECENT_MANAGED_TURN_MATERIALIZATION_LIMIT,
) -> int:
    lookback_start = utc_now() - timedelta(hours=max(1, hours_back))
    activity_anchor = func.coalesce(AgentSession.last_activity_at, AgentSession.started_at)
    # Session-identity-kernel cleanup: ``managed_transport`` was dropped.
    # Approximate by anchoring on activity window only; managed/unmanaged
    # split now lives on SessionConnection which downstream code consults.
    query = db.query(AgentSession.id).filter(
        activity_anchor >= lookback_start,
    )
    if provider:
        query = query.filter(AgentSession.provider == provider)
    if project:
        query = query.filter(AgentSession.project == project)
    if device_id:
        query = query.filter(AgentSession.device_id == device_id)
    query = query.order_by(activity_anchor.desc(), AgentSession.started_at.desc(), AgentSession.id.desc()).limit(max(1, session_limit))

    created = 0
    for (session_id,) in query.all():
        created += materialize_managed_transcript_turns(db, session_id=session_id)
    return created


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


def load_pending_response_turn_map(db: Session, session_ids: list[UUID]) -> dict[UUID, bool]:
    """Return sessions whose latest user prompt has no visible response yet.

    Managed sends create exact ``SessionTurn`` rows, but imported/native
    provider transcripts can still produce a user prompt plus runtime phase
    signals before the matching assistant output lands. In those cases total
    user/assistant counts are not useful: real Claude transcripts often have
    many assistant/tool events per user turn. Fall back to event order plus a
    post-prompt active phase so clients can honestly show transcript syncing
    while the archive catches up.
    """
    if not session_ids:
        return {}
    rows = (
        db.query(SessionTurn.session_id)
        .filter(SessionTurn.session_id.in_(session_ids))
        .filter(SessionTurn.durable_at.is_(None))
        .filter(SessionTurn.state.in_(tuple(PENDING_RESPONSE_TURN_STATES)))
        .all()
    )
    pending = {row[0]: True for row in rows if row[0] is not None}
    remaining_session_ids = [session_id for session_id in session_ids if session_id not in pending]
    pending.update(_load_unanswered_user_prompt_map(db, remaining_session_ids))
    return pending


def _load_unanswered_user_prompt_map(db: Session, session_ids: list[UUID]) -> dict[UUID, bool]:
    if not session_ids:
        return {}

    latest_user_rows = (
        db.query(AgentEvent.session_id, func.max(AgentEvent.id))
        .filter(AgentEvent.session_id.in_(session_ids))
        .filter(durable_transcript_event_predicate())
        .filter(AgentEvent.role == "user")
        .group_by(AgentEvent.session_id)
        .all()
    )
    latest_user_id_by_session = {
        session_id: int(event_id) for session_id, event_id in latest_user_rows if session_id is not None and event_id is not None
    }
    if not latest_user_id_by_session:
        return {}

    latest_response_rows = (
        db.query(AgentEvent.session_id, func.max(AgentEvent.id))
        .filter(AgentEvent.session_id.in_(latest_user_id_by_session.keys()))
        .filter(durable_transcript_event_predicate())
        .filter(AgentEvent.role.in_(("assistant", "tool")))
        .group_by(AgentEvent.session_id)
        .all()
    )
    latest_response_id_by_session = {
        session_id: int(event_id) for session_id, event_id in latest_response_rows if session_id is not None and event_id is not None
    }

    candidate_user_ids = [
        user_event_id
        for session_id, user_event_id in latest_user_id_by_session.items()
        if user_event_id > int(latest_response_id_by_session.get(session_id, 0) or 0)
    ]
    if not candidate_user_ids:
        return {}

    latest_user_events = db.query(AgentEvent.session_id, AgentEvent.timestamp).filter(AgentEvent.id.in_(candidate_user_ids)).all()
    active_phase_conditions = []
    for session_id, timestamp in latest_user_events:
        observed_after = normalize_utc(timestamp)
        if session_id is None or observed_after is None:
            continue
        active_phase_conditions.append(
            and_(
                SessionObservation.session_id == session_id,
                SessionObservation.observed_at >= observed_after,
            )
        )
    if not active_phase_conditions:
        return {}

    rows = (
        db.query(SessionObservation.session_id)
        .filter(SessionObservation.session_id.in_(latest_user_id_by_session.keys()))
        .filter(SessionObservation.kind == OBS_KIND_RUNTIME_SIGNAL)
        .filter(or_(*active_phase_conditions))
        .filter(SessionObservation.payload_json.like('%"kind":"phase_signal"%'))
        .filter(
            or_(
                SessionObservation.payload_json.like('%"phase":"thinking"%'),
                SessionObservation.payload_json.like('%"phase":"running"%'),
                SessionObservation.payload_json.like('%"phase":"blocked"%'),
            )
        )
        .group_by(SessionObservation.session_id)
        .all()
    )
    return {row[0]: True for row in rows if row[0] is not None}


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
) -> tuple[list[ManagedCompletedTurnSummary], int]:
    summaries = list_managed_completed_turns(
        db,
        provider=provider,
        project=project,
        device_id=device_id,
        state=state,
        machine_status=machine_status,
        min_total_turn_time_ms=min_total_turn_time_ms,
        hours_back=hours_back,
        stale_after_seconds=stale_after_seconds,
    )
    # Keep the threshold authoritative even if a future dialect cannot express
    # the SQL pre-filter and list_managed_completed_turns returns the broader
    # completed-turn slice.
    summaries = [item for item in summaries if item.total_turn_time_ms >= min_total_turn_time_ms]
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


def list_managed_completed_turns(
    db: Session,
    *,
    provider: str | None = None,
    project: str | None = None,
    device_id: str | None = None,
    state: str | None = None,
    machine_status: str | None = None,
    min_total_turn_time_ms: int | None = None,
    hours_back: int = 24,
    stale_after_seconds: int = DEFAULT_MACHINE_HEARTBEAT_STALE_AFTER_SECONDS,
) -> list[ManagedCompletedTurnSummary]:
    submitted_after = utc_now() - timedelta(hours=max(1, hours_back))
    completed_at_expr = func.coalesce(SessionTurn.durable_at, SessionTurn.terminal_at)
    total_turn_time_expr = _completed_turn_time_ms_sql(db)

    # Session-identity-kernel cleanup: ``managed_transport`` was dropped.
    # Managed-vs-unmanaged truth lives on ``session_connections``: a session
    # is managed iff one of its primary-thread runs has a connection whose
    # control_plane is one of the managed transports. Filter via EXISTS so
    # we don't double-count sessions with multiple connections.
    from zerg.models.agents import SessionConnection
    from zerg.models.agents import SessionRun
    from zerg.models.agents import SessionThread

    managed_session_exists = (
        db.query(SessionThread.id)
        .join(SessionRun, SessionRun.thread_id == SessionThread.id)
        .join(SessionConnection, SessionConnection.run_id == SessionRun.id)
        .filter(SessionThread.session_id == AgentSession.id)
        .filter(
            SessionConnection.control_plane.in_(
                (
                    "claude_channel_bridge",
                    "codex_bridge",
                    "codex_app_server",
                    "opencode_server_bridge",
                    "opencode_process",
                    "antigravity_process",
                )
            )
        )
        .exists()
    )

    query = (
        db.query(SessionTurn, AgentSession)
        .join(AgentSession, AgentSession.id == SessionTurn.session_id)
        .filter(
            SessionTurn.user_submitted_at >= submitted_after,
            completed_at_expr.isnot(None),
            managed_session_exists,
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
    if min_total_turn_time_ms is not None and total_turn_time_expr is not None:
        query = query.filter(total_turn_time_expr >= int(min_total_turn_time_ms))

    rows = query.order_by(completed_at_expr.desc(), SessionTurn.id.desc()).all()
    device_ids = {str(session.device_id).strip() for _, session in rows if str(session.device_id or "").strip()}
    machine_map = load_machine_transport_health_map(
        db,
        device_ids=device_ids,
        stale_after_seconds=stale_after_seconds,
    )

    summaries: list[ManagedCompletedTurnSummary] = []
    for turn, session in rows:
        completed_at = normalize_utc(turn.durable_at) or normalize_utc(turn.terminal_at)
        total_turn_time_ms = _completed_turn_time_ms(turn)
        if completed_at is None or total_turn_time_ms is None:
            continue
        machine = machine_map.get(str(session.device_id or "").strip()) if session.device_id else None
        if machine_status and (machine is None or machine.status != machine_status):
            continue
        summaries.append(
            ManagedCompletedTurnSummary(
                session=session,
                turn=turn,
                completed_at=completed_at,
                total_turn_time_ms=total_turn_time_ms,
                machine=machine,
            )
        )
    return summaries


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
            session_input_id=turn.session_input_id,
            state=turn.state or "",
            terminal_phase=turn.terminal_phase,
            error_code=turn.error_code,
            user_event_id=turn.user_event_id,
            durable_assistant_event_id=turn.durable_assistant_event_id,
            baseline_event_id=turn.baseline_event_id,
            baseline_observation_cursor=turn.baseline_observation_cursor,
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
    # Unknown dialects still stay correct because list_slow_session_turns
    # re-checks the threshold in Python after load; this only loses the SQL
    # pre-filter.
    return None


def mark_session_turn_send_accepted(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    accepted_at: datetime | None = None,
    user_event_id: int | None = None,
    session_input_id: int | None = None,
) -> bool:
    turn = get_session_turn(db, session_id=session_id, request_id=request_id)
    if turn is None:
        return False
    _attach_session_input(turn, session_input_id=session_input_id)
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
            .filter(durable_transcript_event_predicate())
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


def _iter_completed_transcript_turn_pairs(
    events: list[AgentEvent],
) -> list[tuple[AgentEvent, AgentEvent]]:
    completed_pairs: list[tuple[AgentEvent, AgentEvent]] = []
    current_user: AgentEvent | None = None
    last_assistant: AgentEvent | None = None

    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")

        if role == "user":
            if current_user is not None and last_assistant is not None:
                completed_pairs.append((current_user, last_assistant))
            current_user = event
            last_assistant = None
            continue

        if current_user is None:
            continue

        if role == "assistant" and content_text.strip():
            last_assistant = event

    if current_user is not None and last_assistant is not None:
        completed_pairs.append((current_user, last_assistant))
    return completed_pairs


def _reconstructed_turn_request_id(
    *,
    user_event_id: int,
    assistant_event_id: int,
) -> str:
    return f"{SESSION_TURN_RECONSTRUCTED_REQUEST_PREFIX}:{user_event_id}:{assistant_event_id}"
