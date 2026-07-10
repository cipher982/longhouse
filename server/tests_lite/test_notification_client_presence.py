from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_live_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.notification_client_presence import NotificationClientPresence
from zerg.models.user import User


def _make_db(tmp_path, name: str = "notification_client_presence.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _cleanup_overrides():
    api_app.dependency_overrides.pop(get_db, None)
    api_app.dependency_overrides.pop(get_current_user, None)


def test_user_client_presence_upserts_web_heartbeat(tmp_path):
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
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="user@example.com", role="ADMIN")

    with TestClient(api_app) as client:
        first = client.post(
            "/users/me/client-presence",
            json={
                "client_id": "web-client-1",
                "client_type": "web",
                "visible": True,
                "route": "/timeline/session-1",
                "session_id": "session-1",
            },
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["visible"] is True
        assert first_body["route"] == "/timeline/session-1"
        assert first_body["session_id"] == "session-1"

        second = client.post(
            "/users/me/client-presence",
            json={
                "client_id": "web-client-1",
                "client_type": "web",
                "visible": False,
                "route": "/timeline",
                "session_id": None,
            },
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["visible"] is False
        assert second_body["route"] == "/timeline"
        assert second_body["session_id"] is None

    with SessionLocal() as db:
        rows = db.query(NotificationClientPresence).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.owner_id == 1
        assert row.client_id == "web-client-1"
        assert row.client_type == "web"
        assert row.visible is False
        assert row.route == "/timeline"
        assert row.session_id is None
        last_seen_at = row.last_seen_at
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        response_seen_at = second_body["last_seen_at"].replace("Z", "+00:00")
        assert last_seen_at == datetime.fromisoformat(response_seen_at)

    _cleanup_overrides()
    engine.dispose()


def test_user_client_presence_validates_client_identity(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "notification_client_presence_invalid.db")
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
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="user@example.com", role="ADMIN")

    with TestClient(api_app) as client:
        response = client.post(
            "/users/me/client-presence",
            json={
                "client_id": "short",
                "client_type": "web",
                "visible": True,
                "route": "/timeline",
                "session_id": None,
            },
        )
        assert response.status_code == 422

    _cleanup_overrides()
    engine.dispose()


def test_user_client_presence_uses_live_store_schema(tmp_path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(engine)
    SessionLocal = make_sessionmaker(engine)

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="user@example.com", role="ADMIN")

    class _DirectSerializer:
        async def execute_or_direct(self, fn, fallback_db, **_kwargs):
            result = fn(fallback_db)
            fallback_db.commit()
            return result

    with patch("zerg.routers.users.get_catalog_write_serializer", return_value=_DirectSerializer()):
        with TestClient(api_app) as client:
            response = client.post(
                "/users/me/client-presence",
                json={
                    "client_id": "live-web-client",
                    "client_type": "web",
                    "visible": True,
                    "route": "/timeline",
                    "session_id": None,
                },
            )

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        assert db.query(NotificationClientPresence).filter_by(client_id="live-web-client").count() == 1

    _cleanup_overrides()
    engine.dispose()
