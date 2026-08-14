"""The catalogd writer must be able to say who held it and for how long.

Before this existed, a write that waited a second behind bulk ingest was
indistinguishable from an unreachable host -- which is exactly how one was
misdiagnosed as the other. These tests pin the properties that make that
diagnosis possible, not the exact numbers.
"""

from __future__ import annotations

import time

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.server import CatalogWriterStats
from zerg.catalogd.store import CatalogStore


def test_writer_stats_separate_queue_wait_from_execution() -> None:
    """Total time hides the half that matters.

    A caller blocked for a second behind someone else's write and a caller whose
    own write took a second are different defects with different fixes.
    """

    stats = CatalogWriterStats()
    stats.record("commit_raw_object", queue_wait_ms=0.0, exec_ms=900.0)
    stats.record("create_local_launch", queue_wait_ms=880.0, exec_ms=6.0)

    snapshot = stats.snapshot()
    ingest = snapshot["labels"]["commit_raw_object"]
    launch = snapshot["labels"]["create_local_launch"]

    assert ingest["exec_ms"]["p99"] >= 900.0
    assert ingest["queue_wait_ms"]["p99"] == 0.0
    # The launch is the victim, not the cause: cheap to execute, expensive to wait.
    assert launch["exec_ms"]["p99"] <= 10.0
    assert launch["queue_wait_ms"]["p99"] >= 880.0


def test_writer_stats_name_the_operation_holding_the_writer() -> None:
    stats = CatalogWriterStats()
    stats.record_enqueue()
    stats.mark_active("commit_raw_object", 0.0)

    active = stats.snapshot()
    assert active["active_label"] == "commit_raw_object"
    assert active["depth"] == 1

    stats.record("commit_raw_object", 0.0, 5.0)
    stats.record_dequeue()
    settled = stats.snapshot()
    assert settled["active_label"] is None
    assert settled["depth"] == 0
    # Peak depth survives so a burst is still visible after it drains.
    assert settled["peak_depth"] == 1


def test_writer_stats_window_is_bounded() -> None:
    """A daemon runs for weeks; the histogram must not grow with it."""

    stats = CatalogWriterStats()
    for i in range(5_000):
        stats.record("heartbeat", queue_wait_ms=float(i), exec_ms=1.0)

    snapshot = stats.snapshot()
    assert snapshot["labels"]["heartbeat"]["n"] == 5_000
    # Percentiles come from the recent window, so they track current behaviour
    # rather than averaging away a regression with ancient samples.
    assert snapshot["labels"]["heartbeat"]["queue_wait_ms"]["p50"] > 4_000


def test_catalog_engine_sets_cache_and_mmap(tmp_path) -> None:
    """A default 2 MB page cache against a multi-GB catalog was the old state."""

    engine = create_catalog_engine(str(tmp_path / "catalog.db"))
    try:
        with engine.connect() as connection:
            cache_size = connection.exec_driver_sql("PRAGMA cache_size").scalar()
            mmap_size = connection.exec_driver_sql("PRAGMA mmap_size").scalar()
            temp_store = connection.exec_driver_sql("PRAGMA temp_store").scalar()
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
    finally:
        engine.dispose()

    # Negative means KiB. The default is -2000; anything at or above that is the
    # bug this replaced.
    assert cache_size < -2000
    assert mmap_size > 0
    assert temp_store == 2  # MEMORY
    # The durability posture must not have changed while tuning throughput.
    assert journal_mode.lower() == "wal"
    assert synchronous == 1  # NORMAL


def test_truncate_checkpoint_reclaims_the_wal_file(tmp_path) -> None:
    """PASSIVE reuses WAL space but never shrinks the file.

    That is why the dogfood tenant carried a 692 MB WAL long after the burst
    that created it.
    """

    database = tmp_path / "catalog.db"
    engine = create_catalog_engine(str(database))
    store = CatalogStore(engine)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE blob_rows (id INTEGER PRIMARY KEY, payload TEXT)")
            connection.commit()
            for _ in range(200):
                connection.exec_driver_sql("INSERT INTO blob_rows (payload) VALUES (?)", ("x" * 4096,))
            connection.commit()

        wal = database.with_name(database.name + "-wal")
        grown = wal.stat().st_size
        assert grown > 0

        passive = store.checkpoint_passive()
        assert passive["busy"] == 0
        assert wal.stat().st_size == grown, "PASSIVE is not expected to shrink the file"

        truncated = store.checkpoint_truncate()
        assert truncated["busy"] == 0
        assert wal.stat().st_size < grown
    finally:
        engine.dispose()


def test_checkpoint_reports_its_own_cost(tmp_path) -> None:
    """The result used to be discarded, making starvation unobservable."""

    engine = create_catalog_engine(str(tmp_path / "catalog.db"))
    store = CatalogStore(engine)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            connection.commit()
        result = store.checkpoint_passive()
    finally:
        engine.dispose()

    assert set(result) == {"busy", "log_frames", "checkpointed_frames"}
    assert all(isinstance(value, int) for value in result.values())


def test_stats_snapshot_is_cheap_enough_to_log_periodically() -> None:
    stats = CatalogWriterStats()
    for i in range(2_000):
        stats.record(f"label_{i % 25}", queue_wait_ms=1.0, exec_ms=2.0)

    started = time.perf_counter()
    snapshot = stats.snapshot()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert len(snapshot["labels"]) == 25
    assert elapsed_ms < 50.0
