"""Materialized live-session facts for the hot SQLite lane."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.utils.time import normalize_utc


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_timestamp(*values: object) -> datetime | None:
    """Newest aware-UTC value in the set, ignoring anything unparseable."""

    parsed = [normalize_utc(value) for value in values]
    return max((value for value in parsed if value is not None), default=None)


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
    stats: dict[str, int] | None = None,
) -> set[UUID]:
    """Project managed heartbeat leases into the Live Store session index.

    One row lookup and one update per changed lease, plus a single statement for
    the untouched rest. A machine's heartbeat carries its whole lease snapshot,
    so per-lease ``db.get`` and per-lease writes made this pass scale with fleet
    size on every heartbeat.
    """

    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    normalized_device_id = _normalized(device_id)
    normalized_owner_id = _normalized(owner_id) or None

    entries: list[tuple[Any, UUID]] = []
    for lease in leases:
        session_id = _session_uuid(getattr(lease, "session_id", None))
        if session_id is None:
            continue
        entries.append((lease, session_id))
    if not entries:
        return touched

    existing = {
        row.session_id: row
        for row in db.query(LiveSession).filter(LiveSession.session_id.in_({str(session_id) for _, session_id in entries})).all()
    }
    advance_only: set[str] = set()
    changed_count = 0
    for lease, session_id in entries:
        session_key = str(session_id)
        provider = _normalized(getattr(lease, "provider", None)).lower() or "unknown"
        state = _normalized(getattr(lease, "state", None)).lower() or "unknown"
        device_key = normalized_device_id or None
        machine_id = _normalized(getattr(lease, "machine_id", None)) or None
        row = existing.get(session_key)
        if row is None:
            # Added rows are remembered under their key: one heartbeat can name
            # the same session twice (a lease list built from two scans), and a
            # second `db.add` for the same primary key fails at flush.
            row = LiveSession(
                session_id=session_key,
                provider=provider,
                device_id=device_key,
                machine_id=machine_id,
                state=state,
                owner_id=normalized_owner_id,
                last_seen_at=seen_at,
                updated_at=seen_at,
                started_at=normalize_utc(getattr(lease, "observed_at", None)) or seen_at,
            )
            db.add(row)
            existing[session_key] = row
            changed_count += 1
            touched.add(session_id)
            continue
        unchanged = (
            row.provider == provider
            and row.device_id == device_key
            and row.machine_id == machine_id
            and row.state == state
            and (normalized_owner_id is None or row.owner_id == normalized_owner_id)
        )
        if unchanged:
            advance_only.add(str(session_id))
        else:
            changed_count += 1
            if normalized_owner_id is not None:
                row.owner_id = normalized_owner_id
            row.provider = provider
            row.device_id = device_key
            row.machine_id = machine_id
            row.state = state
            row.last_seen_at = seen_at
            row.updated_at = seen_at
        touched.add(session_id)

    if advance_only:
        db.query(LiveSession).filter(LiveSession.session_id.in_(advance_only)).update(
            {"last_seen_at": seen_at, "updated_at": seen_at},
            synchronize_session=False,
        )

    if stats is not None:
        stats["sessions_present"] = len(entries)
        stats["sessions_changed"] = changed_count
    return touched


def touch_live_sessions_from_runtime_events(
    db: Session,
    events: list[Any],
    *,
    received_at: datetime | None = None,
) -> set[UUID]:
    """Project runtime signals into the Live Store session index.

    Runtime events are liveness evidence for unmanaged/Shadow sessions that
    never acquire a managed lease. Terminal signals still touch last_seen_at
    but never mark the session ended — a completed run is not a gone session;
    lifecycle close stays with leases and archive truth.
    """

    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    for event in events:
        session_id = _session_uuid(getattr(event, "session_id", None))
        if session_id is None:
            continue
        occurred_at = normalize_utc(getattr(event, "occurred_at", None)) or seen_at
        provider = _normalized(getattr(event, "provider", None)).lower() or "unknown"
        device_id = _normalized(getattr(event, "device_id", None)) or None
        revived = False
        row = db.get(LiveSession, str(session_id))
        if row is None:
            row = LiveSession(
                session_id=str(session_id),
                provider=provider,
                device_id=device_id,
                state="observed",
                started_at=occurred_at,
            )
            db.add(row)
        elif row.state in ("missing", "ended", "unknown"):
            # Delayed signals cannot overturn a newer lease or omission.
            state_observed_at = normalize_utc(row.updated_at)
            if state_observed_at is not None and occurred_at <= state_observed_at:
                continue
            row.state = "observed"
            revived = True
        if device_id is not None and not row.device_id:
            row.device_id = device_id
        last_seen = normalize_utc(row.last_seen_at)
        advanced = last_seen is None or occurred_at > last_seen
        if advanced:
            row.last_seen_at = occurred_at
        # updated_at is the server-observed liveness stamp (canary freshness,
        # the revival guard above). Only evidence newer than what the row
        # already holds may move it: a batch the machine resent after a lost
        # response carries occurred_at values the row has seen, and must not
        # make the session look freshly alive.
        if advanced or revived:
            row.updated_at = seen_at
        touched.add(session_id)
    return touched


def list_active_live_session_ids(
    db: Session,
    *,
    limit: int,
    days_back: int,
    now: datetime | None = None,
) -> list[UUID]:
    """Return recently observed live session IDs from the hot lane."""

    normalized_now = normalize_utc(now) or _utc_now()
    cutoff = normalized_now - timedelta(days=days_back)
    rows = (
        db.query(LiveSession.session_id)
        .join(LiveSessionCatalog, LiveSessionCatalog.session_id == LiveSession.session_id)
        .filter(LiveSession.state.notin_(("missing", "ended")))
        .filter(LiveSessionCatalog.user_state.notin_(("archived", "snoozed")))
        .filter(LiveSessionCatalog.user_hidden_from_timeline == 0)
        .filter(LiveSession.last_seen_at >= cutoff)
        .order_by(LiveSession.last_seen_at.desc(), LiveSession.updated_at.desc(), LiveSession.session_id.desc())
        .limit(limit)
        .all()
    )
    session_ids: list[UUID] = []
    for (session_id,) in rows:
        parsed = _session_uuid(session_id)
        if parsed is not None:
            session_ids.append(parsed)
    return session_ids


def mark_missing_live_sessions(
    db: Session,
    missing_session_ids: set[UUID],
    *,
    device_id: str,
    received_at: datetime | None = None,
) -> set[UUID]:
    """Mark only omissions accepted by canonical device ownership."""

    normalized_device_id = _normalized(device_id)
    if not normalized_device_id or not missing_session_ids:
        return set()
    seen_at = normalize_utc(received_at) or _utc_now()
    missing_strings = {str(session_id) for session_id in missing_session_ids}
    query = db.query(LiveSession).filter(
        LiveSession.device_id == normalized_device_id,
        LiveSession.session_id.in_(missing_strings),
        LiveSession.state.notin_(("missing", "ended")),
    )

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
