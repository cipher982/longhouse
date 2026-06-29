"""Managed session input queue wake/readiness service.

Phase 1+2 centralizes the drain decision without changing the public
SessionInput lifecycle. Delivery attempts are modeled in the DB in this phase,
but the durable attempt lease becomes authoritative in the next phase.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputDeliveryAttempt
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTurn
from zerg.models.user import User
from zerg.services.managed_control_dispatcher import MANAGED_CONTROL_UNAVAILABLE_ERROR
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_current_control import current_session_capabilities
from zerg.services.session_inputs import ACTIVE_DELIVERY_ATTEMPT_STATUSES
from zerg.services.session_inputs import ATTEMPT_STATUS_ACQUIRED
from zerg.services.session_inputs import ATTEMPT_STATUS_SUBMITTED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import claim_next_queued
from zerg.services.session_inputs import expire_delivery_attempts
from zerg.services.session_inputs import get_delivery_attempt
from zerg.services.session_inputs import mark_delivered
from zerg.services.session_inputs import mark_delivery_attempt_accepted
from zerg.services.session_inputs import mark_delivery_attempt_failed
from zerg.services.session_inputs import mark_delivery_attempt_released
from zerg.services.session_inputs import mark_delivery_attempt_submitted
from zerg.services.session_inputs import mark_failed
from zerg.services.session_inputs import requeue_delivering
from zerg.services.session_inputs import requeue_delivering_without_active_attempt
from zerg.services.session_kernel_projection import session_lock_scope_id
from zerg.services.session_runtime import session_is_closed_for_input
from zerg.services.session_turns import SESSION_TURN_ERROR_SEND_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_SEND_ACCEPTED

logger = logging.getLogger(__name__)

QUEUE_DRAINABLE_RUNTIME_PHASES = frozenset({"idle", "needs_user", "blocked"})
ACTIVE_TURN_STATES = frozenset({SESSION_TURN_STATE_SEND_ACCEPTED, SESSION_TURN_STATE_ACTIVE})
TRANSPORT_LEASE_SECS = 60
TURN_LEASE_SECS = 300
MAX_DELIVERY_ATTEMPTS = 5
RETRY_BACKOFF_SECS = (5, 30, 120, 300)


@dataclass(frozen=True)
class QueueReadiness:
    ready: bool
    reason: str


@dataclass(frozen=True)
class QueueWakeResult:
    dispatched: bool = False
    input_id: int | None = None
    reason: str = "noop"


def _session_closed_for_input(db: Session, session_id: UUID) -> bool:
    return session_is_closed_for_input(db, session_id)


def _resolve_session_owner_id(db: Session) -> int:
    owner = db.query(User.id).order_by(User.id.asc()).first()
    if owner is None:
        raise RuntimeError("No Longhouse user is configured")
    return int(owner[0])


def _is_transient_managed_control_unavailable(error_code: str | None, error_message: str | None) -> bool:
    if error_code != SESSION_TURN_ERROR_SEND_FAILED:
        return False
    message = str(error_message or "")
    transient_fragments = (
        MANAGED_CONTROL_UNAVAILABLE_ERROR,
        "Machine Agent control channel is offline",
        "Failed to send command to Machine Agent control channel",
    )
    return any(fragment in message for fragment in transient_fragments)


def _latest_runtime_phase(db: Session, session_id: UUID) -> str | None:
    runtime_state = (
        db.query(SessionRuntimeState)
        .filter(SessionRuntimeState.session_id == session_id)
        .order_by(SessionRuntimeState.updated_at.desc(), SessionRuntimeState.runtime_version.desc())
        .first()
    )
    if runtime_state is None:
        return None
    return str(getattr(runtime_state, "phase", "") or "").strip() or None


def _has_active_non_terminal_turn(db: Session, session_id: UUID) -> bool:
    return (
        db.query(SessionTurn.id)
        .filter(
            SessionTurn.session_id == session_id,
            SessionTurn.state.in_(ACTIVE_TURN_STATES),
        )
        .limit(1)
        .first()
        is not None
    )


def _has_unexpired_active_attempt(db: Session, session_id: UUID, *, now: datetime | None = None) -> bool:
    effective_now = now or datetime.now(timezone.utc)
    return (
        db.query(SessionInputDeliveryAttempt.id)
        .filter(
            SessionInputDeliveryAttempt.session_id == session_id,
            SessionInputDeliveryAttempt.status.in_(ACTIVE_DELIVERY_ATTEMPT_STATUSES),
            SessionInputDeliveryAttempt.lease_expires_at > effective_now,
        )
        .limit(1)
        .first()
        is not None
    )


def evaluate_session_input_queue_readiness(
    db: Session,
    *,
    session: AgentSession,
    owner_id: int | None,
) -> QueueReadiness:
    """Return whether the managed session can accept the next queued input."""
    session_id = session.id
    if _session_closed_for_input(db, session_id):
        return QueueReadiness(False, "closed")

    if not current_session_capabilities(db, session, owner_id=owner_id).live_control_available:
        return QueueReadiness(False, "control_unavailable")

    if _has_unexpired_active_attempt(db, session_id):
        return QueueReadiness(False, "lease_active")

    if _has_active_non_terminal_turn(db, session_id):
        return QueueReadiness(False, "active_turn")

    runtime_phase = _latest_runtime_phase(db, session_id)
    if runtime_phase is None:
        return QueueReadiness(False, "runtime_unknown")
    if runtime_phase not in QUEUE_DRAINABLE_RUNTIME_PHASES:
        return QueueReadiness(False, "runtime_busy")

    return QueueReadiness(True, "ready")


async def wake_session_input_queue(
    *,
    db_bind,
    session_id: UUID,
    reason: str,
    lock_scope_id: str | None = None,
) -> QueueWakeResult:
    """Wake the per-session input queue and dispatch at most one queued row."""
    SessionLocal = sessionmaker(bind=db_bind, expire_on_commit=False)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expire_delivery_attempts(db, session_id=session_id, now=now, statuses=(ATTEMPT_STATUS_ACQUIRED, ATTEMPT_STATUS_SUBMITTED))
        requeue_delivering_without_active_attempt(db, session_id=session_id, now=now)

        queued_exists = (
            db.query(SessionInput)
            .filter(
                SessionInput.session_id == session_id,
                SessionInput.status == INPUT_STATUS_QUEUED,
                or_(SessionInput.next_attempt_at.is_(None), SessionInput.next_attempt_at <= now),
            )
            .order_by(SessionInput.created_at.asc(), SessionInput.id.asc())
            .first()
        )
        if queued_exists is None:
            pending_retry = (
                db.query(SessionInput.id)
                .filter(
                    SessionInput.session_id == session_id,
                    SessionInput.status == INPUT_STATUS_QUEUED,
                    SessionInput.next_attempt_at.isnot(None),
                    SessionInput.next_attempt_at > now,
                )
                .first()
            )
            if pending_retry is not None:
                return QueueWakeResult(reason="next_attempt_pending")
            return QueueWakeResult(reason="no_queued_input")

        source_session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if source_session is None:
            logger.warning("Queue wake aborted: session %s not found", session_id)
            return QueueWakeResult(reason="session_missing")

        readiness = evaluate_session_input_queue_readiness(
            db,
            session=source_session,
            owner_id=queued_exists.owner_id,
        )
        if not readiness.ready:
            logger.info("Queue wake deferred for session %s after %s: %s", session_id, reason, readiness.reason)
            return QueueWakeResult(reason=readiness.reason)

        lock_scope = lock_scope_id or session_lock_scope_id(source_session.id)
        drain_request_id = f"drain-{uuid4().hex}"
        lock = await session_lock_manager.acquire(
            session_id=lock_scope,
            holder=drain_request_id,
            ttl_seconds=300,
        )
        if not lock:
            logger.info(
                "Queue wake yielded for session %s after %s: lock already held",
                session_id,
                reason,
            )
            return QueueWakeResult(reason="lock_active")

        claimed = claim_next_queued(
            db,
            session_id,
            delivery_request_id=drain_request_id,
            require_no_active_attempt=True,
            create_attempt=True,
            lease_owner=drain_request_id,
            lease_expires_at=now + timedelta(seconds=TRANSPORT_LEASE_SECS),
        )
        if claimed is None:
            await session_lock_manager.release(lock_scope, drain_request_id)
            return QueueWakeResult(reason="claim_raced")

        result = await _dispatch_claimed_input(
            db=db,
            source_session=source_session,
            claimed=claimed,
            lock_scope=lock_scope,
            drain_request_id=drain_request_id,
        )
        if result.dispatched:
            logger.info("Queue wake drained SessionInput %s for session %s after %s", claimed.id, session_id, reason)
        return result
    finally:
        db.close()


async def _dispatch_claimed_input(
    *,
    db: Session,
    source_session: AgentSession,
    claimed: SessionInput,
    lock_scope: str,
    drain_request_id: str,
) -> QueueWakeResult:
    from zerg.services.session_chat_impl import _dispatch_managed_local_text

    attempt = get_delivery_attempt(db, getattr(claimed, "last_attempt_id", None))
    attempt_id = int(attempt.id) if attempt is not None else None
    recorded_owner = getattr(claimed, "owner_id", None)
    owner_id = int(recorded_owner) if recorded_owner else _resolve_session_owner_id(db)

    try:
        if attempt_id is not None:
            mark_delivery_attempt_submitted(db, attempt_id, submitted_at=datetime.now(timezone.utc))
        dispatch_response = await _dispatch_managed_local_text(
            source_session=source_session,
            owner_id=owner_id,
            message=claimed.body,
            request_id=drain_request_id,
            lock_scope_id=lock_scope,
            db=db,
            session_input_id=int(claimed.id),
        )
    except Exception as exc:
        if attempt_id is not None:
            mark_delivery_attempt_failed(db, attempt_id, error_code="dispatch_exception", error=str(exc))
        mark_failed(db, int(claimed.id), error=str(exc)[:200])
        await session_lock_manager.release(lock_scope, drain_request_id)
        logger.exception("Queue dispatch failed for SessionInput %s", claimed.id)
        return QueueWakeResult(input_id=int(claimed.id), reason="dispatch_exception")

    dispatch_status = int(getattr(dispatch_response, "status_code", 200) or 200)
    if dispatch_status >= 400:
        response_error_code = "send_failed"
        response_error_message = f"drain dispatch returned {dispatch_status}"
        try:
            response_body = json.loads(getattr(dispatch_response, "body", b"{}") or b"{}")
            if isinstance(response_body, dict):
                response_error_code = str(response_body.get("error_code") or response_error_code)
                response_error_message = str(response_body.get("error") or response_error_message)
        except Exception:
            pass
        if _is_transient_managed_control_unavailable(response_error_code, response_error_message):
            attempt_count = int(getattr(claimed, "attempt_count", 0) or 0)
            if attempt_count >= MAX_DELIVERY_ATTEMPTS:
                if attempt_id is not None:
                    mark_delivery_attempt_failed(
                        db,
                        attempt_id,
                        error_code=response_error_code,
                        error=response_error_message,
                    )
                mark_failed(db, int(claimed.id), error=response_error_message)
                return QueueWakeResult(input_id=int(claimed.id), reason="max_attempts_failed")
            backoff = RETRY_BACKOFF_SECS[min(max(attempt_count, 1) - 1, len(RETRY_BACKOFF_SECS) - 1)]
            next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            if attempt_id is not None:
                mark_delivery_attempt_released(
                    db,
                    attempt_id,
                    error_code=response_error_code,
                    error=response_error_message,
                )
            requeue_delivering(db, int(claimed.id), error=response_error_message, next_attempt_at=next_attempt_at)
            logger.info(
                "Queue dispatch deferred for SessionInput %s on session %s: %s",
                claimed.id,
                source_session.id,
                response_error_message,
            )
            return QueueWakeResult(input_id=int(claimed.id), reason="transient_dispatch_failure")
        if attempt_id is not None:
            mark_delivery_attempt_failed(
                db,
                attempt_id,
                error_code=response_error_code,
                error=response_error_message,
            )
        mark_failed(
            db,
            int(claimed.id),
            error=response_error_message,
        )
        logger.warning(
            "Queue dispatch returned %s for SessionInput %s",
            dispatch_status,
            claimed.id,
        )
        return QueueWakeResult(input_id=int(claimed.id), reason="dispatch_failed")

    if attempt_id is not None:
        now = datetime.now(timezone.utc)
        mark_delivery_attempt_accepted(
            db,
            attempt_id,
            accepted_at=now,
            lease_expires_at=now + timedelta(seconds=TURN_LEASE_SECS),
        )
    mark_delivered(db, int(claimed.id))
    return QueueWakeResult(dispatched=True, input_id=int(claimed.id), reason="dispatched")


__all__ = [
    "QueueReadiness",
    "QueueWakeResult",
    "evaluate_session_input_queue_readiness",
    "wake_session_input_queue",
]
