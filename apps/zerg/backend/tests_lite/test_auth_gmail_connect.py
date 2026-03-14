"""Route-level tests for Gmail connector bootstrap state."""

from __future__ import annotations

import os
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.auth.strategy import _decode_jwt_fallback
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_browser_user
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models import Connector
from zerg.models import User
from zerg.routers.auth import JWT_SECRET
from zerg.routers.auth import _encode_jwt
from zerg.utils.crypto import decrypt
from zerg.utils.crypto import encrypt


def _make_db(tmp_path):
    db_path = tmp_path / "test_auth_gmail_connect.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1, email: str = "owner@example.com") -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(
    db,
    *,
    owner_id: int,
    config: dict[str, object],
) -> Connector:
    connector = Connector(owner_id=owner_id, type="email", provider="gmail", config=config)
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def _make_client(session_local, current_user: User):
    def override_db():
        with session_local() as db:
            yield db

    def override_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = override_user
    api_app.dependency_overrides[get_current_browser_user] = override_user
    return TestClient(api_app)


def test_connect_gmail_starts_pubsub_watch_without_callback_url(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)

    client = _make_client(session_local, owner)
    start_watch = Mock(return_value={"history_id": 321, "watch_expiry": 654321})
    settings = SimpleNamespace(testing=False, gmail_pubsub_topic="projects/demo/topics/gmail", app_public_url=None)

    try:
        with (
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth._exchange_google_auth_code", return_value={"refresh_token": "refresh-token"}),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "Owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", start_watch),
        ):
            response = client.post("/auth/google/gmail", json={"auth_code": "auth-code"})

        assert response.status_code == 200
        assert response.json() == {
            "status": "connected",
            "connector_id": 1,
            "mailbox_email": "owner@gmail.com",
            "watch": {
                "status": "active",
                "method": "pubsub",
                "history_id": 321,
                "watch_expiry": 654321,
                "error": None,
            },
        }
        start_watch.assert_called_once_with(access_token="access-token", topic_name="projects/demo/topics/gmail")

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["history_id"] == 321
            assert connector.config["watch_expiry"] == 654321
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert connector.config["watch_status"] == "active"
            assert connector.config["watch_method"] == "pubsub"
            assert connector.config["watch_error"] is None
            assert connector.config["refresh_token"] != "refresh-token"
            assert connector.config["watch_checked_at"]
    finally:
        api_app.dependency_overrides.clear()


def test_connect_gmail_preserves_existing_watch_state_when_bootstrap_fails(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)
        _seed_connector(
            db,
            owner_id=owner.id,
            config={
                "refresh_token": encrypt("old-refresh"),
                "emailAddress": "owner@gmail.com",
                "history_id": 100,
                "watch_expiry": 200,
                "last_notified_history_id": 150,
            },
        )

    client = _make_client(session_local, owner)
    settings = SimpleNamespace(testing=False, gmail_pubsub_topic="projects/demo/topics/gmail", app_public_url=None)

    try:
        with (
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth._exchange_google_auth_code", return_value={"refresh_token": "new-refresh"}),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", side_effect=RuntimeError("pubsub boom")),
        ):
            response = client.post("/auth/google/gmail", json={"auth_code": "auth-code"})

        assert response.status_code == 200
        data = response.json()
        assert data["watch"]["status"] == "failed"
        assert data["watch"]["method"] == "pubsub"
        assert "pubsub boom" in data["watch"]["error"]

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["history_id"] == 100
            assert connector.config["watch_expiry"] == 200
            assert connector.config["last_notified_history_id"] == 150
            assert connector.config["watch_status"] == "failed"
            assert connector.config["watch_method"] == "pubsub"
            assert "pubsub boom" in connector.config["watch_error"]
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert decrypt(connector.config["refresh_token"]) == "new-refresh"
    finally:
        api_app.dependency_overrides.clear()


