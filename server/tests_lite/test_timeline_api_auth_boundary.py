from __future__ import annotations

import time
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

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
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.services.managed_local_tmux import build_tmux_attach_command
from zerg.services.managed_local_transport import build_managed_local_attach_command


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
        return_value=type("S", (), {"auth_disabled": False})(),
    )


def test_timeline_sessions_accept_browser_session_cookie(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/timeline/sessions")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["sessions"][0]["thread_id"] == session_id
        assert payload["sessions"][0]["head"]["project"] == "timeline-auth"
        assert payload["sessions"][0]["detail"]["project"] == "timeline-auth"
        assert "list_threads;dur=" in response.headers["server-timing"]
        assert "build_cards;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_filters_use_cache_control_and_cache_hit_timing(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            first = client.get("/timeline/filters?days_back=14")
            second = client.get("/timeline/filters?days_back=14")

        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, max-age=60"
        assert "distinct_filters;dur=" in first.headers["server-timing"]

        assert second.status_code == 200
        assert second.headers["cache-control"] == "private, max-age=60"
        assert "cache_hit;dur=" in second.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_managed_local_tmux(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="tmux",
            source_runner_id=9,
            source_runner_name="cinder",
            managed_session_name="lh-codex-managed-local",
            managed_launch_profile={
                "required_commands": ["codex"],
                "exported_env_keys": ["LONGHOUSE_MANAGED_SESSION_ID"],
                "argv": ["codex", "--enable", "codex_hooks", "--no-alt-screen"],
            },
        )
        db.add(session)
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_home"] == "managed_local"
        assert payload["source_runner_name"] == "cinder"
        assert payload["attach_command"] == build_tmux_attach_command(session_name="lh-codex-managed-local")
        assert payload["managed_launch_profile"] == {
            "required_commands": ["codex"],
            "exported_env_keys": ["LONGHOUSE_MANAGED_SESSION_ID"],
            "argv": ["codex", "--enable", "codex_hooks", "--no-alt-screen"],
        }
        assert "load_session;dur=" in response.headers["server-timing"]
        assert "build_response;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_native_claude_bridge(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session = AgentSession(
            id=uuid4(),
            provider="claude",
            environment="development",
            project="timeline-auth",
            device_id="work-laptop",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            provider_session_id="provider-123",
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            source_runner_id=9,
            source_runner_name="work-laptop",
        )
        db.add(session)
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_home"] == "managed_local"
        assert payload["source_runner_name"] == "work-laptop"
        assert payload["attach_command"] == build_managed_local_attach_command(session=session)
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_ignores_malformed_managed_launch_profile(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="tmux",
            source_runner_id=9,
            source_runner_name="cinder",
            managed_session_name="lh-codex-managed-local",
            managed_launch_profile={"argv": "bad-shape", "required_commands": ["codex"]},
        )
        db.add(session)
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["managed_launch_profile"] is None
        assert payload["attach_command"] == build_tmux_attach_command(session_name="lh-codex-managed-local")
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_workspace_bootstraps_session_thread_and_projection(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session_id = _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}/workspace?limit=50")

        assert response.status_code == 200
        payload = response.json()
        assert response.headers["cache-control"] == "private, max-age=5"
        assert payload["session"]["id"] == session_id
        assert payload["thread"]["head_session_id"] == session_id
        assert payload["thread"]["sessions"][0]["id"] == session_id
        assert payload["projection"]["focus_session_id"] == session_id
        assert payload["projection"]["total"] == 0
        assert "load_thread;dur=" in response.headers["server-timing"]
        assert "load_projection;dur=" in response.headers["server-timing"]
        assert "build_projection;dur=" in response.headers["server-timing"]
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_session_detail_includes_attach_command_for_native_managed_local_codex(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="timeline-auth",
            device_id="cinder",
            cwd="/tmp/timeline-auth",
            git_repo=None,
            git_branch="main",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="codex_app_server",
            source_runner_id=9,
            source_runner_name="cinder",
        )
        db.add(session)
        db.commit()
        session_id = str(session.id)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get(f"/timeline/sessions/{session_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["execution_home"] == "managed_local"
        assert payload["managed_transport"] == "codex_app_server"
        assert payload["attach_command"] == build_managed_local_attach_command(session=session)
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


def test_agents_sessions_reject_bearer_device_token_without_agents_header(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"Authorization": "Bearer zdt_fake"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_legacy_non_device_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"X-Agents-Token": "legacy-token"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or revoked device token"
    finally:
        api_app.dependency_overrides.clear()
