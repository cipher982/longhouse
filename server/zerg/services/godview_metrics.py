"""Refresh god-view Prometheus gauges from current operational state.

These gauges turn two classes of currently-discarded telemetry into retained
time series:

- the WriteSerializer + SQLite WAL state that today only appears in the
  point-in-time ``/health`` JSON, and
- the per-device shipping state that today is read latest-per-device from the
  ``agent_heartbeats`` table and never trended.

The refresh runs at scrape time (called from ``routers/metrics.py``), so we
never sample on a timer that nobody reads and never touch the shipping hot
path. Prometheus only retains samples it actually scrapes anyway.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime
from datetime import timezone

from sqlalchemy import text

from zerg import metrics
from zerg.services.db_diagnostics import SQLiteTableBytesTimeout
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

_QUANTILE_KEYS = ("p50", "p95", "p99")
_TRUTHY_ENV = {"1", "true", "yes", "on"}
_LIVE_STORE_TABLE_BYTES_METRICS_ENV = "LONGHOUSE_LIVE_STORE_TABLE_BYTES_METRICS"
_LIVE_STORE_TABLE_BYTES_DEADLINE_MS_ENV = "LONGHOUSE_LIVE_STORE_TABLE_BYTES_DEADLINE_MS"
_LIVE_STORE_TABLE_BYTES_DEFAULT_DEADLINE_MS = 50


def refresh_write_serializer_gauges() -> None:
    """Mirror WriteSerializer.get_metrics() + WAL bytes into gauges."""
    try:
        from zerg.services.write_serializer import get_write_serializer

        ws = get_write_serializer()
        if not ws.is_configured:
            return
        m = ws.get_metrics()
    except Exception:
        logger.exception("godview: failed to read write serializer metrics")
        return

    metrics.write_serializer_queue_depth.set(float(m.get("queue_depth", 0)))
    metrics.write_serializer_writer_active.set(1.0 if m.get("writer_active") else 0.0)
    metrics.write_serializer_active_age_ms.set(float(m.get("active_age_ms", 0.0)))
    metrics.write_serializer_idle_queue_stalled.set(1.0 if m.get("idle_queue_stalled") else 0.0)

    rolling = m.get("rolling_by_label") or {}
    for label, buckets in rolling.items():
        wait = (buckets or {}).get("queue_wait_ms") or {}
        exec_ = (buckets or {}).get("exec_ms") or {}
        for quantile in _QUANTILE_KEYS:
            if quantile in wait and wait[quantile] is not None:
                metrics.write_serializer_queue_wait_ms.labels(label=label, quantile=quantile).set(float(wait[quantile]))
            if quantile in exec_ and exec_[quantile] is not None:
                metrics.write_serializer_exec_ms.labels(label=label, quantile=quantile).set(float(exec_[quantile]))

    try:
        from zerg.database import get_wal_bytes

        wal_bytes = get_wal_bytes()
        if wal_bytes is not None:
            metrics.sqlite_wal_bytes.set(float(wal_bytes))
    except Exception:
        logger.exception("godview: failed to read WAL bytes")


def refresh_live_write_serializer_gauges() -> None:
    """Mirror the Live Store WriteSerializer + WAL state into gauges."""
    try:
        from zerg.services.write_serializer import get_live_write_serializer

        ws = get_live_write_serializer()
        if not ws.is_configured:
            return
        m = ws.get_metrics()
    except Exception:
        logger.exception("godview: failed to read live write serializer metrics")
        return

    metrics.live_write_serializer_queue_depth.set(float(m.get("queue_depth", 0)))
    metrics.live_write_serializer_writer_active.set(1.0 if m.get("writer_active") else 0.0)
    metrics.live_write_serializer_active_age_ms.set(float(m.get("active_age_ms", 0.0)))
    metrics.live_write_serializer_idle_queue_stalled.set(1.0 if m.get("idle_queue_stalled") else 0.0)

    rolling = m.get("rolling_by_label") or {}
    for label, buckets in rolling.items():
        wait = (buckets or {}).get("queue_wait_ms") or {}
        exec_ = (buckets or {}).get("exec_ms") or {}
        for quantile in _QUANTILE_KEYS:
            if quantile in wait and wait[quantile] is not None:
                metrics.live_write_serializer_queue_wait_ms.labels(label=label, quantile=quantile).set(float(wait[quantile]))
            if quantile in exec_ and exec_[quantile] is not None:
                metrics.live_write_serializer_exec_ms.labels(label=label, quantile=quantile).set(float(exec_[quantile]))

    try:
        from zerg.database import get_live_wal_bytes

        wal_bytes = get_live_wal_bytes()
        if wal_bytes is not None:
            metrics.live_sqlite_wal_bytes.set(float(wal_bytes))
    except Exception:
        logger.exception("godview: failed to read Live Store WAL bytes")


def refresh_live_store_gauges() -> None:
    """Refresh Live Store outbox and table-size gauges from the hot DB."""
    try:
        from zerg.database import get_live_session_factory
        from zerg.database import live_store_configured

        if not live_store_configured():
            return
        live_session_factory = get_live_session_factory()
        if live_session_factory is None:
            return
        with live_session_factory() as db:
            _refresh_live_archive_outbox_gauges(db)
            _refresh_live_store_table_bytes_gauges(db)
    except Exception:
        logger.exception("godview: failed to refresh Live Store gauges")


def _refresh_live_archive_outbox_gauges(db) -> None:
    now = datetime.now(timezone.utc)
    row = db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE drained_at IS NULL) AS pending_count,
                COUNT(*) FILTER (WHERE drained_at IS NULL AND last_error IS NOT NULL) AS failed_count,
                MIN(CASE WHEN drained_at IS NULL THEN created_at END) AS oldest_pending_created_at,
                MAX(CASE WHEN drained_at IS NOT NULL THEN drained_at END) AS latest_drained_at,
                MAX(attempts) AS max_attempts
            FROM live_archive_outbox
            """
        )
    ).fetchone()
    if row is None:
        return

    pending_count = int(row[0] or 0)
    failed_count = int(row[1] or 0)
    metrics.live_archive_outbox_pending.set(float(pending_count))
    metrics.live_archive_outbox_failed.set(float(failed_count))
    metrics.live_archive_outbox_max_attempts.set(float(int(row[4] or 0)))

    oldest_pending = _normalize_db_datetime(row[2])
    if oldest_pending is not None:
        metrics.live_archive_outbox_oldest_pending_age_seconds.set(max(0.0, (now - oldest_pending).total_seconds()))
    elif pending_count == 0:
        metrics.live_archive_outbox_oldest_pending_age_seconds.set(0.0)

    latest_drained = _normalize_db_datetime(row[3])
    if latest_drained is not None:
        metrics.live_archive_outbox_last_drained_age_seconds.set(max(0.0, (now - latest_drained).total_seconds()))


