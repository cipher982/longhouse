"""SQLite round-trip tests for models using GUID TypeDecorator."""

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.device_token import DeviceToken
from zerg.models.models import User


def test_device_token_roundtrip_sqlite(tmp_path):
    """DeviceToken with GUID primary key works on SQLite."""
    db_path = tmp_path / "device_token.db"
    engine = make_engine(f"sqlite:///{db_path}")
    # Strip schema for SQLite (models use schema="zerg" for Postgres)
    engine = engine.execution_options(schema_translate_map={"zerg": None, "agents": None})

    # Create all tables
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        # Create a user first (DeviceToken has FK to users)
        user = User(
            email="test@example.com",
            provider="dev",
            provider_user_id="test-1",
            display_name="Test User",
            role="USER",
            is_active=True,
        )
        db.add(user)
        db.flush()

        # Create device token with explicit UUID
        token_id = uuid4()
        token = DeviceToken(
            id=token_id,
            owner_id=user.id,
            device_id="test-device",
            token_hash="a" * 64,  # Fake SHA-256 hash
        )
        db.add(token)
        db.commit()

        # Verify round-trip
        loaded = db.query(DeviceToken).filter(DeviceToken.id == token_id).first()
        assert loaded is not None
        assert loaded.id == token_id
        assert loaded.device_id == "test-device"
        assert loaded.owner_id == user.id

        # Verify GUID is stored as string in SQLite
        result = db.execute(text("SELECT typeof(id) FROM device_tokens")).fetchone()
        assert result[0] == "text"  # SQLite stores as text


def test_device_token_default_uuid_sqlite(tmp_path):
    """DeviceToken generates UUID automatically when not provided."""
    db_path = tmp_path / "device_token_default.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"zerg": None, "agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        user = User(
            email="test@example.com",
            provider="dev",
            provider_user_id="test-1",
            display_name="Test User",
            role="USER",
            is_active=True,
        )
        db.add(user)
        db.flush()

        # Create without explicit ID - should auto-generate
        token = DeviceToken(
            owner_id=user.id,
            device_id="auto-device",
            token_hash="b" * 64,
        )
        db.add(token)
        db.commit()

        # ID should be auto-generated UUID
        assert token.id is not None
        assert len(str(token.id)) == 36  # UUID string format
