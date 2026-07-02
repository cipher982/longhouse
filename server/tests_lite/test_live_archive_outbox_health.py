from __future__ import annotations

import datetime
import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-value")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-value")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from zerg.database import initialize_database as init_archive_db  # noqa: E402
from zerg.database import make_live_engine  # noqa: E402
from zerg.database import initialize_live_database  # noqa: E402
from zerg.models.live_store import LiveArchiveOutbox  # noqa: E402
from zerg.config import get_settings  # noqa: E402
import zerg.routers.health as health_mod  # noqa: E402
import zerg.database as db_mod  # noqa: E402


def _make_live_store(tmp_path):
    live_engine = make_live_engine(f"sqlite:///{tmp_path / 'live.db'}")
    initialize_live_database(live_engine)
    LiveSession = sessionmaker(bind=live_engine)
    return live_engine, LiveSession


def _with_live_url(settings, url):
    settings.live_database_url = url
    return settings


def _patch_for_live(url, LiveSession):
    orig_gs = health_mod.get_settings
    health_mod.get_settings = lambda: _with_live_url(get_settings(), url)
    orig_ls = db_mod.live_store_configured
    db_mod.live_store_configured = lambda: True
    orig_gsf = db_mod.get_live_session_factory
    db_mod.get_live_session_factory = lambda: LiveSession
    return orig_gs, orig_ls, orig_gsf


def _unpatch(orig_gs, orig_ls, orig_gsf):
    health_mod.get_settings = orig_gs
    db_mod.live_store_configured = orig_ls
    db_mod.get_live_session_factory = orig_gsf


def test_outbox_stats_populate_when_live_session_exists(tmp_path):
    live_engine, LiveSession = _make_live_store(tmp_path)
    live_url = f"sqlite:///{tmp_path / 'live.db'}"

    init_archive_db()
    app = FastAPI()
    app.include_router(health_mod.router)

    saved = _patch_for_live(live_url, LiveSession)
    try:
        with TestClient(app) as client:
            resp = client.get("/health")
    finally:
        _unpatch(*saved)

    assert resp.status_code == 200
    body = resp.json()
    live_store = body["checks"]["live_store"]
    outbox = live_store["live_archive_outbox"]
    assert outbox["checked"] is True
    assert outbox["table_exists"] is True
    assert outbox["pending_count"] == 0
    assert outbox["failed_count"] == 0
    assert outbox["oldest_pending_created_at"] is None

    live_engine.dispose()


def test_outbox_status_warns_on_failed_count(tmp_path):
    live_engine, LiveSession = _make_live_store(tmp_path)

    now = datetime.datetime.now(datetime.timezone.utc)
    with LiveSession() as db:
        db.add(
            LiveArchiveOutbox(
                idempotency_key="failed-key",
                kind="heartbeat_stamp.v1",
                payload_json="{}",
                created_at=now,
                attempts=3,
                last_error="disk full",
            )
        )
        db.commit()

    live_url = f"sqlite:///{tmp_path / 'live.db'}"

    init_archive_db()
    app = FastAPI()
    app.include_router(health_mod.router)

    saved = _patch_for_live(live_url, LiveSession)
    try:
        with TestClient(app) as client:
            resp = client.get("/health")
    finally:
        _unpatch(*saved)

    assert resp.status_code == 200
    body = resp.json()
    live_store = body["checks"]["live_store"]
    outbox = live_store["live_archive_outbox"]
    assert outbox["checked"] is True
    assert outbox["failed_count"] == 1
    assert live_store["status"] == "warn"
    assert live_store.get("outbox_warn_reason") == "live_archive_outbox_failures"

    live_engine.dispose()


def test_outbox_status_warns_when_pending_row_is_old(tmp_path):
    live_engine, LiveSession = _make_live_store(tmp_path)

    old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)
    with LiveSession() as db:
        db.add(
            LiveArchiveOutbox(
                idempotency_key="old-pending-key",
                kind="runtime_event.v1",
                payload_json="{}",
                created_at=old_time,
                attempts=0,
            )
        )
        db.commit()

    live_url = f"sqlite:///{tmp_path / 'live.db'}"

    init_archive_db()
    app = FastAPI()
    app.include_router(health_mod.router)

    saved = _patch_for_live(live_url, LiveSession)
    try:
        with TestClient(app) as client:
            resp = client.get("/health")
    finally:
        _unpatch(*saved)

    assert resp.status_code == 200
    body = resp.json()
    live_store = body["checks"]["live_store"]
    outbox = live_store["live_archive_outbox"]
    assert outbox["checked"] is True
    assert outbox["pending_count"] == 1
    assert outbox["oldest_pending_created_at"] is not None
    assert live_store["status"] == "warn"
    assert live_store.get("outbox_warn_reason") == "live_archive_outbox_lagging"

    live_engine.dispose()