def _normalize_db_datetime(value) -> datetime | None:
    if isinstance(value, str):
        try:
            return normalize_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return normalize_utc(value)


def _refresh_live_store_table_bytes_gauges(_db) -> None:
    if not _live_store_table_bytes_metrics_enabled():
        return

    try:
        from zerg.services.db_diagnostics import collect_sqlite_table_bytes_with_deadline
        from zerg.services.db_diagnostics import sqlite_db_paths

        live_database_url = _live_store_database_url()
        paths = sqlite_db_paths(live_database_url) if live_database_url else None
        if paths is None:
            return

        db_path = paths[0].expanduser()
        if not db_path.exists():
            return

        deadline_monotonic = time.monotonic() + _live_store_table_bytes_deadline_seconds()
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1) as conn:
            table_bytes = collect_sqlite_table_bytes_with_deadline(conn, deadline_monotonic=deadline_monotonic)
    except SQLiteTableBytesTimeout:
        logger.warning("godview: Live Store table-byte collection timed out")
        return
    except Exception:
        logger.exception("godview: failed to collect Live Store table bytes")
        return

    if not table_bytes.get("available"):
        return
    tables = table_bytes.get("tables")
    if not isinstance(tables, dict):
        return
    for table_name, payload in tables.items():
        if not isinstance(payload, dict):
            continue
        metrics.live_store_table_bytes.labels(table=str(table_name)).set(float(payload.get("bytes") or 0))


def _live_store_table_bytes_metrics_enabled() -> bool:
    return os.getenv(_LIVE_STORE_TABLE_BYTES_METRICS_ENV, "").strip().lower() in _TRUTHY_ENV


def _live_store_table_bytes_deadline_seconds() -> float:
    raw_value = os.getenv(_LIVE_STORE_TABLE_BYTES_DEADLINE_MS_ENV, "").strip()
    if not raw_value:
        return _LIVE_STORE_TABLE_BYTES_DEFAULT_DEADLINE_MS / 1000.0
    try:
        deadline_ms = int(raw_value)
    except ValueError:
        return _LIVE_STORE_TABLE_BYTES_DEFAULT_DEADLINE_MS / 1000.0
    return max(1, deadline_ms) / 1000.0


def _live_store_database_url() -> str:
    from zerg.config import get_settings_unchecked

    return get_settings_unchecked().live_database_url


