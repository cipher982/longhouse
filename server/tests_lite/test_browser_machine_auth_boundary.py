from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

import zerg.dependencies.agents_auth as agents_auth_deps
import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import JWT_SECRET
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.auth.session_tokens import _encode_jwt
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models import User


def _make_db(tmp_path):
    db_path = tmp_path / "test_browser_machine_auth_boundary.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1) -> User:
    user = User(id=user_id, email="owner@example.com", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {
            "sub": str(user_id),
            "exp": int(time.time()) + 300,
        },
        JWT_SECRET,
    )


def _make_client(session_local: object) -> TestClient:
    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


@contextmanager
def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    with (
        patch.object(auth_deps, "AUTH_DISABLED", False),
        patch("zerg.routers.auth_browser.get_settings", return_value=type("S", (), {"auth_disabled": False})()),
    ):
        yield
    auth_deps._strategy_cache.clear()


@contextmanager
def _force_agents_token_mode():
    with patch.object(
        agents_auth_deps,
        "get_settings",
        return_value=type("S", (), {"auth_disabled": False, "agents_api_token": None})(),
    ):
        yield


def test_auth_status_ignores_bearer_token_without_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    client = _make_client(session_local)
    bearer = _issue_session_cookie()

    try:
        with _force_browser_jwt_mode():
            response = client.get("/auth/status", headers={"Authorization": f"Bearer {bearer}"})
        assert response.status_code == 200
        assert response.json() == {"authenticated": False, "user": None}
    finally:
        api_app.dependency_overrides.clear()


def test_auth_verify_rejects_bearer_token_without_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    client = _make_client(session_local)
    bearer = _issue_session_cookie()

    try:
        with _force_browser_jwt_mode():
            response = client.get("/auth/verify", headers={"Authorization": f"Bearer {bearer}"})
        assert response.status_code == 401
    finally:
        api_app.dependency_overrides.clear()
