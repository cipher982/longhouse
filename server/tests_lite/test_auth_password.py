"""Tests for password auth user binding in single-tenant mode."""

import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
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


def _get_client(session_factory, refresh_calls=None):
    """Create a TestClient with DB override."""
    from zerg.main import api_app

    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _resolve_local_user(**params):
        with session_factory() as db:
            user = db.query(User).filter(User.email == params["email"]).first()
            existing = (
                db.query(User).filter((User.provider != "service") | User.provider.is_(None)).order_by(User.id).first()
            )
            if user is None and existing is not None and params["require_email_match"]:
                raise HTTPException(
                    status_code=409,
                    detail="Password auth is bound to the configured owner. Existing user does not match OWNER_EMAIL.",
                )
            if user is None and existing is not None and params["adopt_existing"]:
                user = existing
            if user is None:
                user = User(email=params["email"], provider=params["provider"], role=params["role"])
                db.add(user)
                db.commit()
                db.refresh(user)
            else:
                _ = (user.id, user.email, user.display_name, user.avatar_url)
                db.expunge(user)
            return user

    def _create_refresh(**params):
        if refresh_calls is not None:
            refresh_calls.append(params)
        return {"created": True, "exact_replay": False}

    api_app.dependency_overrides[get_db] = _override_db
    with (
        patch("zerg.routers.auth_browser.resolve_local_user", side_effect=_resolve_local_user),
        patch("zerg.routers.auth_browser.create_refresh", side_effect=_create_refresh),
    ):
        yield TestClient(api_app)
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
    """Password login should issue refresh state through the catalog boundary."""
    sf = _make_db(tmp_path)

    settings = SimpleNamespace(
        longhouse_password="secret",
        longhouse_password_hash=None,
        single_tenant=True,
        testing=False,
    )
    refresh_calls: list[dict] = []

    with (
        patch.dict(os.environ, {"OWNER_EMAIL": "alice@example.com"}, clear=False),
        patch("zerg.routers.auth_browser.get_settings", return_value=settings),
    ):
        for client in _get_client(sf, refresh_calls):
            resp = client.post("/auth/password", json={"password": "secret"})
            assert resp.status_code == 200

    assert len(refresh_calls) == 1
    assert refresh_calls[0]["user_id"] == 1
    assert len(refresh_calls[0]["token_hash"]) == 64
