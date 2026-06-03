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

from zerg import metrics

logger = logging.getLogger(__name__)

_QUANTILE_KEYS = ("p50", "p95", "p99")


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


def refresh_device_gauges() -> None:
    """Fan the latest heartbeat per device out into labeled gauges.

    Device cardinality is the number of user-owned machines (small). Age and
    offline state are best derived in PromQL from the last-heartbeat timestamp,
    but we also export the heartbeat's own offline flag for convenience.
    """
    try:
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


def refresh_godview_gauges() -> None:
    """Refresh all god-view gauges. Safe to call at scrape time."""
    refresh_write_serializer_gauges()
    refresh_device_gauges()
