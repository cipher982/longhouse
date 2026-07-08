"""Small archive-lane pressure decisions shared by ingest and health."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ARCHIVE_WAL_SHED_BYTES = 1 * 1024 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def archive_wal_shed_threshold_bytes() -> int:
    """Return the WAL size at which archive replay/scan should shed."""

    return _env_int("LONGHOUSE_ARCHIVE_INGEST_WAL_SHED_BYTES", DEFAULT_ARCHIVE_WAL_SHED_BYTES)


@dataclass(frozen=True)
class ArchiveWalPressure:
    wal_bytes: int | None
    threshold_bytes: int
    shed: bool
    status: str
    reason: str | None = None

    def as_health_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "threshold_bytes": self.threshold_bytes,
            "shed": self.shed,
        }
        if self.wal_bytes is not None:
            payload["wal_bytes"] = self.wal_bytes
        if self.reason:
            payload["reason"] = self.reason
        return payload


def evaluate_archive_wal_pressure(wal_bytes: int | None) -> ArchiveWalPressure:
    """Evaluate archive WAL pressure without touching the database."""

    threshold = archive_wal_shed_threshold_bytes()
    if threshold <= 0:
        return ArchiveWalPressure(
            wal_bytes=wal_bytes,
            threshold_bytes=threshold,
            shed=False,
            status="skip",
            reason="disabled",
        )
    if wal_bytes is None:
        return ArchiveWalPressure(
            wal_bytes=None,
            threshold_bytes=threshold,
            shed=False,
            status="skip",
            reason="wal path unknown",
        )
    if wal_bytes >= threshold:
        return ArchiveWalPressure(
            wal_bytes=wal_bytes,
            threshold_bytes=threshold,
            shed=True,
            status="warn",
            reason="archive_wal_pressure",
        )
    return ArchiveWalPressure(
        wal_bytes=wal_bytes,
        threshold_bytes=threshold,
        shed=False,
        status="pass",
    )
