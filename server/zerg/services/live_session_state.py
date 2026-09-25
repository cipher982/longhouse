"""Materialized live-session facts for the hot SQLite lane."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
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
        if device_id is not None and not row.device_id:
            row.device_id = device_id
        last_seen = normalize_utc(row.last_seen_at)
        if last_seen is None or occurred_at > last_seen:
            row.last_seen_at = occurred_at
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


# A wrapper killed before it could ship its terminal fact leaves a run row with
# `ended_at IS NULL`, and the session it belonged to eventually stops being
# enumerated by the machine. That omission is control-path evidence, not process
# exit: it says the Machine Agent stopped reporting the session, which the
# deliberate contract in tests_lite/test_heartbeat_endpoint.py forbids treating
# as a terminal state. So this path waits for the omission to be old, requires
# the same no-live-evidence predicate resume uses, and labels what it ends as
# `unobserved_retired` rather than `process_gone`.
#
# 24h is the age at which a session the machine has not enumerated for a day,
# with no attachment, runtime signal or launch attempt since, is not work in
# progress by any reading of the evidence we hold.
ABSENT_RUN_RETIRE_AGE = timedelta(hours=24)
# The heartbeat runs inside catalogd's single writer, so convergence is bounded
# per beat and drains over successive ones instead of holding the writer for a
# backlog of thousands.
ABSENT_RUN_RETIRE_BATCH = 256


def retire_stale_absent_runs(
    db: Session,
    *,
    device_id: str,
    received_at: datetime | None = None,
) -> set[UUID]:
    """End long-absent managed runs the machine has certified it cannot see.

    Only the half of the story the machine cannot supply: a run whose provider
    exited unobserved *and* whose observation record is gone, so no per-process
    exit fact can ever be produced for it. Resume already retires such a run
    when someone tries to use the session; this converges the ones nobody has
    tried yet, which is what keeps `ended_at IS NULL` meaning "executing".

    Called from the certified-snapshot branch of the heartbeat, so the owning
    device has just declared a complete enumeration that excludes the session --
    the same statement `mark_missing_live_sessions` already trusts for the
    session and lease rows.

    Excluded by construction:

    - Console runs: the Runtime Host dispatches those with no local process, so
      a machine's enumeration says nothing about their execution.
    - Runs younger than `ABSENT_RUN_RETIRE_AGE`.
    - Runs that still show live evidence: a lease in an attached/degraded state
      inside the control lease, a runtime state still being signalled or
      asserted, or an unexpired pending launch attempt. The attachment's own
      stamp is deliberately not read: accepting absence detaches it as a
      consequence, so a fresh stamp there marks the session gone, not alive.
    """

    from zerg.services.managed_control_state import DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS

    normalized_device_id = _normalized(device_id)
    if not normalized_device_id:
        return set()
    seen_at = normalize_utc(received_at) or _utc_now()
    lease_floor = seen_at - timedelta(milliseconds=DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
    absent_before = seen_at - ABSENT_RUN_RETIRE_AGE

    candidates = (
        db.query(LiveSessionRun, LiveSessionCatalog)
        .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
        .join(LiveSession, LiveSession.session_id == LiveSessionThread.session_id)
        .join(LiveSessionCatalog, LiveSessionCatalog.session_id == LiveSessionThread.session_id)
        .filter(
            LiveSessionRun.ended_at.is_(None),
            LiveSessionRun.started_at <= absent_before,
            LiveSession.device_id == normalized_device_id,
            LiveSession.state == "missing",
            LiveSession.updated_at <= absent_before,
            LiveSessionThread.is_primary == 1,
            LiveSessionThread.branch_kind == "root",
            or_(LiveSessionCatalog.origin_kind.is_(None), LiveSessionCatalog.origin_kind != "console"),
        )
        .order_by(LiveSessionRun.started_at.asc(), LiveSessionRun.id.asc())
        .all()
    )
    if not candidates:
        return set()

    session_ids = {str(catalog_session.session_id) for _, catalog_session in candidates}
    run_ids = [str(run.id) for run, _ in candidates]
    live_lease_session_ids = {
        str(row[0])
        for row in db.query(LiveControlLease.session_id)
        .filter(
            LiveControlLease.session_id.in_(session_ids),
            LiveControlLease.device_id == normalized_device_id,
            LiveControlLease.state.in_(("attached", "degraded")),
            LiveControlLease.heartbeat_at.is_not(None),
            LiveControlLease.heartbeat_at > lease_floor,
        )
        .all()
    }
    fresh_state_run_ids = {
        str(row[0])
        for row in db.query(LiveRuntimeState.run_id)
        .filter(
            LiveRuntimeState.run_id.in_(run_ids),
            LiveRuntimeState.terminal_state.is_(None),
            or_(
                LiveRuntimeState.freshness_expires_at > seen_at,
                LiveRuntimeState.last_runtime_signal_at > lease_floor,
                LiveRuntimeState.last_asserted_at > lease_floor,
                LiveRuntimeState.updated_at > lease_floor,
            ),
        )
        .all()
    }
    pending_attempt_run_ids = {
        str(row[0])
        for row in db.query(LiveSessionLaunchAttempt.run_id)
        .filter(
            LiveSessionLaunchAttempt.run_id.in_(run_ids),
            LiveSessionLaunchAttempt.state == "pending",
            or_(
                LiveSessionLaunchAttempt.expires_at.is_(None),
                LiveSessionLaunchAttempt.expires_at > seen_at,
            ),
        )
        .all()
    }

    touched: set[UUID] = set()
    retired = 0
    for run, catalog_session in candidates:
        run_id = str(run.id)
        if run_id in fresh_state_run_ids or run_id in pending_attempt_run_ids:
            continue
        if str(catalog_session.session_id) in live_lease_session_ids:
            continue
        run.ended_at = seen_at
        run.exit_status = "unobserved_retired"
        for connection in (
            db.query(LiveSessionConnection)
            .filter(
                LiveSessionConnection.run_id == run_id,
                LiveSessionConnection.released_at.is_(None),
            )
            .all()
        ):
            connection.state = "ended"
            connection.released_at = seen_at
            connection.last_health_at = seen_at
            connection.can_send_input = 0
            connection.can_interrupt = 0
            connection.can_terminate = 0
            connection.can_tail_output = 0
            connection.can_resume = 0
        session_id = _session_uuid(catalog_session.session_id)
        if session_id is not None:
            touched.add(session_id)
        retired += 1
        if retired >= ABSENT_RUN_RETIRE_BATCH:
            break
    return touched
