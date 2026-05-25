"""Durable user-originated session inputs.

Separate from `SessionMessage` (agent-to-agent). Records user text targeted at
a managed session, along with the user's intent and the lifecycle status.

Status lifecycle:
  queued -> delivering -> delivered | failed
  queued -> cancelled  (user-initiated)
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment

logger = logging.getLogger(__name__)

InputIntent = Literal["auto", "queue", "steer"]
InputStatus = Literal["queued", "delivering", "delivered", "cancelled", "failed"]
RetryInputStatus = Literal["queued", "delivering"]
InputOutcome = Literal["sent", "queued"]
InputConflictReason = Literal["different_text", "cancelled"]

INPUT_INTENT_AUTO: InputIntent = "auto"
INPUT_INTENT_QUEUE: InputIntent = "queue"
INPUT_INTENT_STEER: InputIntent = "steer"

INPUT_STATUS_QUEUED: InputStatus = "queued"
INPUT_STATUS_DELIVERING: InputStatus = "delivering"
INPUT_STATUS_DELIVERED: InputStatus = "delivered"
INPUT_STATUS_CANCELLED: InputStatus = "cancelled"
INPUT_STATUS_FAILED: InputStatus = "failed"

VALID_INTENTS: frozenset[InputIntent] = frozenset({INPUT_INTENT_AUTO, INPUT_INTENT_QUEUE, INPUT_INTENT_STEER})

# Startup reconciliation: any `delivering` row older than this at boot is
# considered wedged (the process died mid-dispatch) and rewound to queued.
DELIVERING_STALE_AFTER_SECS = 60.0


MAX_QUEUED_PER_SESSION = 5


def create_session_input(
    db: Session,
    *,
    session_id: UUID,
    text: str,
    intent: InputIntent,
    status: InputStatus,
    owner_id: int | None = None,
    client_request_id: str | None = None,
    delivery_request_id: str | None = None,
) -> SessionInput:
    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent}")
    # Phase 2: stamp thread_id so Phase 3 can flip session_inputs to
    # thread-keyed without a separate backfill pass.
    from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

    thread_id = ensure_thread_id_for_session(db, session_id)
    row = SessionInput(
        session_id=session_id,
        thread_id=thread_id,
        body=text,
        owner_id=owner_id,
        intent=intent,
        status=status,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def count_queued(db: Session, session_id: UUID) -> int:
    return (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_QUEUED,
        )
        .count()
    )


def list_queued_inputs(db: Session, session_id: UUID) -> list[SessionInput]:
    return (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_QUEUED,
        )
        .order_by(SessionInput.created_at.asc(), SessionInput.id.asc())
        .all()
    )


RECENT_FAILED_WINDOW_SECS = 15 * 60


def list_recent_inputs(db: Session, session_id: UUID) -> list[SessionInput]:
    """Queued rows + recently-failed rows so the UI can surface drain failures.

    Delivered/cancelled rows are excluded — they've already served their
    purpose and shouldn't clutter the chip area.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECENT_FAILED_WINDOW_SECS)
    return (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            (
                (SessionInput.status == INPUT_STATUS_QUEUED)
                | (SessionInput.status == INPUT_STATUS_DELIVERING)
                | ((SessionInput.status == INPUT_STATUS_FAILED) & (SessionInput.updated_at >= cutoff))
            ),
        )
        .order_by(SessionInput.created_at.asc(), SessionInput.id.asc())
        .all()
    )


def get_session_input(db: Session, input_id: int) -> SessionInput | None:
    return db.query(SessionInput).filter(SessionInput.id == input_id).first()


