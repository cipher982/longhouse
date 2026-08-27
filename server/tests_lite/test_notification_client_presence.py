from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.user import User
from zerg.services.catalogd_supervisor import catalogd_paths

OWNER_EMAIL = "user@example.com"


def _make_db(tmp_path, name: str = "notification_client_presence.db"):
    engine = make_engine(f"sqlite:///{tmp_path}/{name}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _cleanup_overrides():
    api_app.dependency_overrides.pop(get_db, None)
    api_app.dependency_overrides.pop(get_current_user, None)


def _presence_rows(owner_id: int) -> list[dict[str, Any]]:
    """Rows the live catalog actually wrote, read straight off its database."""

    database_path, _socket_path = catalogd_paths()
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT owner_id, client_id, client_type, visible, route, session_id, last_seen_at "
            "FROM notification_client_presence WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def test_user_client_presence_upserts_web_heartbeat(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user(OWNER_EMAIL)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=OWNER_EMAIL)}

    first = live_catalog_client.post(
        "/users/me/client-presence",
        json={
            "client_id": "web-client-1",
            "client_type": "web",
            "visible": True,
            "route": "/timeline/session-1",
            "session_id": "session-1",
        },
        cookies=cookies,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["visible"] is True
    assert first_body["route"] == "/timeline/session-1"
    assert first_body["session_id"] == "session-1"

    second = live_catalog_client.post(
        "/users/me/client-presence",
        json={
            "client_id": "web-client-1",
            "client_type": "web",
            "visible": False,
            "route": "/timeline",
            "session_id": None,
        },
        cookies=cookies,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["visible"] is False
    assert second_body["route"] == "/timeline"
    assert second_body["session_id"] is None

    rows = _presence_rows(owner_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["client_id"] == "web-client-1"
    assert row["client_type"] == "web"
    assert bool(row["visible"]) is False
    assert row["route"] == "/timeline"
    assert row["session_id"] is None

    last_seen_at = datetime.fromisoformat(row["last_seen_at"])
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    assert last_seen_at == datetime.fromisoformat(second_body["last_seen_at"].replace("Z", "+00:00"))


def test_user_client_presence_validates_client_identity(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "notification_client_presence_invalid.db")
    with SessionLocal() as db:
        db.add(User(id=1, email=OWNER_EMAIL, role="ADMIN"))
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email=OWNER_EMAIL, role="ADMIN")

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
