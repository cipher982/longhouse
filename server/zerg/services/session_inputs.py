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
from typing import Iterable
from typing import Literal
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.models.agents import SessionInputDeliveryAttempt

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
ACTIVE_DELIVERY_ATTEMPT_STATUSES = frozenset({"acquired", "submitted", "accepted"})

ATTEMPT_STATUS_ACQUIRED = "acquired"
ATTEMPT_STATUS_SUBMITTED = "submitted"
ATTEMPT_STATUS_ACCEPTED = "accepted"
ATTEMPT_STATUS_COMPLETED = "completed"
ATTEMPT_STATUS_RELEASED = "released"
ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_EXPIRED = "expired"

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
    row = create_session_input_row(
        db,
        session_id=session_id,
        text=text,
        intent=intent,
        status=status,
        owner_id=owner_id,
        client_request_id=client_request_id,
        delivery_request_id=delivery_request_id,
    )
    db.commit()
    db.refresh(row)
    return row


def create_session_input_row(
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
    """Create an input without committing so callers can compose one transaction."""

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
    db.flush()
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


def claim_next_queued(
    db: Session,
    session_id: UUID,
    *,
    delivery_request_id: str,
    require_no_active_attempt: bool = False,
    create_attempt: bool = False,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> SessionInput | None:
    """Atomically move the oldest queued input to delivering.

    Returns the claimed row or None if nothing to drain.
    """
    candidate = (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_QUEUED,
            or_(SessionInput.next_attempt_at.is_(None), SessionInput.next_attempt_at <= datetime.now(timezone.utc)),
        )
        .order_by(SessionInput.created_at.asc(), SessionInput.id.asc())
        .first()
    )
    if candidate is None:
        return None

    now = datetime.now(timezone.utc)
    attempt_number = int(getattr(candidate, "attempt_count", 0) or 0) + 1
    claim_query = db.query(SessionInput).filter(
        SessionInput.id == candidate.id,
        SessionInput.status == INPUT_STATUS_QUEUED,
        or_(SessionInput.next_attempt_at.is_(None), SessionInput.next_attempt_at <= now),
    )
    if require_no_active_attempt:
        active_attempt_exists = (
            db.query(SessionInputDeliveryAttempt.id)
            .filter(
                SessionInputDeliveryAttempt.session_id == session_id,
                SessionInputDeliveryAttempt.status.in_(ACTIVE_DELIVERY_ATTEMPT_STATUSES),
                SessionInputDeliveryAttempt.lease_expires_at > now,
            )
            .exists()
        )
        claim_query = claim_query.filter(~active_attempt_exists)

    updates = {
        "status": INPUT_STATUS_DELIVERING,
        "delivery_request_id": delivery_request_id,
        "next_attempt_at": None,
        "updated_at": now,
    }
    if create_attempt:
        updates["attempt_count"] = attempt_number

    try:
        claimed = claim_query.update(
            updates,
            synchronize_session=False,
        )
        if claimed == 1 and create_attempt:
            attempt = SessionInputDeliveryAttempt(
                session_input_id=int(candidate.id),
                session_id=session_id,
                thread_id=getattr(candidate, "thread_id", None),
                owner_id=getattr(candidate, "owner_id", None),
                request_id=delivery_request_id,
                attempt_number=attempt_number,
                status=ATTEMPT_STATUS_ACQUIRED,
                lease_owner=lease_owner or delivery_request_id,
                lease_expires_at=lease_expires_at or (now + timedelta(seconds=60)),
            )
            db.add(attempt)
            db.flush()
            db.query(SessionInput).filter(SessionInput.id == candidate.id).update(
                {
                    "last_attempt_id": int(attempt.id),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    if claimed != 1:
        return None
    db.expire_all()
    return get_session_input(db, int(candidate.id))


def get_delivery_attempt(db: Session, attempt_id: int | None) -> SessionInputDeliveryAttempt | None:
    if attempt_id is None:
        return None
    return db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.id == int(attempt_id)).first()


def mark_delivery_attempt_submitted(db: Session, attempt_id: int, *, submitted_at: datetime | None = None) -> None:
    now = submitted_at or datetime.now(timezone.utc)
    db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.id == attempt_id).update(
        {
            "status": ATTEMPT_STATUS_SUBMITTED,
            "submitted_at": now,
            "updated_at": now,
        },
        synchronize_session=False,
    )
    db.commit()


def mark_delivery_attempt_accepted(
    db: Session,
    attempt_id: int,
    *,
    accepted_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
) -> None:
    now = accepted_at or datetime.now(timezone.utc)
    updates = {
        "status": ATTEMPT_STATUS_ACCEPTED,
        "accepted_at": now,
        "updated_at": now,
    }
    if lease_expires_at is not None:
        updates["lease_expires_at"] = lease_expires_at
    db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.id == attempt_id).update(
        updates,
        synchronize_session=False,
    )
    db.commit()


def mark_delivery_attempt_released(
    db: Session,
    attempt_id: int,
    *,
    released_at: datetime | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    now = released_at or datetime.now(timezone.utc)
    db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.id == attempt_id).update(
        {
            "status": ATTEMPT_STATUS_RELEASED,
            "released_at": now,
            "error_code": error_code,
            "error": str(error)[:500] if error else None,
            "updated_at": now,
        },
        synchronize_session=False,
    )
    db.commit()


