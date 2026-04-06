"""Minimal per-turn shadow ledger for managed-local continuation."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import ManagedLocalTurn
from zerg.services.claude_channel_text import strip_claude_channel_wrapper

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedLocalTurnSnapshot:
    session_id: UUID
    request_id: str
    baseline_event_id: int
    baseline_runtime_event_id: int
    send_accepted_at: datetime | None
    terminal_phase: str | None
    terminal_at: datetime | None
    terminal_runtime_event_id: int | None
    durable_user_event_id: int | None
    durable_assistant_event_id: int | None
    durable_at: datetime | None
    review_id: int | None
    error_code: str | None


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_managed_local_turn_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def create_managed_local_turn(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    baseline_event_id: int,
    baseline_runtime_event_id: int,
    expected_user_text: str,
) -> ManagedLocalTurn:
    existing = (
        db.query(ManagedLocalTurn)
        .filter(
            ManagedLocalTurn.session_id == session_id,
            ManagedLocalTurn.request_id == request_id,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing

    turn = ManagedLocalTurn(
        session_id=session_id,
        request_id=str(request_id),
        baseline_event_id=int(baseline_event_id or 0),
        baseline_runtime_event_id=int(baseline_runtime_event_id or 0),
        expected_user_text_hash=hash_managed_local_turn_text(expected_user_text),
    )
    db.add(turn)
    db.flush()
    return turn


def mark_managed_local_turn_send_accepted(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    accepted_at: datetime | None = None,
) -> bool:
    turn = _get_turn_by_request(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.send_accepted_at is not None:
        return False
    turn.send_accepted_at = _normalize_utc(accepted_at) or datetime.now(timezone.utc)
    return True


def mark_managed_local_turn_terminal(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    phase: str,
    terminal_at: datetime | None = None,
    terminal_runtime_event_id: int | None = None,
) -> bool:
    turn = _get_turn_by_request(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.terminal_at is not None:
        return False
    turn.terminal_phase = str(phase or "").strip() or None
    turn.terminal_at = _normalize_utc(terminal_at) or datetime.now(timezone.utc)
    if terminal_runtime_event_id is not None and turn.terminal_runtime_event_id is None:
        turn.terminal_runtime_event_id = int(terminal_runtime_event_id)
    return True


def mark_managed_local_turn_failed(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    error_code: str,
) -> bool:
    turn = _get_turn_by_request(db, session_id=session_id, request_id=request_id)
    if turn is None or turn.error_code is not None:
        return False
    turn.error_code = str(error_code or "").strip() or None
    return True


def maybe_mark_managed_local_turn_durable(db: Session, *, session_id: UUID) -> ManagedLocalTurn | None:
    pending_turns = (
        db.query(ManagedLocalTurn)
        .filter(
            ManagedLocalTurn.session_id == session_id,
            ManagedLocalTurn.send_accepted_at.isnot(None),
            ManagedLocalTurn.durable_at.is_(None),
        )
        .order_by(ManagedLocalTurn.created_at.asc(), ManagedLocalTurn.id.asc())
        .all()
    )
    if not pending_turns:
        return None

    for turn in pending_turns:
        events = (
            db.query(AgentEvent)
            .filter(
                AgentEvent.session_id == session_id,
                AgentEvent.id > int(turn.baseline_event_id or 0),
            )
            .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
            .all()
        )
        match = _match_durable_turn(events=events, expected_user_text_hash=str(turn.expected_user_text_hash or ""))
        if match is None:
            continue

        user_event, assistant_event = match
        turn.durable_user_event_id = int(user_event.id)
        turn.durable_assistant_event_id = int(assistant_event.id)
        turn.durable_at = datetime.now(timezone.utc)
        if turn.error_code == "turn_timeout":
            turn.error_code = None
        db.flush()
        return turn

    return None


def attach_review_to_managed_local_turn(
    db: Session,
    *,
    session_id: UUID,
    assistant_event_id: int,
    review_id: int,
) -> bool:
    turn = (
        db.query(ManagedLocalTurn)
        .filter(
            ManagedLocalTurn.session_id == session_id,
            ManagedLocalTurn.durable_assistant_event_id == int(assistant_event_id),
            ManagedLocalTurn.review_id.is_(None),
        )
        .order_by(ManagedLocalTurn.created_at.asc(), ManagedLocalTurn.id.asc())
        .first()
    )
    if turn is None:
        return False
    turn.review_id = int(review_id)
    db.flush()
    return True


def get_reviewable_managed_local_turns(
    db: Session,
    *,
    session_id: UUID,
    limit: int | None = None,
) -> list[ManagedLocalTurn]:
    query = (
        db.query(ManagedLocalTurn)
        .filter(
            ManagedLocalTurn.session_id == session_id,
            ManagedLocalTurn.durable_at.isnot(None),
            ManagedLocalTurn.review_id.is_(None),
        )
        .order_by(ManagedLocalTurn.created_at.asc(), ManagedLocalTurn.id.asc())
    )
    if limit is not None:
        query = query.limit(int(limit))
    return query.all()


def get_next_reviewable_managed_local_turn(
    db: Session,
    *,
    session_id: UUID,
) -> ManagedLocalTurn | None:
    turns = get_reviewable_managed_local_turns(db, session_id=session_id, limit=1)
    return turns[0] if turns else None


def get_managed_local_turn(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
) -> ManagedLocalTurn | None:
    return _get_turn_by_request(db, session_id=session_id, request_id=request_id)


def get_managed_local_turn_snapshot(
    *,
    db_bind,
    session_id: UUID,
    request_id: str,
) -> ManagedLocalTurnSnapshot | None:
    with Session(bind=db_bind) as snapshot_db:
        turn = _get_turn_by_request(snapshot_db, session_id=session_id, request_id=request_id)
        if turn is None:
            return None
        return ManagedLocalTurnSnapshot(
            session_id=session_id,
            request_id=str(turn.request_id or ""),
            baseline_event_id=int(turn.baseline_event_id or 0),
            baseline_runtime_event_id=int(turn.baseline_runtime_event_id or 0),
            send_accepted_at=_normalize_utc(turn.send_accepted_at),
            terminal_phase=str(turn.terminal_phase or "").strip() or None,
            terminal_at=_normalize_utc(turn.terminal_at),
            terminal_runtime_event_id=(int(turn.terminal_runtime_event_id) if turn.terminal_runtime_event_id is not None else None),
            durable_user_event_id=int(turn.durable_user_event_id) if turn.durable_user_event_id is not None else None,
            durable_assistant_event_id=(int(turn.durable_assistant_event_id) if turn.durable_assistant_event_id is not None else None),
            durable_at=_normalize_utc(turn.durable_at),
            review_id=int(turn.review_id) if turn.review_id is not None else None,
            error_code=str(turn.error_code or "").strip() or None,
        )


def _get_turn_by_request(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
) -> ManagedLocalTurn | None:
    return (
        db.query(ManagedLocalTurn)
        .filter(
            ManagedLocalTurn.session_id == session_id,
            ManagedLocalTurn.request_id == str(request_id),
        )
        .one_or_none()
    )


def run_best_effort_managed_local_turn_write(
    *,
    db_bind,
    label: str,
    fn: Callable[[Session], object],
):
    try:
        with Session(bind=db_bind) as ledger_db:
            result = fn(ledger_db)
            ledger_db.commit()
            return result
    except Exception:
        logger.warning("Managed-local turn shadow write failed for %s", label, exc_info=True)
        return None


def _match_durable_turn(
    *,
    events: list[AgentEvent],
    expected_user_text_hash: str,
) -> tuple[AgentEvent, AgentEvent] | None:
    expected_hash = str(expected_user_text_hash or "").strip()
    if not expected_hash:
        return None

    matched_user: AgentEvent | None = None
    last_assistant: AgentEvent | None = None
    for event in events:
        role = str(getattr(event, "role", "") or "").strip().lower()
        content_text = str(getattr(event, "content_text", "") or "")

        if matched_user is None:
            normalized_user_text = strip_claude_channel_wrapper(content_text)
            if role == "user" and hash_managed_local_turn_text(normalized_user_text) == expected_hash:
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
