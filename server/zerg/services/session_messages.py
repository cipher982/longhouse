from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID

from sqlalchemy.orm import Session

import zerg.services.live_session_dispatch as live_session_dispatch
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.user import User
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_runtime import current_presence_state_for_session

logger = logging.getLogger(__name__)

MESSAGE_STATUS_QUEUED = "queued"
MESSAGE_STATUS_DELIVERING = "delivering"
MESSAGE_STATUS_DELIVERED = "delivered"
MESSAGE_STATUS_STORED_ONLY = "stored_only"
MESSAGE_STATUS_FAILED = "failed"

MESSAGE_DELIVERABLE_STATES = {"idle", "thinking", "needs_user"}
MAX_MESSAGES_PER_SAFE_BOUNDARY = 10


@dataclass
class SessionMessageDispatchOutcome:
    message: SessionMessage
    delivery_status: str
    error: str | None = None


def resolve_session_message_owner_id(db: Session, token: object | None) -> int | None:
    owner_id = getattr(token, "owner_id", None)
    if owner_id is not None:
        return int(owner_id)

    owner = db.query(User.id).order_by(User.id).first()
    if owner is None:
        return None
    return int(owner[0])


def is_session_message_deliverable_state(state: str | None) -> bool:
    return state in MESSAGE_DELIVERABLE_STATES


def _build_injected_message(from_session: AgentSession, message: SessionMessage) -> str:
    device_name = (
        str(getattr(from_session, "device_name", "") or "").strip()
        or str(getattr(from_session, "source_runner_name", "") or "").strip()
        or str(getattr(from_session, "device_id", "") or "").strip()
        or "unknown-device"
    )
    return "\n".join(
        [
            f"[Message #{message.id} from session {from_session.id} on {device_name}]",
            message.body,
            f"[End message — use session_tail({from_session.id}) for full context]",
        ]
    )


def _mark_message_failed(message: SessionMessage, *, error: str | None) -> None:
    message.delivery_status = MESSAGE_STATUS_FAILED
    message.last_error = str(error or "Message delivery failed")
    message.delivery_attempts = int(getattr(message, "delivery_attempts", 0) or 0) + 1


async def deliver_next_queued_session_message(
    *,
    db: Session,
    owner_id: int | None,
    target_session_id: UUID,
    target_presence_state: str | None = None,
) -> SessionMessageDispatchOutcome | None:
    target_session = db.query(AgentSession).filter(AgentSession.id == target_session_id).first()
    if target_session is None or not current_session_capabilities(db, target_session, owner_id=owner_id).live_control_available:
        return None

    current_state = target_presence_state or current_presence_state_for_session(
        db,
        target_session_id,
        session=target_session,
    )
    if not is_session_message_deliverable_state(current_state):
        return None

    queued_message = (
        db.query(SessionMessage)
        .filter(
            SessionMessage.to_session_id == target_session_id,
            SessionMessage.delivery_status == MESSAGE_STATUS_QUEUED,
        )
        .order_by(SessionMessage.created_at.asc(), SessionMessage.id.asc())
        .first()
    )
    if queued_message is None:
        return None

    claimed = (
        db.query(SessionMessage)
        .filter(
            SessionMessage.id == queued_message.id,
            SessionMessage.delivery_status == MESSAGE_STATUS_QUEUED,
        )
        .update(
            {
                "delivery_status": MESSAGE_STATUS_DELIVERING,
                "updated_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed != 1:
        return None

    message = db.query(SessionMessage).filter(SessionMessage.id == queued_message.id).one()
    from_session = db.query(AgentSession).filter(AgentSession.id == message.from_session_id).first()
    if from_session is None:
        _mark_message_failed(message, error="Sender session not found")
        db.commit()
        return SessionMessageDispatchOutcome(
            message=message,
            delivery_status=message.delivery_status,
            error=message.last_error,
        )

    if owner_id is None:
        _mark_message_failed(message, error="No owner available for live session delivery")
        db.commit()
        return SessionMessageDispatchOutcome(
            message=message,
            delivery_status=message.delivery_status,
            error=message.last_error,
        )

    injected_text = _build_injected_message(from_session, message)
    send_result = await live_session_dispatch.send_text_to_live_session(
        db=db,
        owner_id=owner_id,
        session=target_session,
        text=injected_text,
        request_id=f"session-message-{message.id}",
        timeout_secs=15,
        verify_turn_started=True,
        verification_timeout_secs=15.0,
    )
    if not send_result.ok:
        _mark_message_failed(message, error=send_result.error)
        db.commit()
        return SessionMessageDispatchOutcome(
            message=message,
            delivery_status=message.delivery_status,
            error=message.last_error,
        )

    message.delivery_status = MESSAGE_STATUS_DELIVERED
    message.delivery_attempts = int(getattr(message, "delivery_attempts", 0) or 0) + 1
    message.last_error = None
    message.delivered_via = live_session_dispatch.live_text_dispatch_label(db, target_session)
    message.delivered_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(message)
    return SessionMessageDispatchOutcome(message=message, delivery_status=message.delivery_status)


async def deliver_queued_session_messages(
    *,
    db: Session,
    owner_id: int | None,
    target_session_id: UUID,
    target_presence_state: str | None = None,
    max_messages: int = MAX_MESSAGES_PER_SAFE_BOUNDARY,
) -> list[SessionMessageDispatchOutcome]:
    """Deliver queued messages while the target session stays in a safe state."""

    outcomes: list[SessionMessageDispatchOutcome] = []
    current_state = target_presence_state

    for _ in range(max_messages):
        if not is_session_message_deliverable_state(current_state or current_presence_state_for_session(db, target_session_id)):
            break

        outcome = await deliver_next_queued_session_message(
            db=db,
            owner_id=owner_id,
            target_session_id=target_session_id,
            target_presence_state=current_state,
        )
        if outcome is None:
            break

        outcomes.append(outcome)
        if outcome.delivery_status != MESSAGE_STATUS_DELIVERED:
            break

        current_state = current_presence_state_for_session(db, target_session_id)

    return outcomes


async def create_session_message(
    *,
    db: Session,
    owner_id: int | None,
    from_session_id: UUID,
    to_session_id: UUID,
    text: str,
    source_event_id: int | None = None,
) -> SessionMessageDispatchOutcome:
    from_session = db.query(AgentSession).filter(AgentSession.id == from_session_id).first()
    if from_session is None:
        raise ValueError("Sender session not found")

    to_session = db.query(AgentSession).filter(AgentSession.id == to_session_id).first()
    if to_session is None:
        raise ValueError("Target session not found")

    if from_session_id == to_session_id:
        raise ValueError("Cannot send a session message to the same session")

    initial_status = (
        MESSAGE_STATUS_QUEUED
        if current_session_capabilities(db, to_session, owner_id=owner_id).live_control_available
        else MESSAGE_STATUS_STORED_ONLY
    )
    message = SessionMessage(
        from_session_id=from_session_id,
        to_session_id=to_session_id,
        body=text,
        source_event_id=source_event_id,
        delivery_status=initial_status,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    if initial_status == MESSAGE_STATUS_STORED_ONLY:
        return SessionMessageDispatchOutcome(message=message, delivery_status=message.delivery_status)

    current_state = current_presence_state_for_session(db, to_session_id, session=to_session)
    if is_session_message_deliverable_state(current_state):
        await deliver_queued_session_messages(
            db=db,
            owner_id=owner_id,
            target_session_id=to_session_id,
            target_presence_state=current_state,
        )
        db.refresh(message)

    return SessionMessageDispatchOutcome(message=message, delivery_status=message.delivery_status)
