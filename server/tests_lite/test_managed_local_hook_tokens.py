from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.auth.managed_local_hook_tokens import issue_managed_local_hook_token
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.user import User
from zerg.services.session_hot_cards import upsert_timeline_card_from_session


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_hook_tokens.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1) -> User:
    user = User(id=user_id, email="managed-local-hooks@test.local", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(
    db,
    *,
    session_id: str | None = None,
    project: str = "hiring",
    device_id: str = "cinder",
) -> AgentSession:
    sid = session_id or str(uuid4())
    session = AgentSession(
        id=sid,
        provider="claude",
        environment="development",
        project=project,
        device_id=device_id,
        cwd=f"/tmp/{project}",
        git_repo=None,
        git_branch="main",
        started_at=datetime.now(timezone.utc),
                        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    db.refresh(session)
    return session


def _settings_override():
    return type("S", (), {"auth_disabled": False, "testing": True, "single_tenant": True})()


def _make_client(db_session):
    def override_db():
        try:
            yield db_session
        finally:
            pass

    api_app.dependency_overrides[get_db] = override_db
    return TestClient(api_app)


def test_presence_accepts_matching_managed_local_hook_token(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.post(
                    "/agents/presence",
                    json={
                        "session_id": str(session.id),
                        "state": "thinking",
                        "cwd": "/tmp/hiring",
                    },
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 204, response.text
            runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
            assert runtime_state.phase == "thinking"
            assert runtime_state.device_id == "cinder"
        finally:
            api_app.dependency_overrides.clear()


def test_presence_rejects_mismatched_managed_local_hook_token(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.post(
                    "/agents/presence",
                    json={
                        "session_id": str(uuid4()),
                        "state": "thinking",
                        "cwd": "/tmp/hiring",
                    },
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "Managed-local hook token does not match session"
        finally:
            api_app.dependency_overrides.clear()


def test_ingest_accepts_managed_local_hook_token_and_forces_session_scope(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session_id = str(uuid4())
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=session_id,
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.post(
                    "/agents/ingest",
                    json={
                        "provider": "claude",
                        "environment": "development",
                        "project": "hiring",
                        "device_id": "wrong-device",
                        "cwd": "/tmp/hiring",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "events": [],
                    },
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 200, response.text
            assert response.json()["session_id"] == session_id
            session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
            assert session.device_id == "cinder"
        finally:
            api_app.dependency_overrides.clear()


def test_agents_sessions_allows_bounded_project_lookup_for_managed_local_hook_token(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        _seed_session(db, project="other", device_id="cinder")
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.get(
                    "/agents/sessions",
                    params={"project": "hiring", "limit": 5, "days_back": 7},
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["total"] == 1
            assert payload["sessions"][0]["project"] == "hiring"
        finally:
            api_app.dependency_overrides.clear()


def test_agents_sessions_rejects_broader_filters_for_managed_local_hook_token(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                for params in (
                    {"project": "hiring", "limit": 6, "days_back": 7},
                    {"project": "hiring", "limit": 5, "days_back": 7, "query": "hiring"},
                ):
                    response = client.get(
                        "/agents/sessions",
                        params=params,
                        headers={"X-Agents-Token": token},
                    )

                    assert response.status_code == 403, response.text
                    assert response.json()["detail"] == (
                        "Managed-local hook token only supports bounded recent project lookup"
                    )
        finally:
            api_app.dependency_overrides.clear()


def test_startup_context_allows_bounded_project_lookup_for_managed_local_hook_token(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        session.summary = "Recent hiring session."
        session.summary_title = "Hiring work"
        db.commit()
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.get(
                    "/agents/sessions/startup-context",
                    params={"project": "hiring", "limit": 5, "days_back": 7},
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["session_count"] == 1
            assert payload["items"][0]["summary_title"] == "Hiring work"
        finally:
            api_app.dependency_overrides.clear()


def test_managed_local_hook_token_is_rejected_outside_allowed_surfaces(tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user = _seed_user(db)
        session = _seed_session(db, project="hiring", device_id="cinder")
        token = issue_managed_local_hook_token(
            owner_id=user.id,
            session_id=str(session.id),
            project="hiring",
            device_id="cinder",
        )
        client = _make_client(db)

        try:
            with patch("zerg.dependencies.agents_auth.get_settings", _settings_override):
                response = client.get(
                    "/agents/sessions/summary",
                    params={"project": "hiring", "limit": 5, "days_back": 7},
                    headers={"X-Agents-Token": token},
                )

            assert response.status_code == 403, response.text
            assert response.json()["detail"] == "Managed-local hook token is not allowed on this endpoint"
        finally:
            api_app.dependency_overrides.clear()
