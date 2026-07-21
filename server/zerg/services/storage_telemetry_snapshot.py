"""TTL-cached catalog telemetry for scrapes and historical admission."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from zerg.services.catalogd_supervisor import get_catalogd_client

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_SECONDS = 15.0


def _refresh_seconds() -> float:
    raw = os.getenv("LONGHOUSE_STORAGE_TELEMETRY_REFRESH_SECONDS", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_REFRESH_SECONDS
    except ValueError:
        value = _DEFAULT_REFRESH_SECONDS
    return max(1.0, value)


@dataclass(frozen=True, slots=True)
class StorageTelemetrySnapshot:
    payload: dict[str, Any] | None
    refreshed_at_monotonic: float | None
    last_success_timestamp: float | None
    last_error: str | None

    @property
    def fresh(self) -> bool:
        if self.payload is None or self.refreshed_at_monotonic is None:
            return False
        return time.monotonic() - self.refreshed_at_monotonic <= _refresh_seconds() * 2.0

    @property
    def total_stored_bytes(self) -> int | None:
        if self.payload is None:
            return None
        objects = self.payload.get("objects")
        if not isinstance(objects, dict):
            return None
        total = 0
        for kind in ("raw", "render", "media"):
            row = objects.get(kind)
            if not isinstance(row, dict) or type(row.get("bytes")) is not int:
                return None
            total += int(row["bytes"])
        return total


_snapshot = StorageTelemetrySnapshot(None, None, None, "not_started")
_refresh_lock = asyncio.Lock()


def get_storage_telemetry_snapshot() -> StorageTelemetrySnapshot:
    return _snapshot


async def refresh_storage_telemetry_snapshot(*, force: bool = False) -> StorageTelemetrySnapshot:
    global _snapshot
    current = _snapshot
    if not force and current.fresh:
        return current
    async with _refresh_lock:
        current = _snapshot
        if not force and current.fresh:
            return current
        client = get_catalogd_client()
        if client is None:
            _snapshot = StorageTelemetrySnapshot(
                current.payload,
                current.refreshed_at_monotonic,
                current.last_success_timestamp,
                "catalog_unavailable",
            )
            return _snapshot
        try:
            payload = await client.call("storage.telemetry.summary.v2", {}, timeout_seconds=1.0)
            _validate_payload(payload)
        except Exception as exc:
            logger.warning("Storage telemetry refresh failed: %s", exc)
            _snapshot = StorageTelemetrySnapshot(
                current.payload,
                current.refreshed_at_monotonic,
                current.last_success_timestamp,
                f"{type(exc).__name__}: {exc}",
            )
            return _snapshot
        now_monotonic = time.monotonic()
        _snapshot = StorageTelemetrySnapshot(payload, now_monotonic, datetime.now(timezone.utc).timestamp(), None)
        return _snapshot


async def run_storage_telemetry_refresh_loop() -> None:
    while True:
        try:
            await refresh_storage_telemetry_snapshot(force=True)
            await asyncio.sleep(_refresh_seconds())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Storage telemetry refresh loop failed")
            await asyncio.sleep(_refresh_seconds())


def reset_storage_telemetry_snapshot_for_tests() -> None:
    global _snapshot
    _snapshot = StorageTelemetrySnapshot(None, None, None, "not_started")


def _validate_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("catalog telemetry summary is not an object")
    objects = payload.get("objects")
    projectors = payload.get("projectors")
    if not isinstance(objects, dict) or not isinstance(projectors, list):
        raise ValueError("catalog telemetry summary is incomplete")
    for kind in ("raw", "render", "media"):
        row = objects.get(kind)
        if not isinstance(row, dict) or type(row.get("count")) is not int or type(row.get("bytes")) is not int:
            raise ValueError(f"catalog telemetry {kind} summary is invalid")
    for row in projectors:
        if not isinstance(row, dict) or not isinstance(row.get("projector"), str):
            raise ValueError("catalog projector telemetry row is invalid")
        if any(type(row.get(field)) is not int for field in ("lagging", "failed", "claimed")):
            raise ValueError("catalog projector telemetry counts are invalid")


__all__ = [
    "StorageTelemetrySnapshot",
    "get_storage_telemetry_snapshot",
    "refresh_storage_telemetry_snapshot",
    "reset_storage_telemetry_snapshot_for_tests",
    "run_storage_telemetry_refresh_loop",
]
