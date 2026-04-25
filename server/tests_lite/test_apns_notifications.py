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
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.apns_device_registration import APNSDeviceRegistration
from zerg.models.apns_widget_push_state import APNSWidgetPushState
from zerg.models.user import User
from zerg.services.apns_sender import ATTENTION_NOTIFICATION_CATEGORY
from zerg.services.apns_sender import ATTENTION_NOTIFICATION_THREAD_PREFIX
from zerg.services.apns_sender import SessionAttentionPush
from zerg.services.apns_sender import _attention_collapse_id
from zerg.services.apns_sender import build_session_attention_payload
from zerg.services.apns_sender import build_session_attention_resolution_payload
from zerg.services.apns_sender import build_widget_timeline_payload


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
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="user@example.com",
        role="ADMIN",
    )

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


def test_apns_registration_accepts_widget_tokens(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    _seed_user(SessionLocal)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="user@example.com",
        role="ADMIN",
    )

    token = "e" * 64
    with TestClient(api_app) as client:
        response = client.post(
            "/devices/apns-register",
            json={
                "device_token": token,
                "platform": "ios_widget",
                "push_environment": "sandbox",
            },
        )
        assert response.status_code == 200, response.text

    with SessionLocal() as db:
        row = db.query(APNSDeviceRegistration).one()
        assert row.platform == "ios_widget"
        assert row.device_token == token

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
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1,
        email="user@example.com",
        role="ADMIN",
    )

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
    send_mock = AsyncMock(return_value=True)
    resolution_send_mock = AsyncMock(return_value=True)

    with (
        patch("zerg.routers.presence.send_session_attention_push", send_mock),
        patch("zerg.routers.presence.send_session_attention_resolution_push", resolution_send_mock),
    ):
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

    assert send_mock.await_count == 3
    assert resolution_send_mock.await_count == 2
    first_notification = send_mock.await_args_list[0].args[0]
    second_notification = send_mock.await_args_list[1].args[0]
    third_notification = send_mock.await_args_list[2].args[0]
    first_resolution = resolution_send_mock.await_args_list[0].args[0]
    second_resolution = resolution_send_mock.await_args_list[1].args[0]
    assert first_notification.session_id == session_id
    assert first_notification.state == "needs_user"
    assert first_notification.alert_title == "Claude needs you"
    assert first_notification.alert_body == "zerg · Fix failing build"
    assert first_notification.collapse_id == f"lh-attn-{session_id}"
    assert second_notification.state == "blocked"
    assert second_notification.alert_title == "Needs permission"
    assert second_notification.alert_body == "zerg · Blocked on Bash · Fix failing build"
    assert third_notification.state == "needs_user"
    assert first_resolution.session_id == session_id
    assert first_resolution.previous_state == "needs_user"
    assert first_resolution.current_state == "idle"
    assert first_resolution.collapse_id == f"lh-attn-resolved-{session_id}"
    assert second_resolution.previous_state == "blocked"
    resolution_payload = build_session_attention_resolution_payload(first_resolution)
    assert resolution_payload["aps"] == {"content-available": 1}
    assert resolution_payload["event"] == "attention_resolved"
    assert resolution_payload["attention_state"] == "resolved"
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


