"""HTTP tests for the Gmail Pub/Sub webhook."""

from __future__ import annotations

import base64
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.email import providers as email_providers
from zerg.main import api_app
from zerg.models import Connector
from zerg.models.user import User
from zerg.routers import email_webhooks_pubsub


def _make_db(tmp_path):
    db_path = tmp_path / "test_gmail_pubsub_webhook.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, user_id: int = 1, email: str = "owner@gmail.com") -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(
    db,
    *,
    owner_id: int,
    email_address: str,
    history_id: int = 100,
) -> Connector:
    connector = Connector(
        owner_id=owner_id,
        type="email",
        provider="gmail",
        config={
            "emailAddress": email_address,
            "history_id": history_id,
        },
    )
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def _override_db(session_local):
    def override():
        with session_local() as db:
            yield db

    return override


def _pubsub_payload(*, email_address: str | None = None, history_id: object | None = None) -> dict[str, object]:
    data: dict[str, object] = {}
    if email_address is not None:
        data["emailAddress"] = email_address
    if history_id is not None:
        data["historyId"] = history_id
    encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    return {"message": {"data": encoded}}


class _FakeGmailProvider:
    def __init__(self) -> None:
        self.connector_ids: list[int] = []

    async def process_connector(self, connector_id: int) -> None:
        self.connector_ids.append(connector_id)


def test_pubsub_webhook_rejects_invalid_auth(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    monkeypatch.setattr(email_webhooks_pubsub, "validate_pubsub_token", lambda _auth: False)

    client = TestClient(api_app)
    try:
        response = client.post(
            "/email/webhook/google/pubsub",
            headers={"Authorization": "Bearer invalid"},
            json=_pubsub_payload(email_address="owner@gmail.com", history_id=101),
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid authentication token"
    finally:
        api_app.dependency_overrides.clear()


def test_pubsub_webhook_rejects_messages_without_mailbox_address(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(email_webhooks_pubsub, "validate_pubsub_token", lambda _auth: True)
    monkeypatch.setattr(email_providers, "get_provider", lambda _provider: fake_provider)

    client = TestClient(api_app)
    try:
        response = client.post(
            "/email/webhook/google/pubsub",
            headers={"Authorization": "Bearer valid"},
            json=_pubsub_payload(history_id=101),
        )

        assert response.status_code == 202
        assert response.json() == {"status": "rejected", "reason": "missing_email"}
        assert fake_provider.connector_ids == []
    finally:
        api_app.dependency_overrides.clear()


def test_pubsub_webhook_ignores_unknown_mailbox(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(email_webhooks_pubsub, "validate_pubsub_token", lambda _auth: True)
    monkeypatch.setattr(email_providers, "get_provider", lambda _provider: fake_provider)

    client = TestClient(api_app)
    try:
        response = client.post(
            "/email/webhook/google/pubsub",
            headers={"Authorization": "Bearer valid"},
            json=_pubsub_payload(email_address="missing@gmail.com", history_id=101),
        )

        assert response.status_code == 202
        assert response.json() == {"status": "ignored", "reason": "no_connector"}
        assert fake_provider.connector_ids == []
    finally:
        api_app.dependency_overrides.clear()


def test_pubsub_webhook_tracks_notified_cursor_without_advancing_processed_cursor(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        owner = _seed_user(db)
        connector = _seed_connector(
            db,
            owner_id=owner.id,
            email_address="owner@gmail.com",
            history_id=100,
        )
        connector_id = connector.id

    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(email_webhooks_pubsub, "validate_pubsub_token", lambda _auth: True)
    monkeypatch.setattr(email_providers, "get_provider", lambda _provider: fake_provider)

    client = TestClient(api_app)
    try:
        response = client.post(
            "/email/webhook/google/pubsub",
            headers={"Authorization": "Bearer valid"},
            json=_pubsub_payload(email_address="owner@gmail.com", history_id=101),
        )

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        assert response.json()["connector_id"] == connector_id

        with SessionLocal() as db:
            refreshed = db.get(Connector, connector_id)
            assert refreshed is not None
            assert refreshed.config["history_id"] == 100
            assert refreshed.config["last_notified_history_id"] == 101
    finally:
        api_app.dependency_overrides.clear()


def test_pubsub_webhook_ignores_invalid_history_cursor(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        owner = _seed_user(db)
        connector = _seed_connector(
            db,
            owner_id=owner.id,
            email_address="owner@gmail.com",
            history_id=100,
        )
        connector_id = connector.id

    api_app.dependency_overrides[get_db] = _override_db(SessionLocal)
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(email_webhooks_pubsub, "validate_pubsub_token", lambda _auth: True)
    monkeypatch.setattr(email_providers, "get_provider", lambda _provider: fake_provider)

    client = TestClient(api_app)
    try:
        response = client.post(
            "/email/webhook/google/pubsub",
            headers={"Authorization": "Bearer valid"},
            json=_pubsub_payload(email_address="owner@gmail.com", history_id="not-a-number"),
        )

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"

        with SessionLocal() as db:
            refreshed = db.get(Connector, connector_id)
            assert refreshed is not None
            assert refreshed.config["history_id"] == 100
            assert "last_notified_history_id" not in refreshed.config
    finally:
        api_app.dependency_overrides.clear()
