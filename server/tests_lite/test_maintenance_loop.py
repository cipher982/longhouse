from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.live_archive_outbox import enqueue_heartbeat_stamp_outbox
from zerg.services.maintenance import _drain_live_archive_outbox_once
from zerg.services.write_serializer import WriteQueueTimeoutError


@pytest.mark.asyncio
async def test_live_archive_drain_uses_archive_writer_lane(tmp_path, monkeypatch):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    now = datetime.now(timezone.utc)
    with LiveSession() as live_db:
        enqueue_heartbeat_stamp_outbox(
            live_db,
            {
                "device_id": "maintenance-drain",
                "received_at": now,
                "version": "0.5.0",
                "spool_pending": 4,
                "spool_dead": 0,
                "parse_errors_1h": 0,
                "consecutive_failures": 0,
                "ship_attempts_1h": 1,
                "ship_successes_1h": 1,
                "ship_rate_limited_1h": 0,
                "ship_server_errors_1h": 0,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "disk_free_bytes": 1,
                "is_offline": 0,
                "raw_json": "{}",
            },
        )
        live_db.commit()

    calls = []

    class FakeSerializer:
        async def execute(self, fn, **kwargs):
            calls.append(kwargs)
            with ArchiveSession() as archive_db:
                return fn(archive_db)

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: FakeSerializer())

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 1, "drained": 1, "failed": 0, "cleaned": 0}
        assert calls[0]["label"] == "live-archive-drain"
        assert calls[0]["auto_commit"] is False
        assert "timeout_seconds" in calls[0]
        assert "queue_timeout_seconds" in calls[0]
        with ArchiveSession() as archive_db:
            row = archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "maintenance-drain").one()
            assert row.spool_pending == 4
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.drained_at is not None
    finally:
        archive_engine.dispose()
        live_engine.dispose()


@pytest.mark.asyncio
async def test_live_archive_drain_passes_bounded_writer_timeouts(tmp_path, monkeypatch):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    with LiveSession() as live_db:
        enqueue_heartbeat_stamp_outbox(
            live_db,
            {
                "device_id": "maintenance-timeouts",
                "received_at": datetime.now(timezone.utc),
                "version": "0.5.0",
                "spool_pending": 0,
                "spool_dead": 0,
                "parse_errors_1h": 0,
                "consecutive_failures": 0,
                "ship_attempts_1h": 1,
                "ship_successes_1h": 1,
                "ship_rate_limited_1h": 0,
                "ship_server_errors_1h": 0,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "disk_free_bytes": 1,
                "is_offline": 0,
                "raw_json": "{}",
            },
        )
        live_db.commit()

    calls = []

    class FakeSerializer:
        async def execute(self, fn, **kwargs):
            calls.append(kwargs)
            with ArchiveSession() as archive_db:
                return fn(archive_db)

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: FakeSerializer())
    monkeypatch.setattr("zerg.services.maintenance.LIVE_ARCHIVE_OUTBOX_DRAIN_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr("zerg.services.maintenance.LIVE_ARCHIVE_OUTBOX_DRAIN_QUEUE_TIMEOUT_SECONDS", 0.5)

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 1, "drained": 1, "failed": 0, "cleaned": 0}
        assert calls[0]["label"] == "live-archive-drain"
        assert calls[0]["auto_commit"] is False
        assert calls[0]["timeout_seconds"] == 4.0
        assert calls[0]["queue_timeout_seconds"] == 0.5
    finally:
        archive_engine.dispose()
        live_engine.dispose()


@pytest.mark.asyncio
async def test_live_archive_drain_timeout_defers_pending_rows(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    with LiveSession() as live_db:
        live_db.add(
            LiveArchiveOutbox(
                idempotency_key="old-drained-on-timeout",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                drained_at=datetime.now(timezone.utc) - timedelta(days=8),
            )
        )
        enqueue_heartbeat_stamp_outbox(
            live_db,
            {
                "device_id": "maintenance-timeout",
                "received_at": datetime.now(timezone.utc),
                "version": "0.5.0",
                "spool_pending": 0,
                "spool_dead": 0,
                "parse_errors_1h": 0,
                "consecutive_failures": 0,
                "ship_attempts_1h": 1,
                "ship_successes_1h": 1,
                "ship_rate_limited_1h": 0,
                "ship_server_errors_1h": 0,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "disk_free_bytes": 1,
                "is_offline": 0,
                "raw_json": "{}",
            },
        )
        live_db.commit()

    class TimeoutSerializer:
        async def execute(self, *_args, **_kwargs):
            raise TimeoutError("archive writer saturated")

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: TimeoutSerializer())

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 0, "drained": 0, "failed": 0, "cleaned": 1, "deferred": 1}
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.drained_at.is_(None)).one()
            assert outbox.drained_at is None
            assert outbox.attempts == 0
    finally:
        live_engine.dispose()


