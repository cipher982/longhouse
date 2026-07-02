"""Tests for god-view Prometheus gauge refresh.

These assert the refresh helpers mirror current operational state into the
gauges the observability stack scrapes. They skip cleanly when
``prometheus_client`` is not installed (the gauges are no-ops there).
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

prometheus_client = pytest.importorskip("prometheus_client")

from zerg import metrics  # noqa: E402
from zerg.database import Base  # noqa: E402
from zerg.database import initialize_live_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_live_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.models.agents import AgentHeartbeat  # noqa: E402
from zerg.models.live_store import LiveArchiveOutbox  # noqa: E402
from zerg.models.live_store import LiveSession as LiveSessionRow  # noqa: E402


def _gauge_value(gauge, **labels) -> float | None:
    """Read a gauge child value from the prometheus registry."""
    for metric in gauge.collect():
        for sample in metric.samples:
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


def _make_db(tmp_path):
    db_path = tmp_path / "test_godview_metrics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_device_gauges_reflect_latest_heartbeat(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    pinned_now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    import zerg.services.agent_heartbeat_health as health_service

    monkeypatch.setattr(health_service, "utc_now", lambda: pinned_now)

    with SessionLocal() as db:
        # Older row should be ignored in favor of the latest per device.
        db.add(
            AgentHeartbeat(
                device_id="dev-1",
                received_at=pinned_now - timedelta(minutes=10),
                version="0.1.0",
                spool_pending=99,
                spool_dead=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                ship_attempts_1h=1,
                ship_successes_1h=1,
                disk_free_bytes=1,
                is_offline=0,
            )
        )
        db.add(
            AgentHeartbeat(
                device_id="dev-1",
                received_at=pinned_now - timedelta(minutes=1),
                version="0.2.0",
                last_ship_latency_ms=200,
                spool_pending=4,
                spool_dead=2,
                parse_errors_1h=3,
                consecutive_failures=1,
                ship_attempts_1h=5,
                ship_successes_1h=4,
                ship_latency_p50_ms_1h=120,
                ship_latency_p95_ms_1h=240,
                disk_free_bytes=4096,
                is_offline=0,
            )
        )
        db.commit()

    import zerg.services.godview_metrics as godview
    from zerg.database import get_session_factory

    # Route the helper's session factory at our test DB.
    monkeypatch.setattr(godview, "get_session_factory", get_session_factory, raising=False)
    monkeypatch.setattr("zerg.database.get_session_factory", lambda: SessionLocal)

    godview.refresh_device_gauges()

    assert _gauge_value(metrics.device_spool_pending, device="dev-1") == 4.0
    assert _gauge_value(metrics.device_spool_dead, device="dev-1") == 2.0
    assert _gauge_value(metrics.device_consecutive_ship_failures, device="dev-1") == 1.0
    assert _gauge_value(metrics.device_parse_errors_1h, device="dev-1") == 3.0
    assert _gauge_value(metrics.device_disk_free_bytes, device="dev-1") == 4096.0
    assert _gauge_value(metrics.device_reported_offline, device="dev-1") == 0.0
    assert _gauge_value(metrics.device_ship_latency_ms, device="dev-1", quantile="p95") == 240.0
    ts = _gauge_value(metrics.device_last_heartbeat_timestamp_seconds, device="dev-1")
    assert ts == pytest.approx((pinned_now - timedelta(minutes=1)).timestamp())


@pytest.mark.asyncio
async def test_write_serializer_gauges_reflect_metrics(tmp_path, monkeypatch):
    from zerg.database import make_engine as _make_engine
    from zerg.database import make_sessionmaker
    from zerg.services.write_serializer import WriteSerializer

    db_path = tmp_path / "ws.db"
    engine = _make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _write(db):
        db.execute(__import__("sqlalchemy").text("INSERT INTO writes (label) VALUES ('x')"))
        db.commit()

    await serializer.execute(_write, label="ingest-live")

    import zerg.services.godview_metrics as godview

    monkeypatch.setattr(godview, "get_write_serializer", lambda: serializer, raising=False)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: serializer)

    godview.refresh_write_serializer_gauges()

    # At least one write recorded; queue should be drained (depth 0) after await.
    assert _gauge_value(metrics.write_serializer_queue_depth) == 0.0
    # p50 exec gauge for the label should be populated (>= 0).
    p50 = _gauge_value(metrics.write_serializer_exec_ms, label="ingest-live", quantile="p50")
    assert p50 is not None and p50 >= 0.0


@pytest.mark.asyncio
async def test_live_write_serializer_gauges_reflect_metrics(tmp_path, monkeypatch):
    from zerg.database import make_engine as _make_engine
    from zerg.services.write_serializer import WriteSerializer

    db_path = tmp_path / "live-ws.db"
    engine = _make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE live_writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _write(db):
        db.execute(__import__("sqlalchemy").text("INSERT INTO live_writes (label) VALUES ('x')"))
        db.commit()

    await serializer.execute(_write, label="heartbeat-live")

    import zerg.services.godview_metrics as godview

    monkeypatch.setattr("zerg.services.write_serializer.get_live_write_serializer", lambda: serializer)
    monkeypatch.setattr("zerg.database.get_live_wal_bytes", lambda: 123)

    godview.refresh_live_write_serializer_gauges()

    assert _gauge_value(metrics.live_write_serializer_queue_depth) == 0.0
    assert _gauge_value(metrics.live_sqlite_wal_bytes) == 123.0
    p50 = _gauge_value(metrics.live_write_serializer_exec_ms, label="heartbeat-live", quantile="p50")
    assert p50 is not None and p50 >= 0.0


def test_live_store_gauges_reflect_outbox_and_skip_table_bytes_by_default(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live-godview.db")
    initialize_live_database(live_engine)
    LiveSession = make_sessionmaker(live_engine)
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    with LiveSession() as live_db:
        live_db.add(
            LiveArchiveOutbox(
                idempotency_key="pending-1",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                created_at=now - timedelta(seconds=30),
                attempts=2,
                last_error="boom",
            )
        )
        live_db.add(
            LiveArchiveOutbox(
                idempotency_key="drained-1",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                created_at=now - timedelta(seconds=60),
                drained_at=now - timedelta(seconds=5),
                attempts=1,
            )
        )
        live_db.add(
            LiveSessionRow(
                session_id="11111111-1111-1111-1111-111111111111",
                provider="codex",
                device_id="cinder",
                state="attached",
                started_at=now - timedelta(minutes=1),
                last_seen_at=now,
                updated_at=now,
            )
        )
        live_db.commit()

    import zerg.services.godview_metrics as godview

    def fail_table_bytes_url():
        raise AssertionError("default live-store gauges must not walk table bytes")

    monkeypatch.delenv("LONGHOUSE_LIVE_STORE_TABLE_BYTES_METRICS", raising=False)
    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr(godview, "datetime", _PinnedDateTime)
    monkeypatch.setattr(godview, "_live_store_database_url", fail_table_bytes_url)
    _PinnedDateTime.pinned_now = now

    try:
        godview.refresh_live_store_gauges()
    finally:
        live_engine.dispose()

    assert _gauge_value(metrics.live_archive_outbox_pending) == 1.0
    assert _gauge_value(metrics.live_archive_outbox_failed) == 1.0
    assert _gauge_value(metrics.live_archive_outbox_max_attempts) == 2.0
    assert _gauge_value(metrics.live_archive_outbox_oldest_pending_age_seconds) == pytest.approx(30.0)
    assert _gauge_value(metrics.live_archive_outbox_last_drained_age_seconds) == pytest.approx(5.0)


def test_live_store_table_bytes_gauge_is_opt_in_and_deadline_bounded(tmp_path, monkeypatch):
    live_db_path = tmp_path / "live-table-bytes.db"
    live_engine = make_live_engine(f"sqlite:///{live_db_path}")
    initialize_live_database(live_engine)

    import zerg.services.db_diagnostics as db_diagnostics
    import zerg.services.godview_metrics as godview

    calls = {}

    def fake_table_bytes(conn, *, deadline_monotonic, progress_opcodes=100_000):
        calls["deadline_monotonic"] = deadline_monotonic
        calls["progress_opcodes"] = progress_opcodes
        return {
            "available": True,
            "error": None,
            "total_bytes": 4096,
            "total_pages": 1,
            "tables": {"live_sessions": {"bytes": 4096}},
        }

    monkeypatch.setenv("LONGHOUSE_LIVE_STORE_TABLE_BYTES_METRICS", "1")
    monkeypatch.setenv("LONGHOUSE_LIVE_STORE_TABLE_BYTES_DEADLINE_MS", "25")
    monkeypatch.setattr(godview, "_live_store_database_url", lambda: f"sqlite:///{live_db_path}")
    monkeypatch.setattr(db_diagnostics, "collect_sqlite_table_bytes_with_deadline", fake_table_bytes)

    try:
        started = godview.time.monotonic()
        godview._refresh_live_store_table_bytes_gauges(None)
    finally:
        live_engine.dispose()

    assert calls["deadline_monotonic"] >= started
    assert calls["deadline_monotonic"] <= started + 0.25
    assert _gauge_value(metrics.live_store_table_bytes, table="live_sessions") == 4096.0


def test_live_store_gauges_noop_when_not_configured(monkeypatch):
    import zerg.services.godview_metrics as godview

    def fail_factory():
        raise AssertionError("disabled live store must not open a DB")

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: False)
    monkeypatch.setattr("zerg.database.get_live_session_factory", fail_factory)

    godview.refresh_live_store_gauges()


class _PinnedDateTime(datetime):
    pinned_now: datetime

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - test helper
        if tz is None:
            return cls.pinned_now.replace(tzinfo=None)
        return cls.pinned_now.astimezone(tz)
