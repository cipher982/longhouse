from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

import zerg.dependencies.auth as auth_deps
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
from zerg.models.agents import AgentsBase
from zerg.models.work import ActionProposal
from zerg.models.work import Insight


def _make_db(tmp_path):
    db_path = tmp_path / "test_browser_machine_auth_boundary.db"
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


def _seed_insight(db, *, title: str = "Insight title") -> Insight:
    insight = Insight(
        insight_type="learning",
        title=title,
        project="zerg",
        description="Useful note",
        severity="info",
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    return insight


def _seed_proposal(db, *, insight_id) -> ActionProposal:
    proposal = ActionProposal(
        insight_id=insight_id,
        project="zerg",
        title="Proposal title",
        action_blurb="Do the thing",
        status="pending",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


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


def test_insights_list_requires_browser_session_not_agents_header(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get("/insights", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 401
    finally:
        api_app.dependency_overrides.clear()


def test_insights_list_accepts_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/insights")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["insights"][0]["title"] == "Insight title"
    finally:
        api_app.dependency_overrides.clear()


def test_proposals_routes_require_browser_session_not_agents_header(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        insight = _seed_insight(db)
        _seed_proposal(db, insight_id=insight.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get("/proposals", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 401
    finally:
        api_app.dependency_overrides.clear()


def test_proposals_list_accepts_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        insight = _seed_insight(db)
        _seed_proposal(db, insight_id=insight.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/proposals")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["proposals"][0]["title"] == "Proposal title"
    finally:
        api_app.dependency_overrides.clear()
