from __future__ import annotations

import time
from datetime import datetime
from datetime import timezone
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient

import zerg.dependencies.auth as auth_deps
import zerg.dependencies.agents_auth as agents_auth_deps
from zerg.auth.session_tokens import JWT_SECRET
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
from zerg.models.agents import AgentsBase


def _make_db(tmp_path):
    db_path = tmp_path / "test_timeline_api_auth_boundary.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1) -> User:
    user = User(id=user_id, email="owner@example.com", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(db) -> str:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="timeline-auth",
        device_id="dev-machine",
        cwd="/tmp/timeline-auth",
        git_repo=None,
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.commit()
    return str(session.id)


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {
            "sub": str(user_id),
            "exp": int(time.time()) + 300,
        },
        JWT_SECRET,
    )


def _make_client(session_local) -> TestClient:
    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    return patch.object(auth_deps, "AUTH_DISABLED", False)


def _force_agents_token_mode():
    return patch.object(
        agents_auth_deps,
        "get_settings",
        return_value=type("S", (), {"auth_disabled": False, "agents_api_token": None})(),
    )


def test_timeline_sessions_accept_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/timeline/sessions")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["sessions"][0]["project"] == "timeline-auth"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_sessions_reject_agents_header_without_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get("/timeline/sessions", headers={"X-Agents-Token": "dev"})

        assert response.status_code == 401
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_browser_cookie_without_agents_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode(), _force_agents_token_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/agents/sessions")

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()
