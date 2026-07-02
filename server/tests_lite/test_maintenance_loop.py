from __future__ import annotations

import os
from datetime import datetime
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
        async def execute(self, fn, *, auto_commit, label):
            calls.append((label, auto_commit))
            with ArchiveSession() as archive_db:
                return fn(archive_db)

    monkeypatch.setattr("zerg.database.live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.database.get_live_session_factory", lambda: LiveSession)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: FakeSerializer())

    try:
        result = await _drain_live_archive_outbox_once()

        assert result == {"processed": 1, "drained": 1, "failed": 0}
        assert calls == [("live-archive-drain", False)]
        with ArchiveSession() as archive_db:
            row = archive_db.query(AgentHeartbeat).filter(AgentHeartbeat.device_id == "maintenance-drain").one()
            assert row.spool_pending == 4
        with LiveSession() as live_db:
            outbox = live_db.query(LiveArchiveOutbox).one()
            assert outbox.drained_at is not None
    finally:
        archive_engine.dispose()
        live_engine.dispose()
