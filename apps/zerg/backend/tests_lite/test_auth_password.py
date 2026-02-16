"""Tests for password auth user binding in single-tenant mode."""

from types import SimpleNamespace
from unittest.mock import patch
import os

from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models import User


def _make_db(tmp_path):
    """Create a SQLite DB with all tables, return session factory."""
    db_path = tmp_path / "test_auth_password.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _get_client(session_factory):
    """Create a TestClient with DB override."""
    from zerg.main import api_app

    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    client = TestClient(api_app)
    yield client
    api_app.dependency_overrides.clear()


def test_password_login_binds_owner_email(tmp_path):
    """Hosted instances should bind password login to OWNER_EMAIL."""
    sf = _make_db(tmp_path)

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )

    with (
        patch.dict(os.environ, {"OWNER_EMAIL": "alice@example.com"}, clear=False),
        patch("zerg.routers.auth.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "alice@example.com"


def test_password_login_migrates_legacy_user(tmp_path):
    """Legacy local@longhouse user should be migrated to OWNER_EMAIL."""
    sf = _make_db(tmp_path)

    with sf() as db:
        db.add(User(email="local@longhouse", provider="password"))
        db.commit()

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )

    with (
        patch.dict(os.environ, {"OWNER_EMAIL": "owner@example.com"}, clear=False),
        patch("zerg.routers.auth.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "owner@example.com"
