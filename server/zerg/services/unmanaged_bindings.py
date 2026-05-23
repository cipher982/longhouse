"""Compatibility shim for the deleted unmanaged-binding service.

The kernel now owns observation evidence on ``SessionConnection`` rows
(``acquisition_kind='observe_only'`` / ``control_plane='log_tail'``).
The legacy ``UnmanagedSessionBinding`` table and the elaborate
host-freshness projection have been removed.

We keep ``BindingOverlay`` and ``load_binding_overlay`` as no-op stubs so
existing callers compile and gracefully fall back to ``host_state="unknown"``.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from uuid import UUID

from sqlalchemy.orm import Session

HEARTBEAT_CADENCE = timedelta(minutes=5)
HOST_ONLINE_WINDOW = HEARTBEAT_CADENCE * 2
HOST_STALE_WINDOW = timedelta(minutes=30)
HOST_EXPIRED_WINDOW = timedelta(days=7)
BINDING_GONE_WINDOW = HOST_ONLINE_WINDOW + HEARTBEAT_CADENCE
TRANSCRIPT_STALE_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class BindingOverlay:
    """Per-session info used to color the display contract."""

    host_state: str  # online | stale | offline | unknown
    terminal_reason: str | None
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
    """Return an empty overlay map.

    Callers must already cope with sessions absent from the result
    (host_state defaults to ``unknown``). We keep the function so import
    paths and signatures remain stable.
    """

    return {}
