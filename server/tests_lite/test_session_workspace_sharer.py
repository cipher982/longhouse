"""Tests for the ?shared_by=<user_id> resolution on the timeline session workspace endpoint.

The endpoint resolves ``shared_by`` to a small ``SessionSharerResponse`` and
embeds it in the returned session payload. The pill on the client is the only
consumer. These tests pin the contract:

- absent param → no ``sharer``
- resolves to the named user → ``sharer`` populated
- resolves to a deleted user → ``sharer`` is null (no 500)
- resolves to the current viewer → ``sharer`` is null (your own copy of your
  own link does not need a "Shared by" pill)
- param is non-positive or non-integer → 422
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

import zerg.dependencies.auth as auth_deps
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.auth.session_tokens import _encode_jwt
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_workspace_sharer.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int, email: str, display_name: str | None) -> User:
    user = User(id=user_id, email=email, display_name=display_name, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(db) -> str:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="sharer-test",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    return str(session.id)


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {"sub": str(user_id), "exp": int(time.time()) + 300},
        auth_deps.get_settings().jwt_secret,
    )


def _make_client(session_local) -> "TestClient":
    from fastapi.testclient import TestClient

    api_app.dependency_overrides.clear()

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    from unittest.mock import patch

    return patch.object(auth_deps, "AUTH_DISABLED", False)


def test_workspace_sharer_absent_when_param_missing(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        _seed_user(db, user_id=2, email="sharer@example.com", display_name="David Rose")
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10")

        assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
        assert response.json()["session"]["sharer"] is None
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_populated_when_user_resolves(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        _seed_user(db, user_id=2, email="sharer@example.com", display_name="David Rose")
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=2")

        assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
        sharer = response.json()["session"]["sharer"]
        assert sharer == {"id": 2, "display_name": "David Rose"}
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_null_when_user_deleted(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        # user_id=42 referenced by the URL but never seeded
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=42")

        assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
        assert response.json()["session"]["sharer"] is None
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_null_when_self(tmp_path):
    """Visiting your own shared link must not render a 'Shared by yourself' pill."""
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="david@example.com", display_name="David Rose")
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=1")

        assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
        assert response.json()["session"]["sharer"] is None
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_handles_blank_display_name(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        _seed_user(db, user_id=7, email="david010@example.com", display_name=None)
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=7")

        assert response.status_code == 200, f"Got {response.status_code}: {response.text}"
        sharer = response.json()["session"]["sharer"]
        # Display name is null, not the email — the client falls back to the
        # email local part via the user table lookup it already has.
        assert sharer == {"id": 7, "display_name": None}
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_rejects_non_positive_param(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=0")

        assert response.status_code == 422
    finally:
        api_app.dependency_overrides.clear()


def test_workspace_sharer_rejects_non_integer_param(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db, user_id=1, email="viewer@example.com", display_name="Viewer")
        session_id = _seed_session(db)
    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie(user_id=1))
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=10&shared_by=not-a-number")

        # FastAPI validates Optional[int] and rejects non-integers with 422.
        assert response.status_code == 422
    finally:
        api_app.dependency_overrides.clear()
