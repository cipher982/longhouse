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
from zerg.models.agents import AgentsBase
from zerg.models.work import INSIGHT_ORIGIN_SYSTEM
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


def _seed_insight(db, *, title: str = "Insight title", origin: str | None = None) -> Insight:
    insight = Insight(
        insight_type="learning",
        title=title,
        project="zerg",
        description="Useful note",
        origin=origin,
        severity="info",
    )
    db.add(insight)
    db.commit()
    db.refresh(insight)
    return insight


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
        assert payload["insights"][0]["origin"] == "manual"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_insights_list_requires_agents_token_not_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/agents/insights")
        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_insights_list_accepts_agents_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db)

    client = _make_client(session_local)
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/insights", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["insights"][0]["title"] == "Insight title"
        assert payload["insights"][0]["origin"] == "manual"
    finally:
        api_app.dependency_overrides.clear()


def test_insights_list_hides_system_rows_but_keeps_legacy_rows(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db, title="Manual insight")
        legacy = _seed_insight(db, title="Legacy insight")
        _seed_insight(db, title="System insight", origin=INSIGHT_ORIGIN_SYSTEM)
        db.execute(text("UPDATE insights SET origin = NULL WHERE id = :id"), {"id": str(legacy.id)})
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/insights")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        titles = {row["title"] for row in payload["insights"]}
        assert titles == {"Manual insight", "Legacy insight"}
        origins = {row["title"]: row["origin"] for row in payload["insights"]}
        assert origins["Manual insight"] == "manual"
        assert origins["Legacy insight"] is None
    finally:
        api_app.dependency_overrides.clear()


def test_agents_insights_list_can_include_system_rows(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db, title="Manual insight")
        _seed_insight(db, title="System insight", origin=INSIGHT_ORIGIN_SYSTEM)

    client = _make_client(session_local)
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/insights?include_system=true", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        origins = {row["title"]: row["origin"] for row in payload["insights"]}
        assert origins == {
            "Manual insight": "manual",
            "System insight": "system",
        }
    finally:
        api_app.dependency_overrides.clear()


def test_insights_list_hides_archived_rows_by_default(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db, title="Active insight")
        archived = _seed_insight(db, title="Archived insight")
        db.execute(
            text("UPDATE insights SET archived_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc).isoformat(), "id": str(archived.id)},
        )
        db.commit()

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/insights")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["insights"][0]["title"] == "Active insight"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_insights_list_can_include_archived_rows(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_insight(db, title="Active insight")
        archived = _seed_insight(db, title="Archived insight")
        db.execute(
            text("UPDATE insights SET archived_at = :ts WHERE id = :id"),
            {"ts": datetime.now(timezone.utc).isoformat(), "id": str(archived.id)},
        )
        db.commit()

    client = _make_client(session_local)
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/insights", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["insights"][0]["title"] == "Active insight"

        with _force_agents_token_mode():
            response = client.get("/agents/insights?include_archived=true", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        archived_rows = {row["title"]: row["archived_at"] for row in payload["insights"]}
        assert archived_rows["Active insight"] is None
        assert archived_rows["Archived insight"] is not None
    finally:
        api_app.dependency_overrides.clear()


def test_insights_archive_and_unarchive_require_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        insight = _seed_insight(db, title="Archive me")
        insight_id = str(insight.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.post(f"/insights/{insight_id}/archive", headers={"X-Agents-Token": "dev"})
        assert response.status_code == 401

        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.post(f"/insights/{insight_id}/archive")
        assert response.status_code == 200
        assert response.json()["archived_at"] is not None

        with _force_browser_jwt_mode():
            response = client.post(f"/insights/{insight_id}/unarchive")
        assert response.status_code == 200
        assert response.json()["archived_at"] is None
    finally:
        api_app.dependency_overrides.clear()


def test_insights_create_sets_manual_origin(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)

    client = _make_client(session_local)
    api_app.dependency_overrides[verify_agents_token] = lambda: None

    try:
        with _force_agents_token_mode():
            response = client.post(
                "/insights",
                headers={"X-Agents-Token": "dev"},
                json={
                    "insight_type": "learning",
                    "title": "Created via API",
                    "project": "zerg",
                    "description": "Fresh note",
                },
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["origin"] == "manual"
        with session_local() as db:
            insight = db.query(Insight).filter(Insight.title == "Created via API").first()
            assert insight is not None
            assert insight.origin == "manual"
    finally:
        api_app.dependency_overrides.clear()
