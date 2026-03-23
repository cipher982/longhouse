from __future__ import annotations

import os
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.enums import UserRole
from zerg.models.loop_push_subscription import LoopPushSubscription
from zerg.models.user import User


def _make_db(tmp_path):
    db_path = tmp_path / "test_loop_push_router.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _seed_user(db) -> User:
    user = User(email="loop-push@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _subscription(endpoint: str = "https://push.example/sub/1"):
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {
            "p256dh": "p256dh-token",
            "auth": "auth-token",
        },
    }


def test_loop_push_config_disabled_by_default(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    monkeypatch.setattr(
        "zerg.routers.oikos_runs.get_settings",
        lambda: SimpleNamespace(loop_push_enabled=False, loop_push_vapid_public_key=None),
    )

    with session_local() as db:
        user = _seed_user(db)
        client, api_app_ref = _make_client(db, user)
        try:
            response = client.get("/api/oikos/push-config")
            assert response.status_code == 200, response.text
            assert response.json() == {"enabled": False, "vapid_public_key": None}
        finally:
            api_app_ref.dependency_overrides = {}


def test_loop_push_subscription_registers_and_revokes(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    monkeypatch.setattr(
        "zerg.routers.oikos_runs.get_settings",
        lambda: SimpleNamespace(loop_push_enabled=True, loop_push_vapid_public_key="PUBLIC_KEY"),
    )

    with session_local() as db:
        user = _seed_user(db)
        client, api_app_ref = _make_client(db, user)
        try:
            config_response = client.get("/api/oikos/push-config")
            assert config_response.status_code == 200, config_response.text
            assert config_response.json() == {"enabled": True, "vapid_public_key": "PUBLIC_KEY"}

            register_payload = {
                "subscription": _subscription(),
                "install_id": "loop-install-1",
                "user_agent": "Loop PWA Test",
            }
            register_response = client.post("/api/oikos/push-subscriptions", json=register_payload)
            assert register_response.status_code == 200, register_response.text
            assert register_response.json()["status"] == "active"

            rows = db.query(LoopPushSubscription).all()
            assert len(rows) == 1
            assert rows[0].install_id == "loop-install-1"
            assert rows[0].revoked_at is None

            updated_response = client.post(
                "/api/oikos/push-subscriptions",
                json={
                    "subscription": _subscription(),
                    "install_id": "loop-install-2",
                    "user_agent": "Loop PWA Test Updated",
                },
            )
            assert updated_response.status_code == 200, updated_response.text

            rows = db.query(LoopPushSubscription).all()
            assert len(rows) == 1
            assert rows[0].install_id == "loop-install-2"
            assert rows[0].user_agent == "Loop PWA Test Updated"

            delete_response = client.request(
                "DELETE",
                "/api/oikos/push-subscriptions",
                json={"endpoint": "https://push.example/sub/1"},
            )
            assert delete_response.status_code == 204, delete_response.text

            row = db.query(LoopPushSubscription).one()
            assert row.revoked_at is not None
        finally:
            api_app_ref.dependency_overrides = {}