def test_presence_resolution_push_requires_unresolved_attention_push(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN"))
        db.commit()
        db.add(
            APNSDeviceRegistration(
                owner_id=1,
                platform="ios",
                device_token="d" * 64,
                push_environment="sandbox",
                app_build_id="0.1.0-dev+aaaa1111",
            )
        )
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                loop_mode="manual",
                summary_title="Flappy session",
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
    send_mock = AsyncMock(return_value=True)
    resolution_send_mock = AsyncMock(return_value=True)

    with (
        patch("zerg.routers.presence.send_session_attention_push", send_mock),
        patch("zerg.routers.presence.send_session_attention_resolution_push", resolution_send_mock),
    ):
        with TestClient(api_app) as client:
            for state, seconds in [
                ("needs_user", 0),
                ("idle", 5),
                ("needs_user", 10),
                ("idle", 15),
                ("needs_user", 35),
                ("idle", 36),
            ]:
                response = client.post(
                    "/agents/presence",
                    json={
                        "session_id": session_id,
                        "state": state,
                        "occurred_at": (t0 + timedelta(seconds=seconds)).isoformat(),
                    },
                    headers={"X-Agents-Token": "device-token"},
                )
                assert response.status_code == 204, response.text

    assert send_mock.await_count == 2
    assert resolution_send_mock.await_count == 2
    assert send_mock.await_args_list[0].args[0].occurred_at == t0
    assert send_mock.await_args_list[1].args[0].occurred_at == t0 + timedelta(seconds=35)
    assert resolution_send_mock.await_args_list[0].args[0].occurred_at == t0 + timedelta(seconds=5)
    assert resolution_send_mock.await_args_list[1].args[0].occurred_at == t0 + timedelta(seconds=36)
    with SessionLocal() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        assert session is not None
        assert session.last_attention_push_state == "needs_user:resolved"
        last_push_at = session.last_attention_push_at
        assert last_push_at is not None
        if last_push_at.tzinfo is None:
            last_push_at = last_push_at.replace(tzinfo=timezone.utc)
        assert last_push_at == t0 + timedelta(seconds=35)

    _cleanup_overrides()
    engine.dispose()


def test_presence_widget_push_uses_set_hash_and_debounce(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN"))
        db.commit()
        db.add(
            APNSDeviceRegistration(
                owner_id=1,
                platform="ios_widget",
                device_token="f" * 64,
                push_environment="sandbox",
                app_build_id="0.1.0-dev+aaaa1111",
            )
        )
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                loop_mode="manual",
                summary_title="Widget watched session",
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
    widget_send_mock = AsyncMock(return_value=True)

    with patch("zerg.routers.presence.send_widget_timeline_push", widget_send_mock):
        with TestClient(api_app) as client:
            for state, seconds in [
                ("thinking", 0),
                ("running", 5),
                ("running", 35),
            ]:
                response = client.post(
                    "/agents/presence",
                    json={
                        "session_id": session_id,
                        "state": state,
                        "occurred_at": (t0 + timedelta(seconds=seconds)).isoformat(),
                    },
                    headers={"X-Agents-Token": "device-token"},
                )
                assert response.status_code == 204, response.text

    assert widget_send_mock.await_count == 2
    first_push = widget_send_mock.await_args_list[0].args[0]
    second_push = widget_send_mock.await_args_list[1].args[0]
    assert first_push.collapse_id == "lh-widget-1"
    assert first_push.state_hash != second_push.state_hash
    assert first_push.targets[0].device_token == "f" * 64
    assert build_widget_timeline_payload() == {"aps": {"content-changed": True}}

    with SessionLocal() as db:
        state = db.query(APNSWidgetPushState).filter(APNSWidgetPushState.owner_id == 1).one()
        assert state.state_hash == second_push.state_hash

    _cleanup_overrides()
    engine.dispose()


def test_presence_attention_send_failure_clears_debounce_stamp(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    session_id = str(uuid4())

    with SessionLocal() as db:
        db.add(User(id=1, email="user@example.com", role="ADMIN"))
        db.commit()
        db.add(
            APNSDeviceRegistration(
                owner_id=1,
                platform="ios",
                device_token="d" * 64,
                push_environment="sandbox",
                app_build_id="0.1.0-dev+aaaa1111",
            )
        )
        db.add(
            AgentSession(
                id=session_id,
                provider="codex",
                environment="test",
                project="zerg",
                started_at=datetime.now(timezone.utc),
                loop_mode="manual",
                summary_title="Review failed tests",
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
    send_mock = AsyncMock(return_value=False)
    resolution_send_mock = AsyncMock(return_value=True)

    with (
        patch("zerg.routers.presence.send_session_attention_push", send_mock),
        patch("zerg.routers.presence.send_session_attention_resolution_push", resolution_send_mock),
    ):
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
                    "occurred_at": (t0 + timedelta(seconds=5)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert second.status_code == 204, second.text

            third = client.post(
                "/agents/presence",
                json={
                    "session_id": session_id,
                    "state": "needs_user",
                    "occurred_at": (t0 + timedelta(seconds=10)).isoformat(),
                },
                headers={"X-Agents-Token": "device-token"},
            )
            assert third.status_code == 204, third.text

    assert send_mock.await_count == 2
    assert resolution_send_mock.await_count == 0
    with SessionLocal() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        assert session is not None
        assert session.last_attention_push_state is None
        assert session.last_attention_push_at is None

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


def test_attention_payload_bounds_long_title_and_collapse_id():
    long_session_id = "session-" + ("x" * 200)
    long_title = "Investigate " + ("very " * 100) + "long session"
    notification = SessionAttentionPush(
        session_id=long_session_id,
        state="needs_user",
        occurred_at=datetime.now(timezone.utc),
        title=long_title,
        summary=long_title,
        project=None,
        provider="codex",
        tool_name=None,
        alert_title="Codex needs you",
        alert_body="Waiting for you",
        collapse_id=_attention_collapse_id(long_session_id),
        targets=(),
    )

    payload = build_session_attention_payload(notification)

    assert len(notification.collapse_id.encode("utf-8")) <= 64
    assert len(payload["title"]) <= 200
    assert payload["title"].endswith("…")
