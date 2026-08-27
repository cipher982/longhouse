from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.services.catalogd_supervisor import catalogd_paths

DEVICE_ID = "work-macbook"


def _make_db(tmp_path, name: str = "machine_presence.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _cleanup_overrides():
    api_app.dependency_overrides.pop(get_db, None)
    api_app.dependency_overrides.pop(verify_agents_token, None)


def _device_token(*, owner_id: int = 1, device_id: str = DEVICE_ID) -> DeviceToken:
    return DeviceToken(
        id=uuid4(),
        owner_id=owner_id,
        device_id=device_id,
        token_hash="0" * 64,
    )


def _presence_rows(owner_id: int) -> list[dict[str, Any]]:
    """Rows the live catalog actually wrote, read straight off its database."""

    database_path, _socket_path = catalogd_paths()
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT owner_id, device_id, state, source, idle_seconds FROM machine_presence WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _seed_owner(live_catalog: LiveCatalog, *, device_id: str = DEVICE_ID) -> tuple[int, str]:
    owner_id = live_catalog.create_user("user@example.com")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=device_id)
    return owner_id, token


def test_machine_presence_upserts_device_token_owned_state(live_catalog, live_catalog_client):
    owner_id, token = _seed_owner(live_catalog)

    measured_at = datetime(2026, 6, 4, 20, 15, tzinfo=timezone.utc)
    first = live_catalog_client.post(
        "/agents/machine-presence",
        json={
            "state": "idle_5m",
            "source": "macos_hid_idle",
            "idle_seconds": 360,
            "measured_at": measured_at.isoformat(),
        },
        headers={"X-Agents-Token": token},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["owner_id"] == owner_id
    assert first_body["device_id"] == DEVICE_ID
    assert first_body["state"] == "idle_5m"
    assert first_body["source"] == "macos_hid_idle"
    assert first_body["idle_seconds"] == 300

    second = live_catalog_client.post(
        "/agents/machine-presence",
        json={
            "state": "active",
            "source": "macos_hid_idle",
            "idle_seconds": 3,
            "measured_at": measured_at.isoformat(),
        },
        headers={"X-Agents-Token": token},
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "active"

    rows = _presence_rows(owner_id)
    assert len(rows) == 1
    assert rows[0] == {
        "owner_id": owner_id,
        "device_id": DEVICE_ID,
        "state": "active",
        "source": "macos_hid_idle",
        "idle_seconds": 0,
    }


def test_machine_presence_policy_defaults_enabled(live_catalog, live_catalog_client):
    _owner_id, token = _seed_owner(live_catalog)

    response = live_catalog_client.get("/agents/machine-presence/policy", headers={"X-Agents-Token": token})
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "min_interval_seconds": 60}


def test_machine_presence_policy_and_post_respect_user_disable(live_catalog, live_catalog_client):
    owner_id, token = _seed_owner(live_catalog)
    live_catalog.rpc(
        "auth.user.update.v2",
        {
            "user_id": owner_id,
            "display_name": None,
            "avatar_url": None,
            "prefs": {"machine_presence_enabled": False},
            "update_mask": ["prefs"],
        },
    )

    policy = live_catalog_client.get("/agents/machine-presence/policy", headers={"X-Agents-Token": token})
    assert policy.status_code == 200, policy.text
    assert policy.json()["enabled"] is False

    update = live_catalog_client.post(
        "/agents/machine-presence",
        json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 1},
        headers={"X-Agents-Token": token},
    )
    assert update.status_code == 403
    assert _presence_rows(owner_id) == []


def test_machine_presence_rejects_invalid_state_and_idle_range(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_invalid.db")
    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN"))
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: _device_token()

    with TestClient(api_app) as client:
        bad_state = client.post(
            "/agents/machine-presence",
            json={"state": "watching_youtube", "source": "macos_hid_idle"},
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert bad_state.status_code == 422

        bad_idle = client.post(
            "/agents/machine-presence",
            json={"state": "idle_10m", "source": "macos_hid_idle", "idle_seconds": -1},
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert bad_idle.status_code == 422

    _cleanup_overrides()
    engine.dispose()


def test_machine_presence_rebuckets_idle_seconds_server_side(live_catalog, live_catalog_client):
    owner_id, token = _seed_owner(live_catalog)

    response = live_catalog_client.post(
        "/agents/machine-presence",
        json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 999},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "idle_10m"
    assert body["idle_seconds"] == 600

    assert _presence_rows(owner_id)[0]["idle_seconds"] == 600


def test_machine_presence_auth_disabled_uses_single_tenant_owner(live_catalog):
    """No device token: identity falls back to the catalog's single owner."""

    owner_id = live_catalog.create_user("user@example.com")

    with live_catalog.http_client(extra_overrides={verify_agents_token: lambda: None}) as client:
        response = client.post(
            "/agents/machine-presence",
            json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 2},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner_id"] == owner_id
    assert body["device_id"] == "auth-disabled-local"

    assert _presence_rows(owner_id)[0]["device_id"] == "auth-disabled-local"


def test_machine_presence_requires_device_token_identity(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_auth.db")

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=1, device_id="hook")

    with TestClient(api_app) as client:
        response = client.post(
            "/agents/machine-presence",
            json={"state": "active", "source": "macos_hid_idle"},
            headers={"X-Agents-Token": "managed-hook"},
        )
        assert response.status_code == 401

    _cleanup_overrides()
    engine.dispose()
