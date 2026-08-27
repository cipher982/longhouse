from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_pool_status
from zerg.database import make_engine
from zerg.routers import health as health_router


def _trusted_request() -> SimpleNamespace:
    """A caller /health treats as trusted.

    The TestClient host is no longer trusted on its own, so verbose-check
    assertions must present the internal token like any other operator caller.
    """
    from zerg.config import get_settings

    return SimpleNamespace(
        client=SimpleNamespace(host="testclient"),
        headers={"X-Internal-Token": get_settings().internal_api_secret},
    )


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


def test_health_reports_sqlite_wal_checkpoint_metrics(monkeypatch):
    _stub_build_identity(monkeypatch)

    import zerg.database as database_module

    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 123)
    monkeypatch.setattr(database_module, "get_live_wal_bytes", lambda: 45)
    monkeypatch.setattr(
        database_module,
        "get_wal_checkpoint_metrics",
        lambda: {
            "archive": {
                "label": "archive",
                "skipped": False,
                "busy": 0,
                "log_frames": 7,
                "checkpointed_frames": 7,
                "remaining_frames": 0,
                "checked_at_unix": 1.0,
            }
        },
    )

    payload = health_router.health_check(_trusted_request())

    sqlite_wal = payload["checks"]["sqlite_wal"]
    assert sqlite_wal["wal_bytes"] == 123
    assert sqlite_wal["live_wal_bytes"] == 45
    assert sqlite_wal["checkpoints"]["archive"]["log_frames"] == 7
    assert sqlite_wal["checkpoints"]["archive"]["remaining_frames"] == 0


def test_readyz_requires_catalogd_in_live_catalog_production(tmp_path, monkeypatch):
    from types import SimpleNamespace

    engine = make_engine(f"sqlite:///{tmp_path}/readyz_catalogd.db")
    import zerg.database as database_module

    monkeypatch.setattr(health_router, "get_settings", lambda: SimpleNamespace(testing=False))
    monkeypatch.setattr(database_module, "get_live_engine", lambda: engine)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(
        "zerg.catalogd.client.call_catalogd_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("down")),
    )
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.catalogd_paths",
        lambda: (tmp_path / "live.db", tmp_path / "catalogd.sock"),
    )

    response = health_router.readyz_check()

    assert response.status_code == 503
    assert b"catalog_unavailable" in response.body


def test_readyz_probes_catalogd_for_factory_assurance_test_runtime(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import zerg.database as database_module

    observed = {}

    def ping(*_args, **kwargs):
        observed.update(kwargs)
        return {"ready": True}

    monkeypatch.setattr(health_router, "get_settings", lambda: SimpleNamespace(testing=True))
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(
        "zerg.services.factory_assurance_title_binding.factory_assurance_title_enabled",
        lambda: True,
    )
    monkeypatch.setattr("zerg.catalogd.client.call_catalogd_sync", ping)
    monkeypatch.setattr("zerg.catalogd.schema.catalogd_ping_is_compatible", lambda payload: payload["ready"])
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.catalogd_paths",
        lambda: (tmp_path / "live.db", tmp_path / "catalogd.sock"),
    )

    response = health_router.readyz_check()

    assert response == {"status": "ok"}
    assert observed["timeout_seconds"] == health_router.CATALOG_HEALTH_TIMEOUT_SECONDS


def test_readyz_catalog_probe_has_bounded_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    engine = make_engine(f"sqlite:///{tmp_path}/readyz_catalogd_budget.db")
    import zerg.database as database_module

    observed = {}

    def unavailable(*_args, **kwargs):
        observed.update(kwargs)
        raise ConnectionError("down")

    monkeypatch.setattr(health_router, "get_settings", lambda: SimpleNamespace(testing=False))
    monkeypatch.setattr(database_module, "get_live_engine", lambda: engine)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr("zerg.catalogd.client.call_catalogd_sync", unavailable)
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.catalogd_paths",
        lambda: (tmp_path / "live.db", tmp_path / "catalogd.sock"),
    )

    response = health_router.readyz_check()

    assert response.status_code == 503
    assert observed["timeout_seconds"] == health_router.CATALOG_HEALTH_TIMEOUT_SECONDS


def test_health_reports_archive_wal_pressure_as_degraded(monkeypatch):
    _stub_build_identity(monkeypatch)

    import zerg.database as database_module

    monkeypatch.setenv("LONGHOUSE_ARCHIVE_INGEST_WAL_SHED_BYTES", "100")
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 100)
    monkeypatch.setattr(database_module, "get_live_wal_bytes", lambda: 12)
    monkeypatch.setattr(database_module, "get_wal_checkpoint_metrics", lambda: {})

    response = health_router.health_check(_trusted_request())

    assert response["status"] == "degraded"
    assert response["message"] == "Archive WAL pressure is shedding archive ingest; live lane may remain available"
    assert response["checks"]["sqlite_wal"]["status"] == "warn"
    assert response["checks"]["sqlite_wal"]["archive_degraded"] is True
    assert response["checks"]["sqlite_wal"]["shed"] is True
    assert response["checks"]["sqlite_wal"]["wal_bytes"] == 100
    assert response["checks"]["sqlite_wal"]["threshold_bytes"] == 100
    assert response["checks"]["sqlite_wal"]["live_wal_bytes"] == 12