def refresh_device_gauges() -> None:
    """Fan the latest heartbeat per device out into labeled gauges.

    Device cardinality is the number of user-owned machines (small). Age and
    offline state are best derived in PromQL from the last-heartbeat timestamp,
    but we also export the heartbeat's own offline flag for convenience.
    """
    try:
        from zerg.database import live_catalog_enabled

        if live_catalog_enabled():
            _refresh_live_device_gauges()
            return
        from zerg.database import get_session_factory
        from zerg.services.agent_heartbeat_health import load_machine_transport_health_map

        session_factory = get_session_factory()
        with session_factory() as db:
            summary_map = load_machine_transport_health_map(db)
    except Exception:
        logger.exception("godview: failed to load machine transport health")
        return

    for device_id, s in summary_map.items():
        device = device_id or "unknown"
        metrics.device_last_heartbeat_timestamp_seconds.labels(device=device).set(s.last_heartbeat_at.timestamp())
        if s.ship_latency_p50_ms_1h is not None:
            metrics.device_ship_latency_ms.labels(device=device, quantile="p50").set(float(s.ship_latency_p50_ms_1h))
        if s.ship_latency_p95_ms_1h is not None:
            metrics.device_ship_latency_ms.labels(device=device, quantile="p95").set(float(s.ship_latency_p95_ms_1h))
        metrics.device_spool_pending.labels(device=device).set(float(s.spool_pending))
        metrics.device_spool_dead.labels(device=device).set(float(s.spool_dead))
        metrics.device_consecutive_ship_failures.labels(device=device).set(float(s.consecutive_failures))
        metrics.device_parse_errors_1h.labels(device=device).set(float(s.parse_errors_1h))
        metrics.device_disk_free_bytes.labels(device=device).set(float(s.disk_free_bytes))
        metrics.device_reported_offline.labels(device=device).set(1.0 if s.is_offline else 0.0)
        archive = s.archive_repair or {}
        pending_bytes = float(archive.get("pending_bytes", 0) or 0)
        pending_ranges = float(archive.get("pending_ranges", 0) or 0)
        metrics.device_archive_backlog_pending_bytes.labels(device=device).set(pending_bytes)
        metrics.device_archive_backlog_pending_ranges.labels(device=device).set(pending_ranges)


def _refresh_live_device_gauges() -> None:
    """Refresh device gauges from bounded heartbeat stamps only."""

    import json
    from datetime import timezone

    from sqlalchemy import func
    from sqlalchemy import select

    from zerg.database import get_live_session_factory
    from zerg.models.live_store import LiveHeartbeatStamp

    factory = get_live_session_factory()
    if factory is None:
        return
    with factory() as db:
        latest_ids = select(func.max(LiveHeartbeatStamp.id)).group_by(LiveHeartbeatStamp.device_id)
        rows = db.query(LiveHeartbeatStamp).filter(LiveHeartbeatStamp.id.in_(latest_ids)).all()
    for row in rows:
        device = str(row.device_id or "unknown")
        received_at = row.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        metrics.device_last_heartbeat_timestamp_seconds.labels(device=device).set(received_at.timestamp())
        if row.ship_latency_p50_ms_1h is not None:
            metrics.device_ship_latency_ms.labels(device=device, quantile="p50").set(float(row.ship_latency_p50_ms_1h))
        if row.ship_latency_p95_ms_1h is not None:
            metrics.device_ship_latency_ms.labels(device=device, quantile="p95").set(float(row.ship_latency_p95_ms_1h))
        metrics.device_spool_pending.labels(device=device).set(float(row.spool_pending or 0))
        metrics.device_spool_dead.labels(device=device).set(float(row.spool_dead or 0))
        metrics.device_consecutive_ship_failures.labels(device=device).set(float(row.consecutive_failures or 0))
        metrics.device_parse_errors_1h.labels(device=device).set(float(row.parse_errors_1h or 0))
        metrics.device_disk_free_bytes.labels(device=device).set(float(row.disk_free_bytes or 0))
        metrics.device_reported_offline.labels(device=device).set(1.0 if row.is_offline else 0.0)
        try:
            raw = json.loads(row.raw_json or "{}")
        except (TypeError, ValueError):
            raw = {}
        archive = raw.get("archive_repair") if isinstance(raw, dict) else {}
        archive = archive if isinstance(archive, dict) else {}
        metrics.device_archive_backlog_pending_bytes.labels(device=device).set(float(archive.get("pending_bytes") or 0))
        metrics.device_archive_backlog_pending_ranges.labels(device=device).set(float(archive.get("pending_ranges") or 0))


def refresh_godview_gauges() -> None:
    """Refresh all god-view gauges. Safe to call at scrape time."""
    refresh_write_serializer_gauges()
    refresh_live_write_serializer_gauges()
    refresh_live_store_gauges()
    refresh_device_gauges()
