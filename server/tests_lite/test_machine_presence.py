from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.device_token import DeviceToken
from zerg.models.machine_presence import MachinePresence
from zerg.models.user import User


def _make_db(tmp_path, name: str = "machine_presence.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _cleanup_overrides():
    api_app.dependency_overrides.pop(get_db, None)
    api_app.dependency_overrides.pop(verify_agents_token, None)


def _device_token(*, owner_id: int = 1, device_id: str = "work-macbook") -> DeviceToken:
    return DeviceToken(
        id=uuid4(),
        owner_id=owner_id,
        device_id=device_id,
        token_hash="0" * 64,
    )


def test_machine_presence_upserts_device_token_owned_state(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
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

    measured_at = datetime(2026, 6, 4, 20, 15, tzinfo=timezone.utc)
    with TestClient(api_app) as client:
        first = client.post(
            "/agents/machine-presence",
            json={
                "state": "idle_5m",
                "source": "macos_hid_idle",
                "idle_seconds": 360,
                "measured_at": measured_at.isoformat(),
            },
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["owner_id"] == 1
        assert first_body["device_id"] == "work-macbook"
        assert first_body["state"] == "idle_5m"
        assert first_body["source"] == "macos_hid_idle"
        assert first_body["idle_seconds"] == 300

        second = client.post(
            "/agents/machine-presence",
            json={
                "state": "active",
                "source": "macos_hid_idle",
                "idle_seconds": 3,
                "measured_at": measured_at.isoformat(),
            },
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["state"] == "active"

    with SessionLocal() as db:
        rows = db.query(MachinePresence).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.owner_id == 1
        assert row.device_id == "work-macbook"
        assert row.state == "active"
        assert row.source == "macos_hid_idle"
        assert row.idle_seconds == 0

    _cleanup_overrides()
    engine.dispose()


def test_machine_presence_policy_defaults_enabled(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_policy.db")
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
        response = client.get("/agents/machine-presence/policy", headers={"X-Agents-Token": "zdt_test"})
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": True, "min_interval_seconds": 60}

    _cleanup_overrides()
    engine.dispose()


def test_machine_presence_policy_and_post_respect_user_disable(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_disabled.db")
    with SessionLocal() as db:
        db.add(
            User(
                id=1,
                email="user@example.com",
                role="ADMIN",
                prefs={"machine_presence_enabled": False},
            )
        )
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
        policy = client.get("/agents/machine-presence/policy", headers={"X-Agents-Token": "zdt_test"})
        assert policy.status_code == 200, policy.text
        assert policy.json()["enabled"] is False

        update = client.post(
            "/agents/machine-presence",
            json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 1},
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert update.status_code == 403

    _cleanup_overrides()
    engine.dispose()


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


def test_machine_presence_rebuckets_idle_seconds_server_side(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_rebucket.db")
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
        response = client.post(
            "/agents/machine-presence",
            json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 999},
            headers={"X-Agents-Token": "zdt_test"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "idle_10m"
        assert body["idle_seconds"] == 600

    _cleanup_overrides()
    engine.dispose()


def test_machine_presence_auth_disabled_uses_single_tenant_owner(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "machine_presence_auth_disabled.db")
    with SessionLocal() as db:
        db.add(User(id=7, email="user@example.com", role="ADMIN"))
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    with TestClient(api_app) as client:
        response = client.post(
            "/agents/machine-presence",
            json={"state": "active", "source": "macos_hid_idle", "idle_seconds": 2},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["owner_id"] == 7
        assert body["device_id"] == "auth-disabled-local"

    _cleanup_overrides()
    engine.dispose()


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
