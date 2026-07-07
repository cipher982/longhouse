from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_pool_status
from zerg.database import make_engine
from zerg.routers import health as health_router


def _stub_build_identity(monkeypatch):
    monkeypatch.setattr(
        "zerg.build_info.load",
        lambda: SimpleNamespace(as_dict=lambda: {"commit": "test"}),
    )


def test_pool_status_reports_exhausted_queue_pool(tmp_path):
    engine = make_engine(
        f"sqlite:///{tmp_path}/pool_status.db",
        pool_size=1,
        max_overflow=0,
    )

    with engine.connect():
        status = get_pool_status(engine)

    assert status is not None
    assert status["pool_class"] == "QueuePool"
    assert status["size"] == 1
    assert status["checked_out"] == 1
    assert status["checked_in"] == 0
    assert status["max_overflow"] == 0
    assert status["saturated"] is True
    assert status["total_checkouts"] >= 1
    assert status["current_max_hold_ms"] >= 0.0

    released_status = get_pool_status(engine)
    assert released_status is not None
    assert released_status["completed_checkouts"] >= 1
    assert released_status["max_hold_ms"] >= 0.0


def test_health_reports_saturated_writer_without_entering_writer_lane(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/health_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class SaturatedWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 999,
                "writer_active": True,
                "active_label": "ingest-replay",
                "active_age_ms": 30_000.0,
            }

        async def execute(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

        async def execute_after_closing_request_session(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("health must not enter the serialized writer lane")

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: SaturatedWriter())
    monkeypatch.setattr(
        health_router,
        "_session_projection_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )
    monkeypatch.setattr(
        health_router,
        "_session_enrichment_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )

    payload = health_router.health_check(
        SimpleNamespace(
            client=SimpleNamespace(host="testclient"),
            headers={},
        )
    )

    assert payload["checks"]["write_serializer"]["status"] == "pass"
    assert payload["checks"]["write_serializer"]["queue_depth"] == 999
    assert payload["checks"]["write_serializer"]["writer_active"] is True
    assert payload["checks"]["db_pool"]["status"] == "pass"
    assert payload["checks"]["db_pool"]["pool_class"] == "QueuePool"
    assert payload["checks"]["db_pool"]["checked_out"] == 0
    assert payload["checks"]["db_pool"]["saturated"] is False
    assert payload["checks"]["db_pool"]["total_checkouts"] >= 2


def test_health_reports_archive_degraded_for_stale_active_writer_with_queued_work(tmp_path, monkeypatch):
    _stub_build_identity(monkeypatch)
    engine = make_engine(f"sqlite:///{tmp_path}/health_stale_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class StaleWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 38,
                "writer_active": True,
                "active_label": "ingest-scan",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: StaleWriter())
    monkeypatch.setattr(
        health_router,
        "_session_projection_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )
    monkeypatch.setattr(
        health_router,
        "_session_enrichment_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )

    response = health_router.health_check(
        SimpleNamespace(
            client=SimpleNamespace(host="testclient"),
            headers={},
        )
    )

    assert response["status"] == "degraded"
    assert response["message"] == "Archive write serializer is stalled; live lane may remain available"
    assert response["checks"]["write_serializer"]["status"] == "warn"
    assert response["checks"]["write_serializer"]["archive_degraded"] is True
    assert response["checks"]["write_serializer"]["active_label"] == "ingest-scan"


def test_readyz_reports_archive_degraded_for_stale_active_writer_with_queued_work(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/readyz_stale_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class StaleWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 38,
                "writer_active": True,
                "active_label": "ingest-scan",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: StaleWriter())

    response = health_router.readyz_check()

    assert response["status"] == "ready_with_archive_degraded"
    assert response["reason"] == "archive_write_serializer_stalled"
    assert response["write_serializer"]["status"] == "warn"
    assert response["write_serializer"]["archive_degraded"] is True


def test_readyz_fails_stale_non_archive_writer_with_queued_work(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/readyz_stale_non_archive_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class StaleWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 38,
                "writer_active": True,
                "active_label": "device-token-create",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: StaleWriter())

    response = health_router.readyz_check()

    assert response.status_code == 503
    assert b"write_serializer_stalled" in response.body


def test_health_fails_stale_non_archive_writer_with_queued_work(tmp_path, monkeypatch):
    _stub_build_identity(monkeypatch)
    engine = make_engine(f"sqlite:///{tmp_path}/health_stale_non_archive_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class StaleWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 38,
                "writer_active": True,
                "active_label": "device-token-create",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: StaleWriter())
    monkeypatch.setattr(
        health_router,
        "_session_projection_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )
    monkeypatch.setattr(
        health_router,
        "_session_enrichment_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )

    response = health_router.health_check(
        SimpleNamespace(
            client=SimpleNamespace(host="testclient"),
            headers={},
        )
    )

    assert response.status_code == 503
    assert b"Write serializer is stalled" in response.body


def test_health_fails_stale_live_writer_with_queued_work(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/health_stale_live_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class ArchiveWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 0,
                "writer_active": False,
                "active_label": None,
                "active_age_ms": 0.0,
            }

    class StaleLiveWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 12,
                "writer_active": True,
                "active_label": "heartbeat-stamp",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: ArchiveWriter())
    monkeypatch.setattr("zerg.services.write_serializer.get_live_write_serializer", lambda: StaleLiveWriter())
    monkeypatch.setattr(
        health_router,
        "_session_projection_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )
    monkeypatch.setattr(
        health_router,
        "_session_enrichment_lag_check",
        lambda: {"status": "pass", "pending_sessions": 0},
    )

    response = health_router.health_check(
        SimpleNamespace(
            client=SimpleNamespace(host="testclient"),
            headers={},
        )
    )

    assert response.status_code == 503
    assert b"Live write serializer is stalled" in response.body


def test_readyz_fails_stale_live_writer_with_queued_work(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path}/readyz_stale_live_writer.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE events_fts USING fts5(content_text)"))

    class ArchiveWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 0,
                "writer_active": False,
                "active_label": None,
                "active_age_ms": 0.0,
            }

    class StaleLiveWriter:
        is_configured = True

        def get_metrics(self):
            return {
                "queue_depth": 12,
                "writer_active": True,
                "active_label": "heartbeat-stamp",
                "active_age_ms": health_router._write_serializer_stale_active_ms() + 1,
            }

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr("zerg.services.write_serializer.get_write_serializer", lambda: ArchiveWriter())
    monkeypatch.setattr("zerg.services.write_serializer.get_live_write_serializer", lambda: StaleLiveWriter())

    response = health_router.readyz_check()

    assert response.status_code == 503
    assert b"live_write_serializer_stalled" in response.body