def cancel_queued_input(db: Session, input_id: int) -> SessionInput | None:
    """Atomically transition a queued input to cancelled.

    Uses a status-gated UPDATE so concurrent cancels do not both succeed.
    SQLite's single-writer lock was already serializing this, but the SQL
    is portable: one of N concurrent callers sees rowcount==1, the rest
    see rowcount==0 and report the row as no longer cancellable.
    """
    now = datetime.now(timezone.utc)
    updated = (
        db.query(SessionInput)
        .filter(
            SessionInput.id == input_id,
            SessionInput.status == INPUT_STATUS_QUEUED,
        )
        .update(
            {"status": INPUT_STATUS_CANCELLED, "updated_at": now},
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        return None
    # Return the refreshed row so the caller can report id/status.
    return get_session_input(db, input_id)


def claim_next_queued(db: Session, session_id: UUID, *, delivery_request_id: str) -> SessionInput | None:
    """Atomically move the oldest queued input to delivering.

    Returns the claimed row or None if nothing to drain.
    """
    candidate = (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_QUEUED,
        )
        .order_by(SessionInput.created_at.asc(), SessionInput.id.asc())
        .first()
    )
    if candidate is None:
        return None

    claimed = (
        db.query(SessionInput)
        .filter(
            SessionInput.id == candidate.id,
            SessionInput.status == INPUT_STATUS_QUEUED,
        )
        .update(
            {
                "status": INPUT_STATUS_DELIVERING,
                "delivery_request_id": delivery_request_id,
                "updated_at": datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed != 1:
        return None
    db.refresh(candidate)
    return candidate


def retry_failed_input(
    db: Session,
    input_id: int,
    *,
    intent: InputIntent,
    status: RetryInputStatus,
    delivery_request_id: str | None = None,
) -> SessionInput | None:
    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent}")
    if status not in {INPUT_STATUS_QUEUED, INPUT_STATUS_DELIVERING}:
        raise ValueError(f"invalid retry status: {status}")
    now = datetime.now(timezone.utc)
    updated = (
        db.query(SessionInput)
        .filter(SessionInput.id == input_id, SessionInput.status == INPUT_STATUS_FAILED)
        .update(
            {
                "intent": intent,
                "status": status,
                "delivery_request_id": delivery_request_id,
                "last_error": None,
                "delivered_at": None,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        return None
    db.expire_all()
    return get_session_input(db, input_id)


def mark_delivered(db: Session, input_id: int) -> None:
    now = datetime.now(timezone.utc)
    db.query(SessionInput).filter(SessionInput.id == input_id).update(
        {
            "status": INPUT_STATUS_DELIVERED,
            "delivered_at": now,
            "updated_at": now,
            "last_error": None,
        },
        synchronize_session=False,
    )
    db.commit()


def mark_failed(db: Session, input_id: int, *, error: str) -> None:
    db.query(SessionInput).filter(SessionInput.id == input_id).update(
        {
            "status": INPUT_STATUS_FAILED,
            "last_error": str(error or "session input delivery failed")[:500],
            "updated_at": datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )
    db.commit()


def requeue_stuck_delivering(db: Session, *, stale_after_secs: float = DELIVERING_STALE_AFTER_SECS) -> int:
    """Resolve `delivering` rows older than the threshold.

    Called once at runtime startup; wedged rows usually mean the process died
    mid-dispatch.

    - `intent=auto` / `intent=queue` rows without attachments: rewind to
      `queued` so the next terminal-phase drain picks them up. Safe because
      the user asked for either "best-effort dispatch" or "wait for the next
      boundary" — both are compatible with a later retry.
    - rows with attachments: transition to `failed`, never requeue. The
      queued-input drain currently carries text only; requeueing would silently
      resend a screenshot turn without the screenshots.
    - `intent=steer` rows: transition to `failed`, never requeue. Steer is
      a corrective, time-sensitive intent; converting it into a queued
      message after a crash would be a silent fallback that violates the
      no-silent-fallback contract.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=stale_after_secs)

    attached_input_ids = db.query(SessionInputAttachment.session_input_id)

    # Intent-aware split.
    requeued = (
        db.query(SessionInput)
        .filter(
            SessionInput.status == INPUT_STATUS_DELIVERING,
            SessionInput.updated_at < cutoff,
            SessionInput.intent != INPUT_INTENT_STEER,
            ~SessionInput.id.in_(attached_input_ids),
        )
        .update(
            {
                "status": INPUT_STATUS_QUEUED,
                "delivery_request_id": None,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    failed_with_attachments = (
        db.query(SessionInput)
        .filter(
            SessionInput.status == INPUT_STATUS_DELIVERING,
            SessionInput.updated_at < cutoff,
            SessionInput.intent != INPUT_INTENT_STEER,
            SessionInput.id.in_(attached_input_ids),
        )
        .update(
            {
                "status": INPUT_STATUS_FAILED,
                "last_error": "attachment delivery interrupted by restart",
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    failed = (
        db.query(SessionInput)
        .filter(
            SessionInput.status == INPUT_STATUS_DELIVERING,
            SessionInput.updated_at < cutoff,
            SessionInput.intent == INPUT_INTENT_STEER,
        )
        .update(
            {
                "status": INPUT_STATUS_FAILED,
                "last_error": "steer interrupted by restart",
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if requeued:
        logger.info("Requeued %d stuck SessionInput rows from delivering -> queued", requeued)
    if failed_with_attachments:
        logger.info(
            "Marked %d stuck attachment SessionInput rows as failed (no text-only requeue)",
            failed_with_attachments,
        )
    if failed:
        logger.info("Marked %d stuck steer SessionInput rows as failed (no silent requeue)", failed)
    return int(requeued)


def reconcile_startup_session_inputs(db: Session) -> list[UUID]:
    """Boot-time recovery contract for durable session inputs.

    Reconcile interrupted deliveries, then return the distinct sessions that
    still have queued inputs so the runtime can schedule a best-effort drain.
    """
    requeue_stuck_delivering(db)
    rows = db.query(SessionInput.session_id).filter(SessionInput.status == INPUT_STATUS_QUEUED).distinct().all()
    return [row[0] for row in rows]
