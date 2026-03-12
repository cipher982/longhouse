"""Route-level tests for Gmail connector bootstrap state."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models import Connector
from zerg.models import User
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