@pytest.mark.asyncio
async def test_live_archive_drain_queue_timeout_defers_pending_rows(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live_queue_timeout.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    with LiveSession() as live_db:
        enqueue_heartbeat_stamp_outbox(
            live_db,
            {
                "device_id": "maintenance-queue-timeout",
                "received_at": datetime.now(timezone.utc),
                "version": "0.5.0",
                "spool_pending": 0,
                "spool_dead": 0,
                "parse_errors_1h": 0,
                "consecutive_failures": 0,
                "ship_attempts_1h": 1,
                "ship_successes_1h": 1,
                "ship_rate_limited_1h": 0,
                "ship_server_errors_1h": 0,
                "ship_payload_rejections_1h": 0,
                "ship_payload_too_large_1h": 0,
                "ship_retryable_client_errors_1h": 0,
                "ship_connect_errors_1h": 0,
                "disk_free_bytes": 1,
                "is_offline": 0,
                "raw_json": "{}",
            },
        )
        live_db.commit()

    class QueueTimeoutSerializer:
        async def execute(self, *_args, **kwargs):
            raise WriteQueueTimeoutError(
                label=str(kwargs.get("label") or ""),
                queue_timeout_seconds=float(kwargs.get("queue_timeout_seconds") or 2.0),
            )

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: QueueTimeoutSerializer())

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 0, "drained": 0, "failed": 0, "cleaned": 0, "deferred": 1}
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.drained_at is None
            assert outbox.attempts == 0
    finally:
        live_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_batches", "expected_processed", "expected_pending"),
    [
        (3, 5, 0),
        (2, 4, 1),
    ],
)
async def test_live_archive_drain_catches_up_multiple_batches_per_tick(
    tmp_path,
    monkeypatch,
    max_batches,
    expected_processed,
    expected_pending,
):
    archive_engine = make_engine(f"sqlite:///{tmp_path}/archive.db")
    Base.metadata.create_all(bind=archive_engine)
    ArchiveSession = sessionmaker(bind=archive_engine)

    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    now = datetime.now(timezone.utc)
    heartbeat = {
        "device_id": "maintenance-catchup",
        "received_at": now,
        "version": "0.5.0",
        "spool_pending": 4,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "disk_free_bytes": 1,
        "is_offline": 0,
        "raw_json": "{}",
    }
    with LiveSession() as live_db:
        for index in range(5):
            enqueue_heartbeat_stamp_outbox(
                live_db,
                {
                    **heartbeat,
                    "received_at": now + timedelta(milliseconds=index),
                    "sessions_sequence": index,
                },
            )
        live_db.commit()

    calls = []

    class FakeSerializer:
        async def execute(self, fn, **kwargs):
            calls.append(kwargs)
            with ArchiveSession() as archive_db:
                return fn(archive_db)

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: FakeSerializer())
    monkeypatch.setattr("zerg.services.maintenance.LIVE_ARCHIVE_OUTBOX_DRAIN_BATCH_SIZE", 2)
    monkeypatch.setattr("zerg.services.maintenance.LIVE_ARCHIVE_OUTBOX_DRAIN_MAX_BATCHES_PER_TICK", max_batches)

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": expected_processed, "drained": expected_processed, "failed": 0, "cleaned": 0}
        assert calls[0]["label"] == "live-archive-drain"
        assert calls[0]["auto_commit"] is False
        with ArchiveSession() as archive_db:
            assert archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "maintenance-catchup").count() == expected_processed
        with LiveSession() as live_db:
            rows = live_db.query(LiveArchiveOutbox).all()
            assert len(rows) == 5
            drained_count = sum(1 for row in rows if row.drained_at is not None)
            assert drained_count == expected_processed
            assert len(rows) - drained_count == expected_pending
    finally:
        archive_engine.dispose()
        live_engine.dispose()


@pytest.mark.asyncio
async def test_live_archive_drain_cleans_old_drained_rows_without_archive_writer(tmp_path, monkeypatch):
    live_engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)

    now = datetime.now(timezone.utc)
    with LiveSession() as live_db:
        live_db.add(
            LiveArchiveOutbox(
                idempotency_key="old-drained",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                drained_at=now - timedelta(days=8),
            )
        )
        live_db.add(
            LiveArchiveOutbox(
                idempotency_key="recent-drained",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                drained_at=now - timedelta(days=1),
            )
        )
        live_db.commit()

    class FakeSerializer:
        async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("cleanup-only tick must not use archive writer")

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: FakeSerializer())

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 0, "drained": 0, "failed": 0, "cleaned": 1}
        with LiveSession() as live_db:
            rows = {row.idempotency_key for row in live_db.query(LiveArchiveOutbox).all()}
            assert rows == {"recent-drained"}
    finally:
        live_engine.dispose()
