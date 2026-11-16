"""Managed-session control liveness projection.

Control liveness is derived from ``SessionConnection`` rows owned by the
kernel projection and exposed as the overlay shape consumed by session
views.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.provider_contract.managed_provider_contracts import contract_for_provider
from zerg.provider_contract.managed_provider_contracts import control_plane_for_provider
from zerg.provider_contract.managed_provider_contracts import provider_for_control_plane
from zerg.provider_contract.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.services.live_launch_readiness import update_live_launch_readiness_state
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

CONTROL_SOURCE_HEARTBEAT = "machine_heartbeat"
CONTROL_SOURCE_ENGINE_CHANNEL = "machine_control_ws"
CONTROL_SOURCE_RUNNER_CONNECTION = "runner_connection"
DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS = 15 * 60 * 1000
DISABLE_MISSING_MANAGED_LEASE_DETACH_ENV = "LONGHOUSE_DISABLE_MISSING_MANAGED_LEASE_DETACH"
"""How stale a live attachment may get before a heartbeat restamps it.

Unchanged leases skip the attach pass entirely, so this is what keeps an idle
but attached session inside the claim horizon: three times the 60s heartbeat
cadence, and a fifth of the 15 min lease TTL. Matching the cadence instead was
the bug the first version shipped with: at exactly 60s every attachment looked
stale on the very next tick and the pass cost the same as no gate at all.
"""
LIVE_ATTACHMENT_REFRESH_INTERVAL = timedelta(minutes=5)
_CONTROL_READY_BRIDGE_STATUSES = {"ready", "healthy", ""}

_KERNEL_STATE_BY_CONTROL_STATE = {
    "online": "attached",
    "degraded": "degraded",
    "offline": "detached",
}

_CONTROL_STATE_BY_KERNEL_STATE = {
    "attached": "online",
    "degraded": "degraded",
    "detached": "offline",
    "released": "offline",
    "ended": "offline",
}

_PROVIDER_BY_CONTROL_PLANE = {
    control_plane: provider_for_control_plane(control_plane)
    for control_plane in trusted_non_runner_control_planes()
    if provider_for_control_plane(control_plane) is not None
}


def _kernel_connection_state(control_state: str) -> str:
    return _KERNEL_STATE_BY_CONTROL_STATE.get(control_state, "detached")


def _kernel_control_plane_for_provider(provider: str) -> str:
    return control_plane_for_provider(provider)


def _connection_capabilities_for_provider(provider: str, control_plane: str) -> dict[str, int]:
    contract = contract_for_provider(provider)
    if contract is None or control_plane not in contract.control_planes:
        return {}
    return contract.connection_capabilities


def _mirror_connection_state(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    control_state: str,
    external_name: str | None,
    device_id: str | None,
) -> None:
    """Materialize a kernel ``SessionConnection`` reflecting control state.

    Positive attach evidence (online/degraded) materializes a thread + open
    run on demand and upserts an attached/degraded connection. Negative
    evidence (offline) only flips an existing connection — it never
    fabricates a run for a session we have no live record of, since that
    would conflate "we don't know about it" with "it ended."
    """

    from zerg.services.agents.kernel_writes import ensure_open_run_for_session
    from zerg.services.agents.kernel_writes import upsert_connection_for_run

    kernel_state = _kernel_connection_state(control_state)
    control_plane = _kernel_control_plane_for_provider(provider)
    connection_capabilities = _connection_capabilities_for_provider(provider, control_plane)

    if kernel_state in {"detached", "released", "ended"}:
        existing = (
            db.query(SessionConnection)
            .join(SessionRun, SessionConnection.run_id == SessionRun.id)
            .join(SessionThread, SessionRun.thread_id == SessionThread.id)
            .filter(
                SessionThread.session_id == session_id,
                SessionConnection.control_plane == control_plane,
            )
            .order_by(SessionConnection.id.desc())
            .first()
        )
        if existing is None:
            return
        if device_id is not None and existing.device_id not in {device_id, None}:
            return
        if device_id is None and existing.device_id is not None:
            return
        upsert_connection_for_run(
            db,
            run=db.query(SessionRun).filter(SessionRun.id == existing.run_id).one(),
            control_plane=control_plane,
            acquisition_kind=existing.acquisition_kind,
            state=kernel_state,
            external_name=external_name,
            device_id=device_id,
        )
        return

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return

    run = ensure_open_run_for_session(
        db,
        session,
        launch_origin="external_adopted",
        host_id=device_id,
    )
    upsert_connection_for_run(
        db,
        run=run,
        control_plane=control_plane,
        acquisition_kind="adopted_control",
        state=kernel_state,
        external_name=external_name,
        device_id=device_id,
        **connection_capabilities,
    )


@dataclass(frozen=True)
class ManagedControlOverlay:
    session_id: UUID
    provider: str
    device_id: str | None
    machine_id: str | None
    transport: str | None
    lease_state: str
    control_state: str
    reason: str | None
    source: str
    sequence: int | None
    last_control_seen_at: datetime | None
    lease_observed_at: datetime | None
    lease_ttl_ms: int | None
    control_expires_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized(value: Any) -> str:
    return str(value or "").strip()


def _lease_control_state(
    *,
    lease_state: str,
    bridge_status: str | None,
    thread_subscription_status: str | None,
) -> tuple[str, str | None]:
    state = _normalized(lease_state).lower()
    bridge = _normalized(bridge_status).lower()
    thread = _normalized(thread_subscription_status).lower()
    if state == "attached":
        if bridge not in _CONTROL_READY_BRIDGE_STATUSES:
            return "degraded", "bridge_unavailable"
        if thread == "provider_thread_switched":
            return "offline", "provider_thread_switched"
        if thread == "failed":
            return "degraded", "thread_subscription_failed"
        return "online", None
    if state == "degraded":
        if thread == "provider_thread_switched":
            return "offline", "provider_thread_switched"
        return "degraded", "degraded"
    if state == "detached":
        return "offline", "detached"
    if state == "missing":
        return "offline", "missing_from_snapshot"
    return "unknown", "unknown_lease_state"


def _provider_for_connection(conn: SessionConnection) -> str:
    plane = _normalized(conn.control_plane)
    return _PROVIDER_BY_CONTROL_PLANE.get(plane, "unknown")


def _control_state_for_connection(conn: SessionConnection) -> str:
    state = _normalized(conn.state).lower()
    return _CONTROL_STATE_BY_KERNEL_STATE.get(state, "unknown")


def _lease_state_for_connection(conn: SessionConnection) -> str:
    state = _normalized(conn.state).lower() or "unknown"
    return state


def _overlay_from_connection(
    *,
    session_id: UUID,
    conn: SessionConnection,
) -> ManagedControlOverlay:
    last_seen = normalize_utc(conn.last_health_at) or normalize_utc(conn.acquired_at)
    lease_ttl_ms = DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS
    expires_at: datetime | None = None
    if last_seen is not None:
        expires_at = last_seen + timedelta(milliseconds=lease_ttl_ms)
    return ManagedControlOverlay(
        session_id=session_id,
        provider=_provider_for_connection(conn),
        device_id=_normalized(conn.device_id) or None,
        machine_id=_normalized(conn.external_name) or None,
        transport=None,
        lease_state=_lease_state_for_connection(conn),
        control_state=_control_state_for_connection(conn),
        reason=None,
        source=CONTROL_SOURCE_HEARTBEAT,
        sequence=None,
        last_control_seen_at=last_seen,
        lease_observed_at=last_seen,
        lease_ttl_ms=lease_ttl_ms,
        control_expires_at=expires_at,
    )


def _payload_for_live_lease(lease: Any, *, control_state: str, reason: str | None, ttl_ms: int) -> dict[str, Any]:
    observed_at = normalize_utc(getattr(lease, "observed_at", None))
    return {
        "bridge_status": _normalized(getattr(lease, "bridge_status", None)) or None,
        "control_state": control_state,
        "lease_ttl_ms": ttl_ms,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "reason": reason,
        "thread_subscription_status": _normalized(getattr(lease, "thread_subscription_status", None)) or None,
    }


def _live_lease_payload(row: LiveControlLease) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _overlay_from_live_lease(row: LiveControlLease) -> ManagedControlOverlay | None:
    try:
        session_id = UUID(str(row.session_id))
    except (TypeError, ValueError):
        return None
    payload = _live_lease_payload(row)
    heartbeat_at = normalize_utc(row.heartbeat_at) or _utc_now()
    ttl_ms = int(payload.get("lease_ttl_ms") or DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
    control_state, default_reason = _lease_control_state(
        lease_state=_normalized(row.state).lower() or "unknown",
        bridge_status=_normalized(payload.get("bridge_status")) or None,
        thread_subscription_status=_normalized(payload.get("thread_subscription_status")) or None,
    )
    return ManagedControlOverlay(
        session_id=session_id,
        provider=_normalized(row.provider).lower() or "unknown",
        device_id=_normalized(row.device_id) or None,
        machine_id=_normalized(row.machine_id) or None,
        transport=None,
        lease_state=_normalized(row.state).lower() or "unknown",
        control_state=_normalized(payload.get("control_state")).lower() or control_state,
        reason=_normalized(payload.get("reason")) or default_reason,
        source=CONTROL_SOURCE_HEARTBEAT,
        sequence=row.sequence,
        last_control_seen_at=heartbeat_at,
        lease_observed_at=heartbeat_at,
        lease_ttl_ms=ttl_ms,
        control_expires_at=heartbeat_at + timedelta(milliseconds=ttl_ms),
    )


def live_attachment_state_map(db: Session, session_ids: list[str]) -> dict[str, datetime | None]:
    """Which sessions have an open run, and when their attachment was last stamped.

    A key means "this session has an open run", so an attach there can still
    change something; the value is that run's newest connection health, or
    ``None`` when no connection row exists yet. A session with no open run is
    *absent*, and that is the important half: attaching it is a documented no-op
    (`attach_live_catalog_control` returns `None` when there is no current run to
    attach to), so the work must be skipped, not refreshed.

    Conflating absence with staleness cost the gate its whole point on real data:
    a machine's snapshot carries every session it can still see, most with runs
    long ended, so "not in the map" meant "refresh" for 1,613 of 1,623 leases on
    every heartbeat.
    """

    if not session_ids:
        return {}
    rows = (
        db.query(
            LiveSessionThread.session_id,
            LiveSessionRun.id,
            func.max(LiveSessionConnection.last_health_at),
        )
        .join(LiveSessionRun, LiveSessionRun.thread_id == LiveSessionThread.id)
        .outerjoin(
            LiveSessionConnection,
            and_(
                LiveSessionConnection.run_id == LiveSessionRun.id,
                LiveSessionConnection.released_at.is_(None),
            ),
        )
        .filter(
            LiveSessionThread.session_id.in_(session_ids),
            LiveSessionRun.ended_at.is_(None),
        )
        .group_by(LiveSessionThread.session_id, LiveSessionRun.id)
        .all()
    )
    state: dict[str, datetime | None] = {}
    for session_id, _run_id, last_health in rows:
        key = str(session_id)
        normalized = normalize_utc(last_health)
        if key not in state:
            state[key] = normalized
            continue
        current = state[key]
        if normalized is not None and (current is None or normalized > current):
            state[key] = normalized
    return state


def live_run_owner_map(db: Session, session_ids: list[str]) -> dict[str, str]:
    """Device that owns each session's current run, checked before any attach.

    Ownership used to be discovered by letting `attach_live_catalog_control` raise
    `PermissionError` and reading that as "this device may not touch the session".
    The authority behind that error is the run's `host_id`, so skipping the attach
    for unchanged leases means reading it here instead. One join over the same
    candidate sessions the freshness map already reads.
    """

    if not session_ids:
        return {}
    rows = (
        db.query(LiveSessionThread.session_id, LiveSessionRun.host_id)
        .join(LiveSessionRun, LiveSessionRun.thread_id == LiveSessionThread.id)
        .filter(
            LiveSessionThread.session_id.in_(set(session_ids)),
            LiveSessionRun.ended_at.is_(None),
        )
        .all()
    )
    owners: dict[str, str] = {}
    for session_id, host_id in rows:
        normalized = _normalized(host_id)
        if normalized:
            owners[str(session_id)] = normalized
    return owners


def _lease_evidence_signature(
    *,
    state: str,
    machine_id: str | None,
    payload: dict[str, Any],
) -> tuple:
    """The parts of a lease that only change when the machine's evidence does.

    ``observed_at``, ``heartbeat_at`` and ``sequence`` describe when the machine
    looked, not what it saw, so none of them is change evidence. ``sequence`` in
    particular is the heartbeat clock itself: the engine stamps every lease in a
    snapshot with the same `now.timestamp_millis()`, so comparing it made every
    lease look changed on every tick and the gate below saved nothing. Production
    showed it immediately (`leases_changed` equal to `leases_present` on every
    slow heartbeat). A retained row keeps its last sequence, which only orders
    overlays whose `heartbeat_at` already decided freshness.
    """

    return (
        state,
        machine_id,
        payload.get("control_state"),
        payload.get("reason"),
        payload.get("bridge_status"),
        payload.get("thread_subscription_status"),
        payload.get("lease_ttl_ms"),
    )


def upsert_live_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
    attachment_state: dict[str, datetime | None] | None = None,
    session_owners: dict[str, str] | None = None,
    stats: dict[str, int] | None = None,
) -> set[UUID]:
    """Retain device telemetry and return sessions accepted for owned projections.

    Work here is bounded by what changed, not by how many leases the machine
    holds. Every heartbeat carries the machine's whole lease snapshot (absence
    detection needs it), so writing and re-attaching every row made one write
    proportional to fleet size: 1,621 leases on a builder Mac and 6,862 on the
    factory machine, which held the single catalog writer ~17s per heartbeat.

    An unchanged lease now costs one row in a bulk ``heartbeat_at`` update. Its
    attachment is refreshed only when the caller says the session has an open run
    whose attachment is missing or stale (``attachment_state``), because the
    attachment is what keeps a current claim inside the lease TTL and must not
    simply stop advancing.
    """

    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    normalized_device_id = _normalized(device_id) or device_id
    refresh_floor = seen_at - LIVE_ATTACHMENT_REFRESH_INTERVAL

    normalized_leases: list[tuple[Any, str, str]] = []
    for lease in leases:
        session_id = getattr(lease, "session_id", None)
        if session_id is None:
            continue
        provider = _normalized(getattr(lease, "provider", None)).lower() or "unknown"
        normalized_leases.append((lease, str(session_id), provider))
    if not normalized_leases:
        return touched

    session_keys = {session_id for _, session_id, _ in normalized_leases}
    existing_rows = (
        db.query(LiveControlLease)
        .filter(
            LiveControlLease.device_id == normalized_device_id,
            LiveControlLease.session_id.in_(session_keys),
        )
        .all()
    )
    existing = {(row.session_id, row.provider): row for row in existing_rows}

    from zerg.services.live_catalog_launch import attach_live_catalog_control

    unchanged_session_keys: set[str] = set()
    foreign_session_keys: set[str] = set()
    changed_count = 0
    refreshed_count = 0
    foreign_count = 0
    attached_count = 0
    for lease, session_id, provider in normalized_leases:
        lease_state = _normalized(getattr(lease, "state", None)).lower() or "unknown"
        bridge_status = _normalized(getattr(lease, "bridge_status", None)) or None
        thread_subscription_status = _normalized(getattr(lease, "thread_subscription_status", None)) or None
        control_state, reason = _lease_control_state(
            lease_state=lease_state,
            bridge_status=bridge_status,
            thread_subscription_status=thread_subscription_status,
        )
        ttl_ms = int(getattr(lease, "lease_ttl_ms", None) or DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
        machine_id = _normalized(getattr(lease, "machine_id", None)) or None
        sequence = getattr(lease, "sequence", None)
        payload = _payload_for_live_lease(lease, control_state=control_state, reason=reason, ttl_ms=ttl_ms)
        signature = _lease_evidence_signature(
            state=lease_state,
            machine_id=machine_id,
            payload=payload,
        )

        row = existing.get((session_id, provider))
        created = row is None
        if created:
            row = LiveControlLease(
                session_id=session_id,
                provider=provider,
                device_id=normalized_device_id,
            )
            db.add(row)
        changed = (
            created
            or _lease_evidence_signature(
                state=_normalized(row.state).lower() or "unknown",
                machine_id=row.machine_id,
                payload=_live_lease_payload(row),
            )
            != signature
        )
        if session_owners is not None:
            owner = session_owners.get(session_id)
            if owner is not None and owner != normalized_device_id:
                # Retain the device's raw lease, but an index owned by another
                # device is not this heartbeat's to restamp, and the session is
                # not this device's to project. Counted on its own: these leases
                # neither took the changed path nor were cheap retentions.
                if changed:
                    row.machine_id = machine_id
                    row.state = lease_state
                    row.sequence = sequence
                    row.heartbeat_at = seen_at
                    row.updated_at = seen_at
                    row.payload_json = json.dumps(payload, sort_keys=True)
                else:
                    foreign_session_keys.add(session_id)
                foreign_count += 1
                continue
        # Absent from the map means no open run, where an attach is a no-op.
        # Present with missing or stale health means there is work to do.
        refresh_attachment = (
            changed
            or attachment_state is None
            or (session_id in attachment_state and (attachment_state[session_id] is None or attachment_state[session_id] < refresh_floor))
        )
        if not refresh_attachment:
            unchanged_session_keys.add(session_id)
            touched.add(UUID(session_id))
            continue

        if changed:
            changed_count += 1
        else:
            refreshed_count += 1
        row.machine_id = machine_id
        row.state = lease_state
        row.sequence = sequence
        row.heartbeat_at = seen_at
        row.updated_at = seen_at
        row.payload_json = json.dumps(payload, sort_keys=True)
        try:
            connection = attach_live_catalog_control(
                db,
                session_id=UUID(session_id),
                provider=provider,
                device_id=normalized_device_id,
                state={"online": "attached", "degraded": "degraded"}.get(control_state, "detached"),
                external_name=row.machine_id,
                observed_at=seen_at,
            )
            attached_count += 1
            if connection is not None and control_state in {"online", "degraded"}:
                readiness = db.get(LiveLaunchReadiness, session_id)
                if readiness is not None and readiness.state in {"pending", "dispatched"}:
                    update_live_launch_readiness_state(
                        db,
                        session_id=UUID(session_id),
                        state="adopted",
                        clear_expires=True,
                        now=seen_at,
                    )
        except PermissionError:
            # Retain the device's raw lease, but do not let it rewrite the
            # current owner's singleton session index or readiness.
            continue
        except RuntimeError:
            # Heartbeats may precede catalog ingest for a newly discovered
            # Shadow session. The next catalog sync/heartbeat converges it.
            logger.debug("Live control lease arrived before catalog session %s", session_id)
        touched.add(UUID(session_id))

    retained_session_keys = unchanged_session_keys | foreign_session_keys
    if retained_session_keys:
        # One statement for the whole retained snapshot: liveness still has to
        # advance for every lease the machine reported, and that is the only
        # thing an unchanged lease is saying. Keyed by session within this one
        # device, which is what the (device_id, session_id) pair means here: a
        # second provider for the same session on the same machine shares the
        # bump, and that row is still liveness for this heartbeat.
        db.query(LiveControlLease).filter(
            LiveControlLease.device_id == normalized_device_id,
            LiveControlLease.session_id.in_(retained_session_keys),
        ).update(
            {"heartbeat_at": seen_at, "updated_at": seen_at},
            synchronize_session=False,
        )

    if stats is not None:
        stats["leases_present"] = len(normalized_leases)
        stats["leases_changed"] = changed_count
        stats["leases_refreshed"] = refreshed_count
        stats["leases_attached"] = attached_count
        stats["leases_foreign"] = foreign_count
        stats["leases_retained"] = len(unchanged_session_keys)
    return touched


def mark_missing_live_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Apply this device's accepted absence, including previously missing leases."""

    if os.environ.get(DISABLE_MISSING_MANAGED_LEASE_DETACH_ENV) in {"1", "true", "TRUE", "yes", "on"}:
        return set()

    seen_session_ids = {str(getattr(lease, "session_id", "")) for lease in leases if getattr(lease, "session_id", None) is not None}
    normalized_device_id = _normalized(device_id)
    if not normalized_device_id:
        return set()
    seen_at = normalize_utc(received_at) or _utc_now()
    # A lease already marked missing needs absence re-applied only when its
    # session was re-observed since. Re-applying it to every missing lease made
    # each heartbeat rewrite every lease the device ever held: 6,851 for the
    # factory machine, holding the catalog writer ~17s per heartbeat.
    reobserved_session_ids = select(LiveSession.session_id).where(
        LiveSession.device_id == normalized_device_id,
        LiveSession.state.notin_(("missing", "ended")),
    )
    query = db.query(LiveControlLease).filter(
        LiveControlLease.device_id == normalized_device_id,
        or_(
            LiveControlLease.state.in_(("attached", "degraded")),
            and_(
                LiveControlLease.state == "missing",
                LiveControlLease.session_id.in_(reobserved_session_ids),
            ),
        ),
    )
    if seen_session_ids:
        query = query.filter(LiveControlLease.session_id.notin_(seen_session_ids))

    touched: set[UUID] = set()
    for row in query.all():
        last_seen = normalize_utc(row.heartbeat_at)
        if last_seen is not None and last_seen >= seen_at:
            continue
        row.state = "missing"
        row.heartbeat_at = seen_at
        payload = _live_lease_payload(row)
        payload["control_state"] = "offline"
        payload["reason"] = "missing_from_snapshot"
        row.payload_json = json.dumps(payload, sort_keys=True)
        from zerg.services.live_catalog_launch import attach_live_catalog_control

        try:
            attach_live_catalog_control(
                db,
                session_id=row.session_id,
                provider=str(row.provider),
                device_id=normalized_device_id,
                state="detached",
                external_name=row.machine_id,
                observed_at=seen_at,
            )
        except PermissionError:
            continue
        except RuntimeError:
            logger.debug("Missing live control lease has no catalog session %s", row.session_id)
        try:
            touched.add(UUID(str(row.session_id)))
        except (TypeError, ValueError):
            continue
    return touched


