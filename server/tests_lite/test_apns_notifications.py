from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.user import User
from zerg.services.apns_sender import ATTENTION_NOTIFICATION_CATEGORY
from zerg.services.apns_sender import ATTENTION_NOTIFICATION_THREAD_PREFIX
from zerg.services.apns_sender import build_session_attention_payload


def _make_db(tmp_path, name: str = "test_apns.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _cleanup_overrides():
    api_app.dependency_overrides.pop(get_db, None)
    api_app.dependency_overrides.pop(get_current_user, None)
    api_app.dependency_overrides.pop(verify_agents_token, None)


def _seed_user(SessionLocal, *, user_id: int = 1, prefs: dict | None = None):
    with SessionLocal() as db:
        user = User(id=user_id, email=f"user-{user_id}@example.com", role="ADMIN", prefs=prefs or {})
        db.add(user)
        db.commit()


def test_apns_registration_upserts_existing_device(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="user@example.com", role="ADMIN")

    token = "a" * 64
    with TestClient(api_app) as client:
        first = client.post(
            "/devices/apns-register",
            json={
                "device_token": token,
                "platform": "ios",
                "push_environment": "sandbox",
                "app_build_id": "0.1.0-dev+aaaa1111",
            },
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/devices/apns-register",
            json={
                "device_token": token,
                "platform": "ios",
                "push_environment": "production",
                "app_build_id": "0.1.0-dev+bbbb2222",
            },
        )
        assert second.status_code == 200, second.text

    with SessionLocal() as db:
        rows = db.query(APNSDeviceRegistration).all()
        assert len(rows) == 1
        assert rows[0].device_token == token
        assert rows[0].push_environment == "production"
        assert rows[0].app_build_id == "0.1.0-dev+bbbb2222"
        assert rows[0].revoked_at is None

    _cleanup_overrides()
    engine.dispose()


def test_user_notification_settings_default_true_and_patchable(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="user@example.com", role="ADMIN")

    with TestClient(api_app) as client:
        initial = client.get("/users/me/notifications")
        assert initial.status_code == 200, initial.text
        assert initial.json() == {"apns_enabled": True}

        updated = client.patch("/users/me/notifications", json={"apns_enabled": False})
        assert updated.status_code == 200, updated.text
        assert updated.json() == {"apns_enabled": False}

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == 1).first()
        assert user is not None
        assert dict(user.prefs or {})["apns_enabled"] is False

    _cleanup_overrides()
    engine.dispose()


def test_presence_attention_transition_sends_and_debounces_push(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN"))
        db.commit()
        db.add(
            APNSDeviceRegistration(
                owner_id=1,
                platform="ios",
                device_token="b" * 64,
                push_environment="sandbox",
                app_build_id="0.1.0-dev+aaaa1111",
            )
        )
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                loop_mode="manual",
                summary_title="Fix failing build",
                summary="Fix failing build in repo root",
            )
        )
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="devbox", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token

    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    send_mock = AsyncMock()

    with patch("zerg.routers.presence.send_session_attention_push", send_mock):
        with TestClient(api_app) as client:
            first = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "needs_user",
                    "occurred_at": t0.isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert first.status_code == 204, first.text

            second = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "idle",
                    "occurred_at": (t0 + timedelta(seconds=10)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert second.status_code == 204, second.text

            third = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "blocked",
                    "tool_name": "Bash",
                    "occurred_at": (t0 + timedelta(seconds=20)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert third.status_code == 204, third.text

            fourth = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "idle",
                    "occurred_at": (t0 + timedelta(seconds=50)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert fourth.status_code == 204, fourth.text

            fifth = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "needs_user",
                    "occurred_at": (t0 + timedelta(seconds=61)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert fifth.status_code == 204, fifth.text

    assert send_mock.await_count == 2
    first_notification = send_mock.await_args_list[0].args[0]
    second_notification = send_mock.await_args_list[1].args[0]
    assert first_notification.session_id == session_id
    assert first_notification.state == "needs_user"
    assert first_notification.alert_title == "Claude needs you"
    assert first_notification.alert_body == "zerg · Fix failing build"
    assert first_notification.collapse_id == f"lh-attn-{session_id}"
    assert second_notification.state == "needs_user"
    payload = build_session_attention_payload(first_notification)
    assert payload["aps"]["category"] == ATTENTION_NOTIFICATION_CATEGORY
    assert payload["aps"]["thread-id"] == f"{ATTENTION_NOTIFICATION_THREAD_PREFIX}-{session_id}"
    assert payload["attention_state"] == "needs_user"
    assert payload["project"] == "zerg"
    assert payload["provider"] == "claude"

    with SessionLocal() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        assert session is not None
        assert session.last_attention_push_state == "needs_user"
        assert session.last_attention_push_at is not None

    _cleanup_overrides()
    engine.dispose()


def test_presence_attention_push_respects_user_mute(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN", prefs={"apns_enabled": False}))
        db.commit()
        db.add(
            APNSDeviceRegistration(
                owner_id=1,
                platform="ios",
                device_token="c" * 64,
                push_environment="sandbox",
                app_build_id="0.1.0-dev+aaaa1111",
            )
        )
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                loop_mode="manual",
                summary_title="Wait for input",
            )
        )
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="devbox", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token

    send_mock = AsyncMock()
    with patch("zerg.routers.presence.send_session_attention_push", send_mock):
        with TestClient(api_app) as client:
            response = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "needs_user",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert response.status_code == 204, response.text

    send_mock.assert_not_awaited()

    _cleanup_overrides()
    engine.dispose()
