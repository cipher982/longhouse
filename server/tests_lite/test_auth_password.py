"""Tests for password auth user binding in single-tenant mode."""

import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
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
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "alice@example.com"


def test_password_login_rejects_legacy_user_mismatch(tmp_path):
    """Hosted password login should fail closed when the stored user mismatches OWNER_EMAIL."""
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
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 409
            assert resp.json()["detail"] == (
                "Password auth is bound to the configured owner. Existing user does not match OWNER_EMAIL."
            )

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "local@longhouse"


def test_password_login_adopts_existing_local_user_without_explicit_owner_email(tmp_path):
    """B9: upgrading a prior local (no-auth) DB to password auth must not 409.

    A self-hoster who used local mode (creating e.g. local@zerg) and then sets
    LONGHOUSE_PASSWORD_HASH without OWNER_EMAIL should be able to log in; the
    single existing owner is adopted rather than rejected.
    """
    sf = _make_db(tmp_path)

    with sf() as db:
        db.add(User(email="local@zerg", provider="local"))
        db.commit()

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )

    # No OWNER_EMAIL in the environment → synthetic owner default applies, and
    # the existing single user should be adopted instead of a 409.
    env_without_owner = {k: v for k, v in os.environ.items() if k != "OWNER_EMAIL"}
    with (
        patch.dict(os.environ, env_without_owner, clear=True),
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "local@zerg"


def test_password_login_adopts_existing_user_with_null_provider(tmp_path):
    """B9 (Codex round-2): a real user with provider NULL must still be adopted.

    `provider != "service"` excludes NULL in SQL; without the NULL-aware filter
    a pre-existing provider-less owner would be missed and a duplicate created.
    """
    sf = _make_db(tmp_path)

    with sf() as db:
        db.add(User(email="legacy@local", provider=None))
        db.commit()

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )

    env_without_owner = {k: v for k, v in os.environ.items() if k != "OWNER_EMAIL"}
    with (
        patch.dict(os.environ, env_without_owner, clear=True),
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    with sf() as db:
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email == "legacy@local"


def test_password_login_routes_refresh_session_write_through_serializer(tmp_path):
    """Password login should issue the browser refresh session via WriteSerializer."""
    sf = _make_db(tmp_path)

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )
    labels: list[str] = []

    class _FakeSerializer:
        async def execute_or_direct(self, fn, fallback_db, *, label="", auto_commit=True, priority=None):
            labels.append(label)
            result = fn(fallback_db)
            if auto_commit:
                fallback_db.commit()
            return result

    with (
        patch.dict(os.environ, {"OWNER_EMAIL": "alice@example.com"}, clear=False),
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
        patch("zerg.routers.auth_browser.get_write_serializer", return_value=_FakeSerializer()),
    ):
        for client in _get_client(sf):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    assert labels == ["refresh-session"]
