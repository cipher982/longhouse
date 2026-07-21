"""Byte- and disk-aware admission for reconstructable historical work."""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from zerg import metrics

DEFAULT_MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_RATIO = 0.05
_DISK_SAMPLE_TTL_SECONDS = 2.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _byte_budget_configuration() -> tuple[int, int]:
    return (
        max(0, _env_int("LONGHOUSE_HISTORICAL_BYTES_PER_SECOND", 0)),
        max(0, _env_int("LONGHOUSE_HISTORICAL_BURST_BYTES", 0)),
    )


@dataclass(frozen=True, slots=True)
class HistoricalAdmissionDecision:
    admitted: bool
    reason: str
    retry_after_seconds: int
    disk_free_bytes: int | None = None
    disk_free_ratio: float | None = None
    stored_bytes: int | None = None
    stored_ceiling_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    free_bytes: int | None
    free_ratio: float | None
    sampled_at_monotonic: float
    error: str | None


class _HistoricalByteBucket:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._updated_at = time.monotonic()
        self._configuration: tuple[int, int] | None = None

    def consume(self, amount: int) -> tuple[bool, int, float]:
        rate, burst = _byte_budget_configuration()
        if rate == 0 or burst == 0:
            metrics.historical_budget_available_bytes.set(-1.0)
            return True, 0, -1.0
        now = time.monotonic()
        with self._lock:
            configuration = (rate, burst)
            if self._configuration != configuration:
                self._configuration = configuration
                self._tokens = float(burst)
                self._updated_at = now
            elapsed = max(0.0, now - self._updated_at)
            self._tokens = min(float(burst), self._tokens + elapsed * rate)
            self._updated_at = now
            if amount <= self._tokens:
                self._tokens -= amount
                metrics.historical_budget_available_bytes.set(self._tokens)
                return True, 0, self._tokens
            deficit = amount - self._tokens
            retry_after = max(1, int((deficit + rate - 1) // rate))
            metrics.historical_budget_available_bytes.set(self._tokens)
            return False, retry_after, self._tokens

    def reset(self) -> None:
        with self._lock:
            self._tokens = 0.0
            self._updated_at = time.monotonic()
            self._configuration = None


_budget = _HistoricalByteBucket()
_disk_lock = threading.Lock()
_disk_cache: dict[Path, DiskSnapshot] = {}


def evaluate_historical_admission(
    *,
    root: Path,
    admitted_bytes: int,
    stored_bytes: int | None,
    enforce_stored_ceiling: bool = True,
) -> HistoricalAdmissionDecision:
    """Evaluate one historical unit; live work must never call this function."""

    disk = sample_storage_disk(root)
    if disk.error is not None or disk.free_bytes is None or disk.free_ratio is None:
        return HistoricalAdmissionDecision(False, "disk_sample_unavailable", 30)
    min_free_bytes = max(0, _env_int("LONGHOUSE_HISTORICAL_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES))
    min_free_ratio = min(1.0, max(0.0, _env_float("LONGHOUSE_HISTORICAL_MIN_FREE_RATIO", DEFAULT_MIN_FREE_RATIO)))
    if disk.free_bytes < min_free_bytes or disk.free_ratio < min_free_ratio:
        return HistoricalAdmissionDecision(
            False,
            "disk_watermark",
            60,
            disk_free_bytes=disk.free_bytes,
            disk_free_ratio=disk.free_ratio,
        )
    stored_ceiling = tenant_stored_bytes_ceiling() if enforce_stored_ceiling else 0
    if stored_ceiling > 0:
        if stored_bytes is None:
            return HistoricalAdmissionDecision(
                False,
                "stored_usage_unavailable",
                30,
                disk_free_bytes=disk.free_bytes,
                disk_free_ratio=disk.free_ratio,
                stored_ceiling_bytes=stored_ceiling,
            )
        if stored_bytes >= stored_ceiling:
            return HistoricalAdmissionDecision(
                False,
                "stored_byte_ceiling",
                300,
                disk_free_bytes=disk.free_bytes,
                disk_free_ratio=disk.free_ratio,
                stored_bytes=stored_bytes,
                stored_ceiling_bytes=stored_ceiling,
            )
    rate, burst = _byte_budget_configuration()
    if rate > 0 and burst > 0 and admitted_bytes > burst:
        return HistoricalAdmissionDecision(
            False,
            "historical_unit_exceeds_burst",
            300,
            disk_free_bytes=disk.free_bytes,
            disk_free_ratio=disk.free_ratio,
            stored_bytes=stored_bytes,
            stored_ceiling_bytes=stored_ceiling or None,
        )
    admitted, retry_after, _available = _budget.consume(max(0, admitted_bytes))
    if not admitted:
        return HistoricalAdmissionDecision(
            False,
            "historical_byte_budget",
            retry_after,
            disk_free_bytes=disk.free_bytes,
            disk_free_ratio=disk.free_ratio,
            stored_bytes=stored_bytes,
            stored_ceiling_bytes=stored_ceiling or None,
        )
    return HistoricalAdmissionDecision(
        True,
        "admitted",
        0,
        disk_free_bytes=disk.free_bytes,
        disk_free_ratio=disk.free_ratio,
        stored_bytes=stored_bytes,
        stored_ceiling_bytes=stored_ceiling or None,
    )


def tenant_stored_bytes_ceiling() -> int:
    return max(0, _env_int("LONGHOUSE_TENANT_STORED_BYTES_CEILING", 0))


def sample_storage_disk(root: Path, *, force: bool = False) -> DiskSnapshot:
    path = root.expanduser().resolve()
    now = time.monotonic()
    with _disk_lock:
        cached = _disk_cache.get(path)
        if not force and cached is not None and now - cached.sampled_at_monotonic <= _DISK_SAMPLE_TTL_SECONDS:
            return cached
        try:
            probe = path
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            usage = shutil.disk_usage(probe)
            ratio = usage.free / usage.total if usage.total > 0 else 0.0
            snapshot = DiskSnapshot(int(usage.free), float(ratio), now, None)
            metrics.historical_disk_free_bytes.set(float(usage.free))
            metrics.historical_disk_free_ratio.set(ratio)
            metrics.telemetry_health.labels(component="host_disk_sample").set(1.0)
            metrics.telemetry_last_success_timestamp_seconds.labels(component="host_disk_sample").set(time.time())
        except OSError as exc:
            snapshot = DiskSnapshot(None, None, now, f"{type(exc).__name__}: {exc}")
            metrics.telemetry_health.labels(component="host_disk_sample").set(0.0)
        _disk_cache[path] = snapshot
        return snapshot


def reset_historical_admission_for_tests() -> None:
    _budget.reset()
    with _disk_lock:
        _disk_cache.clear()


__all__ = [
    "DiskSnapshot",
    "HistoricalAdmissionDecision",
    "evaluate_historical_admission",
    "reset_historical_admission_for_tests",
    "sample_storage_disk",
    "tenant_stored_bytes_ceiling",
]
