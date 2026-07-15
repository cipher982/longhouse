"""Provider-neutral durable Console turn creation."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionTurn
from zerg.services.agents.session_graph_writes import ensure_primary_thread
from zerg.services.session_inputs import INPUT_INTENT_AUTO
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import create_session_input_row
from zerg.services.session_turns import SESSION_TURN_STATE_QUEUED
from zerg.services.session_turns import create_session_turn


class ConsoleTurnUnavailable(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConsoleTurnConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class EnqueuedConsoleTurn:
    input_id: int
    turn_id: int
    state: str
    created: bool


def enqueue_console_turn(
    db: Session,
    *,
    session: AgentSession,
    owner_id: int | None,
    message: str,
    client_request_id: str,
) -> EnqueuedConsoleTurn:
    """Atomically create or replay one durable input + queued turn."""

    normalized_message = str(message or "").strip()
    normalized_request_id = str(client_request_id or "").strip()
    if not normalized_message:
        raise ValueError("message is required")
    if not normalized_request_id:
        raise ValueError("client_request_id is required")

    thread = ensure_primary_thread(db, session)
    if not str(thread.device_id or "").strip() or not str(thread.cwd or "").strip():
        raise ConsoleTurnUnavailable(
            "execution_target_missing",
            "Session thread has no Console execution target",
        )

    existing = _existing_turn(
        db,
        session_id=session.id,
        owner_id=owner_id,
        client_request_id=normalized_request_id,
        expected_message=normalized_message,
    )
    if existing is not None:
        return existing

    try:
        input_row = create_session_input_row(
            db,
            session_id=session.id,
            text=normalized_message,
            owner_id=owner_id,
            intent=INPUT_INTENT_AUTO,
            status=INPUT_STATUS_QUEUED,
            client_request_id=normalized_request_id,
        )
        turn = create_session_turn(
            db,
            session_id=session.id,
            request_id=normalized_request_id,
            expected_user_text=normalized_message,
            session_input_id=int(input_row.id),
            initial_state=SESSION_TURN_STATE_QUEUED,
        )
        db.commit()
        return EnqueuedConsoleTurn(
            input_id=int(input_row.id),
            turn_id=int(turn.id),
            state=str(turn.state),
            created=True,
        )
    except IntegrityError:
        db.rollback()
        existing = _existing_turn(
            db,
            session_id=session.id,
            owner_id=owner_id,
            client_request_id=normalized_request_id,
            expected_message=normalized_message,
        )
        if existing is None:
            raise
        return existing


def _existing_turn(
    db: Session,
    *,
    session_id: UUID,
    owner_id: int | None,
    client_request_id: str,
    expected_message: str,
) -> EnqueuedConsoleTurn | None:
    input_query = db.query(SessionInput).filter(
        SessionInput.session_id == session_id,
        SessionInput.client_request_id == client_request_id,
    )
    input_query = (
        input_query.filter(SessionInput.owner_id.is_(None)) if owner_id is None else input_query.filter(SessionInput.owner_id == owner_id)
    )
    input_row = input_query.one_or_none()
    if input_row is None:
        return None
    if str(input_row.body) != expected_message:
        raise ConsoleTurnConflict("client_request_id already belongs to different text")

    turn = (
        db.query(SessionTurn)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.request_id == client_request_id,
        )
        .one_or_none()
    )
    if turn is None or int(turn.session_input_id or 0) != int(input_row.id):
        raise ConsoleTurnConflict("client_request_id has incomplete turn linkage")
    return EnqueuedConsoleTurn(
        input_id=int(input_row.id),
        turn_id=int(turn.id),
        state=str(turn.state),
        created=False,
    )
