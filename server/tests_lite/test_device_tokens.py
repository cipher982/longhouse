"""Tests for device-token validation hot-path behavior."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
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
        patch("zerg.services.write_serializer.get_write_serializer", return_value=_FakeSerializer()),
    ):
        validated = validate_device_token(plain_token, db)
        assert validated is not None

    with factory() as db:
        stored = db.query(DeviceToken).filter(DeviceToken.id == token_id).first()
        assert stored is not None
        assert stored.last_used_at is None
