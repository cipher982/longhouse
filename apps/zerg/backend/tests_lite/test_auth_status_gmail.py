from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models import Connector
from zerg.models import User
from zerg.utils.crypto import encrypt


def _make_db(tmp_path):
    db_path = tmp_path / "test_auth_status_gmail.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1, email: str = "owner@example.com") -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(db, *, owner_id: int, config: dict[str, object]) -> Connector:
    connector = Connector(owner_id=owner_id, type="email", provider="gmail", config=config)
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def _make_client(session_local):
    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    return TestClient(api_app)


def test_auth_status_reports_real_gmail_connector_health(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)
        _seed_connector(
            db,
            owner_id=owner.id,
            config={
                "refresh_token": encrypt("refresh-token"),
                "emailAddress": "Owner@gmail.com",
                "watch_status": "active",
                "watch_error": None,
                "watch_expiry": 654321,
            },
        )

    client = _make_client(session_local)

    try:
        with session_local() as db:
            owner = db.get(User, 1)
            with patch("zerg.routers.auth.get_optional_browser_user", return_value=owner):
                response = client.get("/auth/status")

        assert response.status_code == 200
        assert response.json()["user"] == {
            "id": 1,
            "email": "owner@example.com",
            "display_name": None,
            "avatar_url": None,
            "is_active": True,
            "created_at": response.json()["user"]["created_at"],
            "last_login": None,
            "prefs": {},
            "role": "USER",
            "gmail_connected": True,
            "gmail_mailbox_email": "owner@gmail.com",
            "gmail_watch_status": "active",
            "gmail_watch_error": None,
            "gmail_watch_expiry": 654321,
        }
    finally:
        api_app.dependency_overrides.clear()


def test_auth_status_flags_legacy_connector_without_watch_bootstrap(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        owner = _seed_user(db)
        _seed_connector(
            db,
            owner_id=owner.id,
            config={
                "refresh_token": encrypt("refresh-token"),
                "emailAddress": "owner@gmail.com",
            },
        )

    client = _make_client(session_local)

    try:
        with session_local() as db:
            owner = db.get(User, 1)
            with patch("zerg.routers.auth.get_optional_browser_user", return_value=owner):
                response = client.get("/auth/status")

        assert response.status_code == 200
        payload = response.json()["user"]
        assert payload["gmail_connected"] is True
        assert payload["gmail_mailbox_email"] == "owner@gmail.com"
        assert payload["gmail_watch_status"] == "failed"
        assert payload["gmail_watch_error"] == "Reconnect Gmail to finish email sync."
        assert payload["gmail_watch_expiry"] is None
    finally:
        api_app.dependency_overrides.clear()
