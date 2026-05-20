"""Managed-session control liveness projection.

This module owns the explicit control lane. It deliberately does not describe
provider phase or transcript activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import ManagedSessionControlState
from zerg.utils.time import normalize_utc

CONTROL_SOURCE_HEARTBEAT = "machine_heartbeat"
CONTROL_SOURCE_ENGINE_CHANNEL = "machine_control_ws"
CONTROL_SOURCE_LEGACY_RUNNER = "legacy_runner"
DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS = 15 * 60 * 1000
_CONTROL_READY_BRIDGE_STATUSES = {"ready", "healthy", ""}


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
        if thread == "failed":
            return "degraded", "thread_subscription_failed"
        return "online", None
    if state == "degraded":
        return "degraded", "degraded"
    if state == "detached":
        return "offline", "detached"
    if state == "missing":
        return "offline", "missing_from_snapshot"
    return "unknown", "unknown_lease_state"


def _session_transport(db: Session, session_id: UUID) -> str | None:
    row = db.query(AgentSession.managed_transport).filter(AgentSession.id == session_id).first()
    if row is None:
        return None
    return _normalized(row[0]) or None


def _overlay_from_row(row: ManagedSessionControlState) -> ManagedControlOverlay:
    return ManagedControlOverlay(
        session_id=row.session_id,
        provider=_normalized(row.provider).lower() or "unknown",
        device_id=_normalized(row.device_id) or None,
        machine_id=_normalized(row.machine_id) or None,
        transport=_normalized(row.transport) or None,
        lease_state=_normalized(row.lease_state).lower() or "unknown",
        control_state=_normalized(row.control_state).lower() or "unknown",
        reason=_normalized(row.reason) or None,
        source=_normalized(row.source) or CONTROL_SOURCE_HEARTBEAT,
        sequence=int(row.sequence) if row.sequence is not None else None,
        last_control_seen_at=normalize_utc(row.last_control_seen_at),
        lease_observed_at=normalize_utc(row.lease_observed_at),
        lease_ttl_ms=int(row.lease_ttl_ms) if row.lease_ttl_ms is not None else None,
        control_expires_at=normalize_utc(row.control_expires_at),
    )


def live_transport_control_overlay(
    session: AgentSession,
    *,
    source: str,
    seen_at: datetime,
    device_id: str | None = None,
    machine_id: str | None = None,
    ttl_ms: int = DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS,
) -> ManagedControlOverlay:
    """Represent an already-verified live control transport as control facts."""

    normalized_seen_at = normalize_utc(seen_at) or _utc_now()
    normalized_ttl_ms = int(ttl_ms or DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS)
    return ManagedControlOverlay(
        session_id=session.id,
        provider=_normalized(getattr(session, "provider", None)).lower() or "unknown",
        device_id=_normalized(device_id or getattr(session, "device_id", None)) or None,
        machine_id=_normalized(machine_id or getattr(session, "source_runner_name", None)) or None,
        transport=_normalized(getattr(session, "managed_transport", None)) or None,
        lease_state="attached",
        control_state="online",
        reason=None,
        source=source,
        sequence=None,
        last_control_seen_at=normalized_seen_at,
        lease_observed_at=normalized_seen_at,
        lease_ttl_ms=normalized_ttl_ms,
        control_expires_at=normalized_seen_at + timedelta(milliseconds=normalized_ttl_ms),
    )


def upsert_managed_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Materialize managed lease snapshots into explicit control facts."""
    touched: set[UUID] = set()
    seen_at = normalize_utc(received_at) or _utc_now()
    for lease in leases:
        session_id = getattr(lease, "session_id", None)
        if session_id is None:
            continue
        provider = _normalized(getattr(lease, "provider", None)).lower() or "unknown"
        lease_state = _normalized(getattr(lease, "state", None)).lower() or "unknown"
        bridge_status = _normalized(getattr(lease, "bridge_status", None)) or None
        thread_subscription_status = _normalized(getattr(lease, "thread_subscription_status", None)) or None
        control_state, reason = _lease_control_state(
            lease_state=lease_state,
            bridge_status=bridge_status,
            thread_subscription_status=thread_subscription_status,
        )
        ttl_ms = int(getattr(lease, "lease_ttl_ms", 0) or 0) or DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS
        expires_at = seen_at + timedelta(milliseconds=ttl_ms)
        row = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id == session_id).first()
        values = {
            "session_id": session_id,
            "provider": provider,
            "device_id": device_id,
            "machine_id": _normalized(getattr(lease, "machine_id", None)) or None,
            "transport": _session_transport(db, session_id),
            "lease_state": lease_state,
            "control_state": control_state,
            "reason": reason,
            "source": CONTROL_SOURCE_HEARTBEAT,
            "sequence": int(getattr(lease, "sequence", 0) or 0),
            "last_control_seen_at": seen_at,
            "lease_observed_at": normalize_utc(getattr(lease, "observed_at", None)),
            "lease_ttl_ms": ttl_ms,
            "control_expires_at": expires_at,
            "bridge_status": bridge_status,
            "thread_subscription_status": thread_subscription_status,
        }
        if row is None:
            db.add(ManagedSessionControlState(**values))
            touched.add(session_id)
            continue
        changed = any(getattr(row, key) != value for key, value in values.items() if key != "session_id")
        for key, value in values.items():
            setattr(row, key, value)
        if changed:
            touched.add(session_id)
    return touched


def mark_missing_managed_control_leases(
    db: Session,
    leases: list[Any],
    *,
    device_id: str,
    received_at: datetime,
) -> set[UUID]:
    """Mark known managed controls from this device offline when omitted."""
    seen_session_ids = {getattr(lease, "session_id", None) for lease in leases}
    seen_session_ids.discard(None)
    seen_at = normalize_utc(received_at) or _utc_now()
    query = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.device_id == device_id)
    if seen_session_ids:
        query = query.filter(ManagedSessionControlState.session_id.notin_(seen_session_ids))
    rows = query.all()
    touched: set[UUID] = set()
    for row in rows:
        if row.control_state == "offline" and row.reason == "missing_from_snapshot":
            continue
        row.lease_state = "missing"
        row.control_state = "offline"
        row.reason = "missing_from_snapshot"
        row.source = CONTROL_SOURCE_HEARTBEAT
        row.last_control_seen_at = seen_at
        row.control_expires_at = seen_at
        touched.add(row.session_id)
    return touched


def load_managed_control_state_map(
    db: Session,
    session_ids: list[UUID],
) -> dict[UUID, ManagedControlOverlay]:
    if not session_ids:
        return {}
    rows = db.query(ManagedSessionControlState).filter(ManagedSessionControlState.session_id.in_(session_ids)).all()
    return {row.session_id: _overlay_from_row(row) for row in rows}
