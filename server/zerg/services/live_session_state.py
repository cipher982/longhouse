"""Materialized live-session facts for the hot SQLite lane."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.live_store import LiveSession
from zerg.utils.time import normalize_utc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _session_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def upsert_live_sessions_from_managed_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    owner_id: int | str | None = None,
    received_at: datetime | None = None,
) -> set[UUID]:
    """Project managed heartbeat leases into the Live Store session index."""

    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    normalized_device_id = _normalized(device_id)
    normalized_owner_id = _normalized(owner_id) or None
    for lease in leases:
        session_id = _session_uuid(getattr(lease, "session_id", None))
        if session_id is None:
            continue
        provider = _normalized(getattr(lease, "provider", None)).lower() or "unknown"
        state = _normalized(getattr(lease, "state", None)).lower() or "unknown"
        row = db.get(LiveSession, str(session_id))
        if row is None:
            row = LiveSession(
                session_id=str(session_id),
                provider=provider,
                device_id=normalized_device_id or None,
                started_at=normalize_utc(getattr(lease, "observed_at", None)) or seen_at,
            )
            db.add(row)
        if normalized_owner_id is not None:
            row.owner_id = normalized_owner_id
        row.provider = provider
        row.device_id = normalized_device_id or None
        row.machine_id = _normalized(getattr(lease, "machine_id", None)) or None
        row.state = state
        row.last_seen_at = seen_at
        row.updated_at = seen_at
        touched.add(session_id)
    return touched


def mark_missing_live_sessions(
    db: Session,
    seen_session_ids: set[UUID],
    *,
    device_id: str,
    received_at: datetime | None = None,
) -> set[UUID]:
    """Mark previously live sessions from this device missing when omitted."""

    normalized_device_id = _normalized(device_id)
    if not normalized_device_id:
        return set()
    seen_at = normalize_utc(received_at) or _utc_now()
    seen_strings = {str(session_id) for session_id in seen_session_ids}
    query = db.query(LiveSession).filter(
        LiveSession.device_id == normalized_device_id,
        LiveSession.state.notin_(("missing", "ended")),
    )
    if seen_strings:
        query = query.filter(LiveSession.session_id.notin_(seen_strings))

    touched: set[UUID] = set()
    for row in query.all():
        last_seen = normalize_utc(row.last_seen_at)
        if last_seen is not None and last_seen >= seen_at:
            continue
        row.state = "missing"
        row.updated_at = seen_at
        session_id = _session_uuid(row.session_id)
        if session_id is not None:
            touched.add(session_id)
    return touched
