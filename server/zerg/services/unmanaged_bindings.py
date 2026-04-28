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
* ``terminal_reason``: ``process_gone`` when Phase 6 criteria are met.
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

# A heartbeat newer than this window means the host is actively talking
# to us and its per-session bindings reflect current truth.
HOST_ONLINE_WINDOW = timedelta(minutes=2)

# Heartbeats older than this are stale — we still know the host exists
# but cannot claim its bindings still apply.
HOST_STALE_WINDOW = timedelta(minutes=30)

# For Phase 6: if the latest binding for an unmanaged session was
# last_seen_at more than this long ago AND the host is online, treat the
# process as gone.
BINDING_GONE_WINDOW = timedelta(minutes=2)


@dataclass(frozen=True)
class BindingOverlay:
    """Per-session info used to color the display contract."""

    host_state: str  # online | stale | offline | unknown
    terminal_reason: str | None  # "process_gone" when Phase 6 triggers


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

    bindings = db.query(UnmanagedSessionBinding).filter(UnmanagedSessionBinding.session_id.in_(ids)).all()
    if not bindings:
        return {}

    machine_ids = {str(b.machine_id) for b in bindings if b.machine_id}
    host_state_by_machine = _latest_heartbeat_state(db, machine_ids, now=current)

    overlay: dict[UUID, BindingOverlay] = {}
    for binding in bindings:
        session_id = binding.session_id
        if session_id is None:
            continue
        host_state = host_state_by_machine.get(str(binding.machine_id), "unknown")

        terminal_reason: str | None = None
        binding_state = (binding.binding_state or "observed").strip().lower()
        last_seen = _as_utc(binding.last_seen_at)
        # Phase 6: promote to closed only with ground truth. Two shapes
        # of ground truth:
        #   - engine explicitly marked binding 'stale'
        #   - engine is online but its latest heartbeat no longer lists
        #     the binding (so last_seen_at is older than current heartbeat
        #     and the gap exceeds BINDING_GONE_WINDOW).
        if binding_state == "stale":
            terminal_reason = "process_gone"
        elif host_state == "online" and last_seen is not None:
            if current - last_seen > BINDING_GONE_WINDOW:
                terminal_reason = "process_gone"

        overlay[session_id] = BindingOverlay(
            host_state=host_state,
            terminal_reason=terminal_reason,
        )

    return overlay


def _latest_heartbeat_state(
    db: Session,
    machine_ids: set[str],
    *,
    now: datetime,
) -> dict[str, str]:
    """Return ``{machine_id: host_state}`` for each listed machine.

    ``machine_id`` as emitted by the engine's Rust scanner is the same
    value that flows through as the heartbeat's ``device_id``. We join
    on that.
    """
    if not machine_ids:
        return {}

    rows = (
        db.query(AgentHeartbeat.device_id, AgentHeartbeat.received_at)
        .filter(AgentHeartbeat.device_id.in_(machine_ids))
        .order_by(AgentHeartbeat.device_id, AgentHeartbeat.received_at.desc())
        .all()
    )

    latest: dict[str, datetime] = {}
    for device_id, received_at in rows:
        if device_id in latest:
            continue
        latest[device_id] = received_at

    states: dict[str, str] = {}
    for machine_id in machine_ids:
        received = latest.get(machine_id)
        if received is None:
            states[machine_id] = "unknown"
            continue
        age = now - _as_utc(received)
        if age <= HOST_ONLINE_WINDOW:
            states[machine_id] = "online"
        elif age <= HOST_STALE_WINDOW:
            states[machine_id] = "stale"
        else:
            states[machine_id] = "offline"
    return states


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
