"""Tests for ``GET /agents/ingest-health`` over canonical catalog facts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.services import catalogd_supervisor
from zerg.services.ingest_health import compute_ingest_health_from_catalog_facts


def _setup_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test_agents_ingest_health.db"
    blob_root = tmp_path / "media"
    monkeypatch.setenv("LONGHOUSE_MEDIA_BLOB_ROOT", str(blob_root))
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=1)
    api_app.dependency_overrides[require_single_tenant] = lambda: None

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)

    return factory, blob_root, _cleanup


def test_ingest_health_reports_media_repair_debt_separately(live_catalog, live_catalog_client):  # noqa: F811
    """Media debt is counted apart from the session count, not folded into it.

    A session whose commit referenced media the Runtime Host does not hold is
    still a session: it stays in ``session_count`` and shows up separately in
    ``media_repair_refs``. The incomplete state is set here directly because
    what is under test is the health projection, not the media commit path.
    """

    owner_id = live_catalog.create_user("owner@media-debt.test")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    live_catalog.commit_session(owner_id=owner_id)
    incomplete = live_catalog.commit_session(owner_id=owner_id)

    database_path, _socket_path = catalogd_supervisor.catalogd_paths()
    engine = create_catalog_engine(database_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                StorageSession.__table__.update()
                .where(StorageSession.__table__.c.session_id == str(incomplete.session_id))
                .values(media_state="missing", missing_media_hashes_json=json.dumps([hashlib.sha256(b"pending").hexdigest()]))
            )
    finally:
        engine.dispose()

    response = live_catalog_client.get("/agents/ingest-health", headers={"X-Agents-Token": token})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_count"] == 2
    assert body["media_repair_refs"] == 1
    assert body["media_repair_bytes"] == 0


def test_catalog_ingest_health_route_does_not_read_legacy_tables(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    from zerg.routers import agents_backfill as route_module

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.health.v2"
            assert params == {"owner_id": "42"}
            return {
                "session_count": 17_901,
                "last_session_at": datetime.now(timezone.utc).isoformat(),
                "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "media_repair_refs": 10,
                "media_repair_bytes": 0,
            }

    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=42)
    monkeypatch.setattr(route_module, "get_catalogd_client", lambda: Catalog())
    client = TestClient(api_app)
    try:
        response = client.get("/agents/ingest-health")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
        assert response.json()["session_count"] == 17_901
        assert response.json()["media_repair_refs"] == 10
    finally:
        cleanup()


def test_catalog_ingest_health_distinguishes_online_stale_from_offline():
    now = datetime.now(timezone.utc)
    facts = {
        "session_count": 1,
        "last_session_at": (now - timedelta(hours=8)).isoformat(),
        "last_heartbeat_at": (now - timedelta(minutes=1)).isoformat(),
    }
    assert compute_ingest_health_from_catalog_facts(facts, now=now)["status"] == "stale"
    facts["last_heartbeat_at"] = (now - timedelta(hours=1)).isoformat()
    assert compute_ingest_health_from_catalog_facts(facts, now=now)["status"] == "device_offline"
