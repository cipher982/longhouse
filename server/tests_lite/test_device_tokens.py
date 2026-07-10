"""Tests for device-token validation hot-path behavior."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import _validate_device_token_for_request
from zerg.dependencies.auth import get_current_user
from zerg.main import api_app
from zerg.models.device_token import DeviceToken
from zerg.models.models import User
from zerg.routers.device_tokens import generate_device_token
from zerg.routers.device_tokens import hash_token
from zerg.routers.device_tokens import validate_device_token


def _make_db(tmp_path):
    db_path = tmp_path / "test_device_tokens.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _setup_app(tmp_path):
    factory = _make_db(tmp_path)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def _override_user():
        return SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_current_user] = _override_user

    with factory() as db:
        user = User(id=1, email="alice@example.com", role="ADMIN")
        db.add(user)
        db.commit()

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(get_current_user, None)

    return factory, _cleanup


def test_validate_device_token_skips_last_used_write_when_serializer_configured(tmp_path):
    """Hot-path validation should stay read-only when the write serializer is active."""
    factory = _make_db(tmp_path)
    plain_token = generate_device_token()

    with factory() as db:
        user = User(id=1, email="alice@example.com", role="ADMIN")
        db.add(user)
        db.commit()

        device_token = DeviceToken(
            owner_id=user.id,
            device_id="test-device",
            token_hash=hash_token(plain_token),
        )
        db.add(device_token)
        db.commit()
        token_id = device_token.id

    class _FakeSerializer:
        is_configured = True

    with (
        factory() as db,
        patch("zerg.routers.device_tokens.get_write_serializer", return_value=_FakeSerializer()),
    ):
        validated = validate_device_token(plain_token, db)
        assert validated is not None

    with factory() as db:
        stored = db.query(DeviceToken).filter(DeviceToken.id == token_id).first()
        assert stored is not None
        assert stored.last_used_at is None


def test_agents_token_validation_returns_detached_device_token(tmp_path):
    """Agents auth should not keep its validation DB session checked out."""
    factory = _make_db(tmp_path)
    plain_token = generate_device_token()

    with factory() as db:
        user = User(id=1, email="alice@example.com", role="ADMIN")
        db.add(user)
        db.commit()

        device_token = DeviceToken(
            owner_id=user.id,
            device_id="cinder",
            token_hash=hash_token(plain_token),
        )
        db.add(device_token)
        db.commit()

    class _FakeSerializer:
        is_configured = True

    with (
        patch("zerg.dependencies.agents_auth.get_session_factory", return_value=factory),
        patch("zerg.routers.device_tokens.get_write_serializer", return_value=_FakeSerializer()),
    ):
        validated = _validate_device_token_for_request(plain_token)

    assert validated is not None
    assert validated.owner_id == 1
    assert validated.device_id == "cinder"
    assert sa_inspect(validated).detached


def test_create_device_token_routes_write_through_serializer(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    serializer_labels: list[str] = []

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db, *, label="", auto_commit=True):
            serializer_labels.append(label)
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    with patch("zerg.routers.device_tokens.get_write_serializer", return_value=_FakeSerializer()):
        client = TestClient(api_app)
        response = client.post("/devices/tokens", json={"device_id": "macbook"})

    assert response.status_code == 201, response.text
    assert serializer_labels == ["device-token-create"]

    with factory() as db:
        token = db.query(DeviceToken).filter(DeviceToken.device_id == "macbook").first()
        assert token is not None
        assert token.created_at is not None
        assert token.revoked_at is None

    cleanup()


def test_revoke_device_token_routes_write_through_serializer(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    serializer_labels: list[str] = []

    with factory() as db:
        token = DeviceToken(owner_id=1, device_id="macbook", token_hash=hash_token(generate_device_token()))
        db.add(token)
        db.commit()
        token_id = token.id

    class _FakeSerializer:
        is_configured = True

        async def execute_or_direct(self, fn, fallback_db, *, label="", auto_commit=True):
            serializer_labels.append(label)
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    with patch("zerg.routers.device_tokens.get_write_serializer", return_value=_FakeSerializer()):
        client = TestClient(api_app)
        response = client.delete(f"/devices/tokens/{token_id}")

    assert response.status_code == 204, response.text
    assert serializer_labels == ["device-token-revoke"]

    with factory() as db:
        stored = db.query(DeviceToken).filter(DeviceToken.id == token_id).first()
        assert stored is not None
        assert stored.revoked_at is not None

    cleanup()
