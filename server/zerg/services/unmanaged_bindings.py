"""Read-side service for Phase 5c + 6 of session-liveness-honesty.

Maps unmanaged sessions to machine-agent observations so
``session_runtime_display.host_state`` and (Phase 6) ``lifecycle=closed``
are driven by ground truth instead of heuristics.

Inputs:

* ``unmanaged_session_bindings``: one row per (machine, provider,
  provider_session_id) the machine agent most recently observed.
  Populated by ``/api/agents/heartbeat`` (see Phase 5a).
* ``agent_heartbeats``: latest heartbeat per machine. Used to decide
  whether the host's observations are still fresh enough to trust.

Outputs, keyed by session UUID:

* ``host_state``: ``online`` / ``stale`` / ``offline`` / ``unknown``.
* ``terminal_reason``: ``process_gone`` when Phase 6 criteria are met, or
  ``host_expired`` when a previously observed host has been offline too long
  to keep the session actionable.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import UnmanagedSessionBinding

# Engine server heartbeat cadence (see engine/src/daemon.rs). Must be the
# same value or Phase 6 false-positives every other heartbeat.
HEARTBEAT_CADENCE = timedelta(minutes=5)

# A heartbeat newer than ~2x the cadence means the host is actively
# talking to us. 2x lets one missed heartbeat land without flipping the
# machine to stale.
HOST_ONLINE_WINDOW = HEARTBEAT_CADENCE * 2

# Heartbeats older than this are stale — we still know the host exists
# but cannot claim its bindings still apply.
HOST_STALE_WINDOW = timedelta(minutes=30)

# If a host has been offline this long, close old unmanaged observations as
# unverifiable rather than process-gone. This is a lifecycle cleanup, not proof
# the provider process exited.
HOST_EXPIRED_WINDOW = timedelta(days=7)

# For Phase 6: only promote to process_gone if the latest binding was
# last_seen_at more than this long ago. Must be > HOST_ONLINE_WINDOW so
# "one heartbeat missed the binding" can't alone trigger closure.
BINDING_GONE_WINDOW = HOST_ONLINE_WINDOW + HEARTBEAT_CADENCE

# For Phase 6 we additionally require the transcript to be inactive for
# this window before inferring process_gone. A provider CLI that closed
# its fd between writes will appear unbound to the scanner; the growing
# JSONL tells us the process is still alive.
TRANSCRIPT_STALE_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class BindingOverlay:
    """Per-session info used to color the display contract."""

    host_state: str  # online | stale | offline | unknown
    terminal_reason: str | None  # process_gone | host_expired | None
    host_last_seen_at: datetime | None = None
    machine_id: str | None = None
    device_id: str | None = None
    pid: int | None = None
    process_start_time: datetime | None = None
    observed_at: datetime | None = None
    last_seen_at: datetime | None = None
    source_mtime: datetime | None = None
    source_path: str | None = None
    binding_state: str | None = None


def load_binding_overlay(
    db: Session,
    session_ids: Iterable[UUID],
    *,
    now: datetime | None = None,
) -> Mapping[UUID, BindingOverlay]:
    """Return the binding overlay for each session we know about.

    Only sessions we have a binding row for get an entry; sessions with
    no binding are intentionally left out — the caller should default to
    ``host_state="unknown"``.
    """
    ids = {sid for sid in session_ids if sid is not None}
    if not ids:
        return {}

    current = (now or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)

    # Order by last_seen_at desc so the newest observation for each
    # session wins when multiple machines have reported the same one.
    bindings = (
        db.query(UnmanagedSessionBinding)
        .filter(UnmanagedSessionBinding.session_id.in_(ids))
        .order_by(UnmanagedSessionBinding.last_seen_at.desc())
        .all()
    )
    if not bindings:
        return {}

    # Use the binding's device_id (the heartbeat-auth token identity)
    # for heartbeat freshness — not machine_id, which is config-supplied
    # on the engine side and may not match. Fall back to machine_id when
    # device_id is null (older engines or tokenless installs).
    def _heartbeat_key(binding: UnmanagedSessionBinding) -> str:
        return str(binding.device_id or binding.machine_id or "")

    heartbeat_keys = {_heartbeat_key(b) for b in bindings}
    heartbeat_keys.discard("")
    host_seen_at_by_key = _latest_heartbeat_at(db, heartbeat_keys)
    host_state_by_key = _heartbeat_state_from_latest(host_seen_at_by_key, heartbeat_keys, now=current)

    overlay: dict[UUID, BindingOverlay] = {}
    for binding in bindings:
        session_id = binding.session_id
        if session_id is None or session_id in overlay:
            # Rows are already newest-first; keep the first one.
            continue
        heartbeat_key = _heartbeat_key(binding)
        host_state = host_state_by_key.get(heartbeat_key, "unknown")
        heartbeat_at = host_seen_at_by_key.get(heartbeat_key)

        terminal_reason: str | None = None
        binding_state = (binding.binding_state or "observed").strip().lower()
        last_seen = _as_utc(binding.last_seen_at)
        source_mtime = _as_utc(binding.source_mtime) if binding.source_mtime else None
        # Phase 6: promote to closed only with ground truth. Two shapes:
        #   - engine explicitly marked binding 'stale'
        #   - engine is online AND last_seen_at gap exceeds
        #     BINDING_GONE_WINDOW AND the transcript has not grown for
        #     TRANSCRIPT_STALE_WINDOW. The transcript check avoids
        #     false-positives when the provider CLI closes its fd between
        #     writes — the scanner sees no open fd, but the file keeps
        #     growing.
        if binding_state == "stale":
            terminal_reason = "process_gone"
        elif host_state == "offline":
            if heartbeat_at is not None and current - _as_utc(heartbeat_at) > HOST_EXPIRED_WINDOW:
                terminal_reason = "host_expired"
        elif (
            host_state == "online"
            and last_seen is not None
            and current - last_seen > BINDING_GONE_WINDOW
            and (source_mtime is None or current - source_mtime > TRANSCRIPT_STALE_WINDOW)
        ):
            terminal_reason = "process_gone"

        overlay[session_id] = BindingOverlay(
            host_state=host_state,
            terminal_reason=terminal_reason,
            host_last_seen_at=_as_utc(heartbeat_at) if heartbeat_at else None,
            machine_id=binding.machine_id,
            device_id=binding.device_id,
            pid=binding.pid,
            process_start_time=_as_utc(binding.process_start_time) if binding.process_start_time else None,
            observed_at=_as_utc(binding.observed_at) if binding.observed_at else None,
            last_seen_at=last_seen,
            source_mtime=source_mtime,
            source_path=binding.source_path,
            binding_state=binding_state,
        )

    return overlay


def _latest_heartbeat_at(db: Session, device_ids: set[str]) -> dict[str, datetime]:
    if not device_ids:
        return {}

    from sqlalchemy import func

    rows = (
        db.query(AgentHeartbeat.device_id, func.max(AgentHeartbeat.received_at))
        .filter(AgentHeartbeat.device_id.in_(device_ids))
        .group_by(AgentHeartbeat.device_id)
        .all()
    )
    return {device_id: received for device_id, received in rows if received is not None}


def _heartbeat_state_from_latest(
    latest: dict[str, datetime],
    device_ids: set[str],
    *,
    now: datetime,
) -> dict[str, str]:
    states: dict[str, str] = {}
    for device_id in device_ids:
        received = latest.get(device_id)
        if received is None:
            states[device_id] = "unknown"
            continue
        age = now - _as_utc(received)
        if age <= HOST_ONLINE_WINDOW:
            states[device_id] = "online"
        elif age <= HOST_STALE_WINDOW:
            states[device_id] = "stale"
        else:
            states[device_id] = "offline"
    return states


def _latest_heartbeat_state(
    db: Session,
    device_ids: set[str],
    *,
    now: datetime,
) -> dict[str, str]:
    """Return ``{device_id: host_state}`` for each listed device.

    ``device_id`` is the heartbeat-auth identity (see
    ``routers/heartbeat.py``). The engine's Rust scanner also stores
    this as ``UnmanagedSessionBinding.device_id`` so we join on it.
    """
    if not device_ids:
        return {}

    from sqlalchemy import func

    rows = (
        db.query(AgentHeartbeat.device_id, func.max(AgentHeartbeat.received_at))
        .filter(AgentHeartbeat.device_id.in_(device_ids))
        .group_by(AgentHeartbeat.device_id)
        .all()
    )

    latest: dict[str, datetime] = {device_id: received for device_id, received in rows}

    return _heartbeat_state_from_latest(latest, device_ids, now=now)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
