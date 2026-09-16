"""Focused contract coverage for catalogd writer telemetry."""

from __future__ import annotations

from zerg.catalogd.server import CatalogWriterStats


def test_writer_stats_exports_bounded_series_and_cumulative_cost() -> None:
    stats = CatalogWriterStats()
    stats.record("ingest", queue_wait_ms=3.0, exec_ms=8.0)
    stats.record("ingest", queue_wait_ms=5.0, exec_ms=12.0)

    label = stats.snapshot()["labels"]["ingest"]

    assert label["n"] == 2
    assert label["total_exec_ms"] == 20.0
    assert label["queue_wait_ms"]["p95"] == 5.0
    assert label["exec_ms"]["p99"] == 12.0


def test_writer_stats_does_not_grow_histograms_with_daemon_lifetime() -> None:
    stats = CatalogWriterStats()
    for _ in range(1_000):
        stats.record("heartbeat", queue_wait_ms=1.0, exec_ms=2.0)

    label = stats.snapshot()["labels"]["heartbeat"]

    assert label["n"] == 1_000
    assert label["total_exec_ms"] == 2_000.0
    assert label["exec_ms"]["p50"] == 2.0