def test_connect_gmail_reports_pubsub_not_configured_in_production(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)

    client = _make_client(session_local, owner)
    start_watch = Mock()
    settings = SimpleNamespace(testing=False, gmail_pubsub_topic=None, app_public_url="https://example.com")

    try:
        with (
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth._exchange_google_auth_code", return_value={"refresh_token": "refresh-token"}),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", start_watch),
        ):
            response = client.post(
                "/auth/google/gmail",
                json={"auth_code": "auth-code", "callback_url": "https://example.com/api/email/webhook/google"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["watch"]["status"] == "not_configured"
        assert "test-only" in data["watch"]["error"]
        start_watch.assert_not_called()

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["watch_status"] == "not_configured"
            assert connector.config["watch_method"] is None
            assert "test-only" in connector.config["watch_error"]
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert "history_id" not in connector.config
            assert "watch_expiry" not in connector.config
    finally:
        api_app.dependency_overrides.clear()


def test_connect_gmail_fails_pubsub_watch_when_mailbox_address_is_missing(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)

    client = _make_client(session_local, owner)
    settings = SimpleNamespace(testing=False, gmail_pubsub_topic="projects/demo/topics/gmail", app_public_url=None)

    try:
        with (
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth._exchange_google_auth_code", return_value={"refresh_token": "refresh-token"}),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={}),
            patch("zerg.services.gmail_api.start_watch", return_value={"history_id": 321, "watch_expiry": 654321}),
        ):
            response = client.post("/auth/google/gmail", json={"auth_code": "auth-code"})

        assert response.status_code == 200
        data = response.json()
        assert data["mailbox_email"] is None
        assert data["watch"]["status"] == "failed"
        assert data["watch"]["method"] == "pubsub"
        assert "could not resolve mailbox email" in data["watch"]["error"]

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["watch_status"] == "failed"
            assert connector.config["watch_method"] == "pubsub"
            assert "could not resolve mailbox email" in connector.config["watch_error"]
            assert "history_id" not in connector.config
            assert "watch_expiry" not in connector.config
            assert "emailAddress" not in connector.config
    finally:
        api_app.dependency_overrides.clear()


def test_start_hosted_gmail_connect_returns_control_plane_redirect(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)

    client = _make_client(session_local, owner)
    settings = SimpleNamespace(control_plane_url="https://control.longhouse.ai")

    try:
        with (
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch.dict(os.environ, {"INSTANCE_ID": "hosted-owner"}, clear=False),
        ):
            response = client.post("/auth/google/gmail/start")

        assert response.status_code == 200
        url = response.json()["url"]
        parsed = urllib.parse.urlparse(url)
        token = urllib.parse.parse_qs(parsed.query)["token"][0]
        payload = _decode_jwt_fallback(token, JWT_SECRET)
        assert parsed.scheme == "https"
        assert parsed.netloc == "control.longhouse.ai"
        assert parsed.path == "/auth/google/gmail/start"
        assert payload["purpose"] == "hosted_gmail_connect_start"
        assert payload["instance"] == "hosted-owner"
        assert payload["email"] == "owner@example.com"
    finally:
        api_app.dependency_overrides.clear()


def test_connect_gmail_rejects_hosted_instances(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)

    client = _make_client(session_local, owner)
    settings = SimpleNamespace(control_plane_url="https://control.longhouse.ai", testing=False)

    try:
        with patch("zerg.routers.auth.get_settings", return_value=settings):
            response = client.post("/auth/google/gmail", json={"auth_code": "auth-code"})

        assert response.status_code == 409
        assert response.json()["detail"] == "Hosted Gmail connect must start on the control plane."
    finally:
        api_app.dependency_overrides.clear()


def test_hosted_gmail_handoff_bootstraps_connector(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    client = TestClient(api_app)
    settings = SimpleNamespace(
        auth_disabled=False,
        testing=False,
        internal_api_secret="test-internal-secret",
        single_tenant=True,
        gmail_pubsub_topic="projects/demo/topics/gmail",
        app_public_url=None,
    )
    handoff_token = _encode_jwt(
        {
            "sub": "owner@example.com",
            "email": "owner@example.com",
            "instance": "hosted-owner",
            "purpose": "hosted_gmail_connect_handoff",
            "exp": int(time.time()) + 300,
        },
        JWT_SECRET,
    )

    try:
        with (
            patch("zerg.dependencies.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_settings", return_value=settings),
            patch("zerg.routers.auth.get_settings", return_value=settings),
            patch("zerg.routers.auth_internal.get_sso_keys", return_value=[]),
            patch("zerg.services.gmail_api.exchange_refresh_token", return_value="access-token"),
            patch("zerg.services.gmail_api.get_profile", return_value={"emailAddress": "Owner@gmail.com"}),
            patch("zerg.services.gmail_api.start_watch", return_value={"history_id": 321, "watch_expiry": 654321}),
            patch.dict(os.environ, {"INSTANCE_ID": "hosted-owner"}, clear=False),
        ):
            response = client.post(
                "/internal/auth/google/gmail/handoff",
                json={
                    "refresh_token": "refresh-token",
                    "handoff_token": handoff_token,
                },
                headers={"X-Internal-Token": "test-internal-secret"},
            )

        assert response.status_code == 200
        assert response.json()["mailbox_email"] == "owner@gmail.com"
        assert response.json()["watch"]["status"] == "active"

        with session_local() as db:
            connector = db.get(Connector, 1)
            assert connector is not None
            assert connector.config["emailAddress"] == "owner@gmail.com"
            assert connector.config["watch_status"] == "active"
            assert decrypt(connector.config["refresh_token"]) == "refresh-token"
    finally:
        api_app.dependency_overrides.clear()
