"""Hot-lane receipts for user text input control work."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.services.session_inputs import INPUT_STATUS_CANCELLED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import INPUT_STATUS_QUEUED
from zerg.services.session_inputs import RECENT_FAILED_WINDOW_SECS
from zerg.services.write_serializer import get_live_write_serializer
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

LIVE_INPUT_RECEIPT_WRITE_TIMEOUT_SECS = 0.25


@dataclass(frozen=True)
class LiveInputReceiptSnapshot:
    id: str
    owner_id: int
    session_id: str
    provider: str
    text: str
    intent: str
    status: str
    client_request_id: str | None
    archive_session_input_id: int | None
    delivery_request_id: str | None = None
    error_json: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _session_key(session_id: UUID | str) -> str:
    return str(session_id)


def _clean_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


def _snapshot(row: LiveSessionInputReceipt) -> LiveInputReceiptSnapshot:
    return LiveInputReceiptSnapshot(
        id=str(row.id),
        owner_id=int(row.owner_id),
        session_id=str(row.session_id),
        provider=str(row.provider),
        text=str(row.text or ""),
        intent=str(row.intent or "auto"),
        status=str(row.status or "created"),
        client_request_id=row.client_request_id,
        archive_session_input_id=(int(row.archive_session_input_id) if row.archive_session_input_id is not None else None),
        delivery_request_id=row.delivery_request_id,
        error_json=row.error_json,
        created_at=normalize_utc(row.created_at),
        updated_at=normalize_utc(row.updated_at),
    )


def get_live_input_receipt_by_client_request(
    db: Session,
    *,
    owner_id: int,
    session_id: UUID | str,
    client_request_id: str | None,
) -> LiveInputReceiptSnapshot | None:
    client_key = _clean_str(client_request_id)
    if client_key is None:
        return None
    row = (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.owner_id == int(owner_id),
            LiveSessionInputReceipt.session_id == _session_key(session_id),
            LiveSessionInputReceipt.client_request_id == client_key,
        )
        .first()
    )
    return _snapshot(row) if row is not None else None


def load_live_input_receipt_by_id(
    db: Session,
    *,
    receipt_id: str,
) -> LiveInputReceiptSnapshot | None:
    row = db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.id == str(receipt_id)).first()
    return _snapshot(row) if row is not None else None


def list_recent_live_input_receipts(db: Session, *, session_id: UUID | str) -> list[LiveInputReceiptSnapshot]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECENT_FAILED_WINDOW_SECS)
    rows = (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.session_id == _session_key(session_id),
            (
                (LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED)
                | (LiveSessionInputReceipt.status == INPUT_STATUS_DELIVERING)
                | ((LiveSessionInputReceipt.status == INPUT_STATUS_FAILED) & (LiveSessionInputReceipt.updated_at >= cutoff))
            ),
        )
        .order_by(LiveSessionInputReceipt.created_at.asc(), LiveSessionInputReceipt.id.asc())
        .all()
    )
    return [_snapshot(row) for row in rows]


def count_live_queued_receipts(db: Session, *, session_id: UUID | str) -> int:
    return (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.session_id == _session_key(session_id),
            LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED,
        )
        .count()
    )


def list_session_ids_with_queued_live_receipts(db: Session, *, limit: int) -> list[UUID]:
    rows = (
        db.query(LiveSessionInputReceipt.session_id)
        .filter(LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED)
        .distinct()
        .limit(max(1, int(limit)))
        .all()
    )
    session_ids: list[UUID] = []
    for row in rows:
        try:
            session_ids.append(UUID(str(row[0])))
        except (TypeError, ValueError):
            logger.warning("Ignoring queued live input receipt with invalid session_id=%r", row[0])
    return session_ids


def cancel_live_queued_receipt(
    db: Session,
    *,
    session_id: UUID | str,
    receipt_id: str,
) -> LiveInputReceiptSnapshot | None:
    now = datetime.now(timezone.utc)
    updated = (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.session_id == _session_key(session_id),
            LiveSessionInputReceipt.id == str(receipt_id),
            LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED,
        )
        .update(
            {
                "status": INPUT_STATUS_CANCELLED,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated != 1:
        return None
    return load_live_input_receipt_by_id(db, receipt_id=str(receipt_id))


def claim_next_live_queued_receipt(
    db: Session,
    *,
    session_id: UUID | str,
    delivery_request_id: str,
) -> LiveInputReceiptSnapshot | None:
    candidate = (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.session_id == _session_key(session_id),
            LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED,
        )
        .order_by(LiveSessionInputReceipt.created_at.asc(), LiveSessionInputReceipt.id.asc())
        .first()
    )
    if candidate is None:
        return None
    now = datetime.now(timezone.utc)
    claimed = (
        db.query(LiveSessionInputReceipt)
        .filter(
            LiveSessionInputReceipt.id == candidate.id,
            LiveSessionInputReceipt.status == INPUT_STATUS_QUEUED,
        )
        .update(
            {
                "status": INPUT_STATUS_DELIVERING,
                "delivery_request_id": str(delivery_request_id),
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed != 1:
        return None
    return load_live_input_receipt_by_id(db, receipt_id=str(candidate.id))


def mark_live_receipt_delivered_with_projection(
    db: Session,
    *,
    receipt_id: str,
    delivery_request_id: str,
) -> LiveInputReceiptSnapshot | None:
    row = db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.id == str(receipt_id)).first()
    if row is None:
        return None
    row.status = INPUT_STATUS_DELIVERED
    row.delivery_request_id = str(delivery_request_id)
    row.updated_at = datetime.now(timezone.utc)
    from zerg.services.live_archive_outbox import enqueue_session_input_receipt_outbox

    enqueue_session_input_receipt_outbox(
        db,
        receipt_id=str(row.id),
        owner_id=int(row.owner_id),
        session_id=str(row.session_id),
        text=str(row.text or ""),
        intent=str(row.intent or "auto"),
        client_request_id=row.client_request_id,
        delivery_request_id=delivery_request_id,
    )
    db.commit()
    return _snapshot(row)


def mark_live_receipt_failed(
    db: Session,
    *,
    receipt_id: str,
    error: str,
) -> LiveInputReceiptSnapshot | None:
    row = db.query(LiveSessionInputReceipt).filter(LiveSessionInputReceipt.id == str(receipt_id)).first()
    if row is None:
        return None
    row.status = INPUT_STATUS_FAILED
    row.error_json = _json_or_none({"message": str(error or "session input delivery failed")[:500]})
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _snapshot(row)


def upsert_live_input_receipt(
    db: Session,
    *,
    owner_id: int,
    session_id: UUID | str,
    provider: str,
    text: str,
    intent: str,
    status: str,
    client_request_id: str | None,
    device_id: str | None = None,
    thread_id: UUID | str | None = None,
    archive_session_input_id: int | None = None,
    control_command_id: str | None = None,
    delivery_request_id: str | None = None,
    error: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> LiveSessionInputReceipt:
    """Create or refresh a live input receipt by the public idempotency key."""

    session_key = _session_key(session_id)
    client_key = _clean_str(client_request_id)
    row = None
    if client_key is not None:
        row = (
            db.query(LiveSessionInputReceipt)
            .filter(
                LiveSessionInputReceipt.owner_id == int(owner_id),
                LiveSessionInputReceipt.session_id == session_key,
                LiveSessionInputReceipt.client_request_id == client_key,
            )
            .first()
        )
    if row is None:
        row = LiveSessionInputReceipt(id=str(uuid4()))
        db.add(row)

    row.owner_id = int(owner_id)
    row.session_id = session_key
    row.thread_id = _clean_str(thread_id)
    row.provider = _clean_str(provider) or "unknown"
    row.device_id = _clean_str(device_id)
    row.client_request_id = client_key
    row.intent = _clean_str(intent) or "auto"
    row.status = _clean_str(status) or "created"
    row.text = str(text or "")
    if archive_session_input_id is not None:
        row.archive_session_input_id = int(archive_session_input_id)
    if control_command_id is not None:
        row.control_command_id = _clean_str(control_command_id)
    if delivery_request_id is not None:
        row.delivery_request_id = _clean_str(delivery_request_id)
    row.error_json = _json_or_none(error)
    row.expires_at = normalize_utc(expires_at)
    row.updated_at = normalize_utc(now) or datetime.now(timezone.utc)
    return row


async def record_live_input_receipt_best_effort(
    *,
    owner_id: int,
    session_id: UUID | str,
    provider: str,
    text: str,
    intent: str,
    status: str,
    client_request_id: str | None,
    device_id: str | None = None,
    thread_id: UUID | str | None = None,
    archive_session_input_id: int | None = None,
    control_command_id: str | None = None,
    delivery_request_id: str | None = None,
    enqueue_archive_projection: bool = False,
    error: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> str | None:
    """Best-effort additive live receipt write for archive-backed paths."""

    if not database_module.live_store_configured():
        return None
    live_ws = get_live_write_serializer()
    if not live_ws.is_configured:
        return None
    try:
        row_id = await live_ws.execute(
            lambda live_db: _record_live_input_receipt(
                live_db,
                owner_id=owner_id,
                session_id=session_id,
                provider=provider,
                text=text,
                intent=intent,
                status=status,
                client_request_id=client_request_id,
                device_id=device_id,
                thread_id=thread_id,
                archive_session_input_id=archive_session_input_id,
                control_command_id=control_command_id,
                delivery_request_id=delivery_request_id,
                enqueue_archive_projection=enqueue_archive_projection,
                error=error,
                expires_at=expires_at,
            ),
            auto_commit=True,
            label="live-session-input-receipt",
            timeout_seconds=LIVE_INPUT_RECEIPT_WRITE_TIMEOUT_SECS,
        )
        return str(row_id)
    except Exception:
        logger.warning("Failed to record live input receipt for session %s", session_id, exc_info=True)
        return None


def _record_live_input_receipt(
    live_db: Session,
    *,
    owner_id: int,
    session_id: UUID | str,
    provider: str,
    text: str,
    intent: str,
    status: str,
    client_request_id: str | None,
    device_id: str | None,
    thread_id: UUID | str | None,
    archive_session_input_id: int | None,
    control_command_id: str | None,
    delivery_request_id: str | None,
    enqueue_archive_projection: bool,
    error: dict[str, Any] | None,
    expires_at: datetime | None,
) -> str:
    row = upsert_live_input_receipt(
        live_db,
        owner_id=owner_id,
        session_id=session_id,
        provider=provider,
        text=text,
        intent=intent,
        status=status,
        client_request_id=client_request_id,
        device_id=device_id,
        thread_id=thread_id,
        archive_session_input_id=archive_session_input_id,
        control_command_id=control_command_id,
        delivery_request_id=delivery_request_id,
        error=error,
        expires_at=expires_at,
    )
    if enqueue_archive_projection:
        from zerg.services.live_archive_outbox import enqueue_session_input_receipt_outbox

        enqueue_session_input_receipt_outbox(
            live_db,
            receipt_id=str(row.id),
            owner_id=owner_id,
            session_id=session_id,
            text=text,
            intent=intent,
            client_request_id=client_request_id,
            delivery_request_id=delivery_request_id,
        )
    return str(row.id)


async def load_live_input_receipt_by_client_request_best_effort(
    *,
    owner_id: int,
    session_id: UUID | str,
    client_request_id: str | None,
) -> LiveInputReceiptSnapshot | None:
    if not database_module.live_store_configured():
        return None
    session_factory = database_module.get_live_session_factory()
    if session_factory is None:
        return None
    try:
        with session_factory() as live_db:
            return get_live_input_receipt_by_client_request(
                live_db,
                owner_id=owner_id,
                session_id=session_id,
                client_request_id=client_request_id,
            )
    except Exception:
        logger.warning("Failed to load live input receipt for session %s", session_id, exc_info=True)
        return None