def mark_delivery_attempt_failed(
    db: Session,
    attempt_id: int,
    *,
    failed_at: datetime | None = None,
    error_code: str | None = None,
    error: str | None = None,
) -> None:
    now = failed_at or datetime.now(timezone.utc)
    db.query(SessionInputDeliveryAttempt).filter(SessionInputDeliveryAttempt.id == attempt_id).update(
        {
            "status": ATTEMPT_STATUS_FAILED,
            "failed_at": now,
            "error_code": error_code,
            "error": str(error)[:500] if error else None,
            "updated_at": now,
        },
        synchronize_session=False,
    )
    db.commit()


def mark_delivery_attempt_completed(
    db: Session,
    *,
    session_id: UUID,
    request_id: str,
    completed_at: datetime | None = None,
) -> bool:
    now = completed_at or datetime.now(timezone.utc)
    updated = (
        db.query(SessionInputDeliveryAttempt)
        .filter(
            SessionInputDeliveryAttempt.session_id == session_id,
            SessionInputDeliveryAttempt.request_id == request_id,
            SessionInputDeliveryAttempt.status.in_(ACTIVE_DELIVERY_ATTEMPT_STATUSES),
        )
        .update(
            {
                "status": ATTEMPT_STATUS_COMPLETED,
                "completed_at": now,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    return bool(updated)


def expire_delivery_attempts(
    db: Session,
    *,
    session_id: UUID,
    now: datetime | None = None,
    statuses: Iterable[str] = ACTIVE_DELIVERY_ATTEMPT_STATUSES,
) -> int:
    effective_now = now or datetime.now(timezone.utc)
    expired = (
        db.query(SessionInputDeliveryAttempt)
        .filter(
            SessionInputDeliveryAttempt.session_id == session_id,
            SessionInputDeliveryAttempt.status.in_(tuple(statuses)),
            SessionInputDeliveryAttempt.lease_expires_at <= effective_now,
        )
        .update(
            {
                "status": ATTEMPT_STATUS_EXPIRED,
                "updated_at": effective_now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return int(expired)


def requeue_delivering_without_active_attempt(db: Session, *, session_id: UUID, now: datetime | None = None) -> int:
    """Resolve delivering rows that no longer have a live durable attempt.

    Text-only auto/queue rows are safe to retry. Steer and attachments are
    intentionally terminal here: once their attempt is gone, replay would be a
    silent semantic fallback or risk dropping payload bytes.
    """
    effective_now = now or datetime.now(timezone.utc)
    active_attempt_input_ids = (
        db.query(SessionInputDeliveryAttempt.session_input_id)
        .filter(
            SessionInputDeliveryAttempt.session_id == session_id,
            SessionInputDeliveryAttempt.status.in_(ACTIVE_DELIVERY_ATTEMPT_STATUSES),
            SessionInputDeliveryAttempt.lease_expires_at > effective_now,
        )
        .subquery()
    )
    attached_input_ids = db.query(SessionInputAttachment.session_input_id)
    requeued = (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_DELIVERING,
            ~SessionInput.id.in_(active_attempt_input_ids),
            SessionInput.intent != INPUT_INTENT_STEER,
            ~SessionInput.id.in_(attached_input_ids),
        )
        .update(
            {
                "status": INPUT_STATUS_QUEUED,
                "delivery_request_id": None,
                "updated_at": effective_now,
            },
            synchronize_session=False,
        )
    )
    failed_with_attachments = (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_DELIVERING,
            ~SessionInput.id.in_(active_attempt_input_ids),
            SessionInput.intent != INPUT_INTENT_STEER,
            SessionInput.id.in_(attached_input_ids),
        )
        .update(
            {
                "status": INPUT_STATUS_FAILED,
                "last_error": "attachment delivery interrupted before accepted attempt",
                "updated_at": effective_now,
            },
            synchronize_session=False,
        )
    )
    failed_steer = (
        db.query(SessionInput)
        .filter(
            SessionInput.session_id == session_id,
            SessionInput.status == INPUT_STATUS_DELIVERING,
            ~SessionInput.id.in_(active_attempt_input_ids),
            SessionInput.intent == INPUT_INTENT_STEER,
        )
        .update(
            {
                "status": INPUT_STATUS_FAILED,
                "last_error": "steer delivery interrupted before accepted attempt",
                "updated_at": effective_now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if requeued:
        logger.info("Requeued %d SessionInput rows without an active delivery attempt", requeued)
    if failed_with_attachments:
        logger.info("Failed %d attachment SessionInput rows without an active delivery attempt", failed_with_attachments)
    if failed_steer:
        logger.info("Failed %d steer SessionInput rows without an active delivery attempt", failed_steer)
    return int(requeued)


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


def requeue_delivering(
    db: Session,
    input_id: int,
    *,
    error: str | None = None,
    next_attempt_at: datetime | None = None,
) -> None:
    """Move a claimed input back to queued after a transient dispatch miss."""
    db.query(SessionInput).filter(
        SessionInput.id == input_id,
        SessionInput.status == INPUT_STATUS_DELIVERING,
    ).update(
        {
            "status": INPUT_STATUS_QUEUED,
            "delivery_request_id": None,
            "next_attempt_at": next_attempt_at,
            "last_error": str(error)[:500] if error else None,
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
