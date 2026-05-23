"""Managed-session control liveness projection.

Post-kernel cleanup: the legacy ``ManagedSessionControlState`` table is
gone. Control liveness is derived from ``SessionConnection`` rows owned by
the kernel projection. This module keeps a small overlay shape so existing
callers don't have to rewrite every projection at once.
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
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.utils.time import normalize_utc

CONTROL_SOURCE_HEARTBEAT = "machine_heartbeat"
CONTROL_SOURCE_ENGINE_CHANNEL = "machine_control_ws"
CONTROL_SOURCE_LEGACY_RUNNER = "legacy_runner"
DEFAULT_MANAGED_CONTROL_LEASE_TTL_MS = 15 * 60 * 1000
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
    "codex_bridge": "codex",
    "claude_channel_bridge": "claude",
    "opencode_process": "opencode",
    "antigravity_process": "antigravity",
}


def _kernel_connection_state(control_state: str) -> str:
    return _KERNEL_STATE_BY_CONTROL_STATE.get(control_state, "detached")


def _kernel_control_plane_for_provider(provider: str) -> str:
    if provider == "codex":
        return "codex_bridge"
    if provider == "opencode":
        return "opencode_process"
    if provider == "antigravity":
        return "antigravity_process"
    return "claude_channel_bridge"


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
        upsert_connection_for_run(
            db,
            run=db.query(SessionRun).filter(SessionRun.id == existing.run_id).one(),
            control_plane=control_plane,
            acquisition_kind=existing.acquisition_kind,
            state=kernel_state,
            external_name=external_name,
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
        device_id=None,
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
        machine_id=_normalized(machine_id) or None,
        transport=None,
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


def engine_channel_control_overlay(
    session: AgentSession,
    *,
    seen_at: datetime,
) -> ManagedControlOverlay:
    """Represent the live Machine Agent control WebSocket as control facts."""

    return live_transport_control_overlay(
        session,
        source=CONTROL_SOURCE_ENGINE_CHANNEL,
        seen_at=seen_at,
    )


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
            device_id=device_id,
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
            .filter(SessionThread.session_id.in_(touched))
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

    seen_session_ids = {getattr(lease, "session_id", None) for lease in leases}
    seen_session_ids.discard(None)
    # Without ManagedSessionControlState we no longer have a per-device
    # index of "what was previously known managed for this device". The
    # heartbeat path still owns liveness via ``_mirror_connection_state``
    # for present leases; absent ones can be conservatively flipped via the
    # control_plane state already on SessionConnection if their last health
    # is older than the snapshot's seen_at.
    seen_at = normalize_utc(received_at) or _utc_now()
    touched: set[UUID] = set()

    # Find connections in attached/degraded state whose health timestamps
    # predate this snapshot — those must be missing from the snapshot.
    query = (
        db.query(SessionConnection, SessionThread.session_id)
        .join(SessionRun, SessionConnection.run_id == SessionRun.id)
        .join(SessionThread, SessionRun.thread_id == SessionThread.id)
        .filter(SessionConnection.state.in_(("attached", "degraded")))
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
    return {sid: _overlay_from_connection(session_id=sid, conn=conn) for sid, conn in best.items()}


def _connection_priority(conn: SessionConnection) -> tuple:
    state = _normalized(conn.state).lower()
    state_rank = {"attached": 5, "degraded": 4, "detached": 3, "released": 2, "ended": 1}.get(state, 0)
    last_health = normalize_utc(conn.last_health_at) or datetime.min.replace(tzinfo=timezone.utc)
    return (state_rank, last_health, conn.id)