def upsert_managed_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Materialize managed lease snapshots into kernel ``SessionConnection`` rows."""

    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    normalized_device_id = _normalized(device_id) or device_id
    for lease in leases:
        session_id = getattr(lease, "session_id", None)
        if session_id is None:
            continue
        provider = _normalized(getattr(lease, "provider", None)).lower() or "unknown"
        lease_state = _normalized(getattr(lease, "state", None)).lower() or "unknown"
        bridge_status = _normalized(getattr(lease, "bridge_status", None)) or None
        thread_subscription_status = _normalized(getattr(lease, "thread_subscription_status", None)) or None
        control_state, _reason = _lease_control_state(
            lease_state=lease_state,
            bridge_status=bridge_status,
            thread_subscription_status=thread_subscription_status,
        )
        machine_id = _normalized(getattr(lease, "machine_id", None)) or None
        _mirror_connection_state(
            db,
            session_id=session_id,
            provider=provider,
            control_state=control_state,
            external_name=machine_id,
            device_id=normalized_device_id,
        )
        touched.add(session_id)
    # Update last_health_at on the affected connections so freshness reflects
    # this snapshot. ``_mirror_connection_state`` only writes state; bump
    # health timestamps here so overlay projection picks up "just seen".
    if touched:
        rows = (
            db.query(SessionConnection)
            .join(SessionRun, SessionConnection.run_id == SessionRun.id)
            .join(SessionThread, SessionRun.thread_id == SessionThread.id)
            .filter(
                SessionThread.session_id.in_(touched),
                SessionConnection.device_id == normalized_device_id,
            )
            .all()
        )
        for row in rows:
            row.last_health_at = seen_at
    return touched


def mark_missing_managed_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Mark connections from this device offline when omitted from the snapshot."""

    if os.environ.get(DISABLE_MISSING_MANAGED_LEASE_DETACH_ENV) in {"1", "true", "TRUE", "yes", "on"}:
        return set()

    seen_session_ids = {getattr(lease, "session_id", None) for lease in leases}
    seen_session_ids.discard(None)
    normalized_device_id = _normalized(device_id)
    if not normalized_device_id:
        return set()
    seen_at = normalize_utc(received_at) or _utc_now()
    touched: set[UUID] = set()

    # Find this device's connections in attached/degraded state whose health
    # timestamps predate this snapshot. Unknown-owner rows are intentionally
    # sticky: they require positive heartbeat evidence to claim/update them and
    # are never detached just because a device omitted them.
    query = (
        db.query(SessionConnection, SessionThread.session_id)
        .join(SessionRun, SessionConnection.run_id == SessionRun.id)
        .join(SessionThread, SessionRun.thread_id == SessionThread.id)
        .filter(
            SessionConnection.device_id == normalized_device_id,
            SessionConnection.state.in_(("attached", "degraded")),
        )
    )
    if seen_session_ids:
        query = query.filter(SessionThread.session_id.notin_(seen_session_ids))
    for conn, session_id in query.all():
        last_health = normalize_utc(conn.last_health_at)
        if last_health is not None and last_health >= seen_at:
            continue
        conn.state = "detached"
        conn.last_health_at = seen_at
        touched.add(session_id)
    return touched


