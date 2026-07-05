"""Hot-lane receipts for user text input control work."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.services.write_serializer import get_live_write_serializer
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

LIVE_INPUT_RECEIPT_WRITE_TIMEOUT_SECS = 0.25


def _session_key(session_id: UUID | str) -> str:
    return str(session_id)


def _clean_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True)


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
            lambda live_db: upsert_live_input_receipt(
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
                error=error,
                expires_at=expires_at,
            ).id,
            auto_commit=True,
            label="live-session-input-receipt",
            timeout_seconds=LIVE_INPUT_RECEIPT_WRITE_TIMEOUT_SECS,
        )
        return str(row_id)
    except Exception:
        logger.warning("Failed to record live input receipt for session %s", session_id, exc_info=True)
        return None