def load_managed_control_state_map(
    db: Session,
    session_ids: list[UUID],
) -> dict[UUID, ManagedControlOverlay]:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionThread.session_id, SessionConnection)
        .join(SessionRun, SessionRun.thread_id == SessionThread.id)
        .join(SessionConnection, SessionConnection.run_id == SessionRun.id)
        .filter(
            SessionThread.session_id.in_(session_ids),
            SessionConnection.control_plane.in_(tuple(_PROVIDER_BY_CONTROL_PLANE.keys())),
        )
        .all()
    )
    best: dict[UUID, SessionConnection] = {}
    for session_id, conn in rows:
        existing = best.get(session_id)
        if existing is None:
            best[session_id] = conn
            continue
        # Prefer attached > degraded > others, then most recent health.
        prefer = _connection_priority(conn)
        current = _connection_priority(existing)
        if prefer > current:
            best[session_id] = conn
    overlays = {sid: _overlay_from_connection(session_id=sid, conn=conn) for sid, conn in best.items()}
    for session_id, overlay in _load_live_managed_control_state_map(session_ids).items():
        existing = overlays.get(session_id)
        if existing is None or _overlay_priority(overlay) >= _overlay_priority(existing):
            overlays[session_id] = overlay
    return overlays


def _load_live_managed_control_state_map(session_ids: list[UUID]) -> dict[UUID, ManagedControlOverlay]:
    if not session_ids or not database_module.live_store_configured():
        return {}
    session_id_strings = [str(session_id) for session_id in session_ids]
    from zerg.services.catalog_facts import hydrate_catalog_row
    from zerg.services.catalog_facts import session_facts_map

    facts_by_session = session_facts_map(session_id_strings)
    rows = [
        row
        for facts in facts_by_session.values()
        for payload in facts.get("control_leases") or []
        if (row := hydrate_catalog_row(LiveControlLease, payload)) is not None
    ]
    best: dict[UUID, ManagedControlOverlay] = {}
    for row in rows:
        overlay = _overlay_from_live_lease(row)
        if overlay is None:
            continue
        existing = best.get(overlay.session_id)
        if existing is None or _overlay_priority(overlay) > _overlay_priority(existing):
            best[overlay.session_id] = overlay
    return best


def _overlay_priority(overlay: ManagedControlOverlay) -> tuple:
    control_state = _normalized(overlay.control_state).lower()
    state_rank = {"online": 5, "degraded": 4, "offline": 3, "unknown": 0}.get(control_state, 0)
    last_seen = normalize_utc(overlay.last_control_seen_at) or datetime.min.replace(tzinfo=timezone.utc)
    return (last_seen, state_rank, overlay.sequence or 0, overlay.source)


def _connection_priority(conn: SessionConnection) -> tuple:
    state = _normalized(conn.state).lower()
    state_rank = {"attached": 5, "degraded": 4, "detached": 3, "released": 2, "ended": 1}.get(state, 0)
    last_health = normalize_utc(conn.last_health_at) or datetime.min.replace(tzinfo=timezone.utc)
    return (state_rank, last_health, conn.id)
