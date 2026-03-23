from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.routers import session_chat


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_session_chat.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _seed_user_and_runner(db):
    user = User(email="managed-local-chat@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    runner = Runner(
        owner_id=user.id,
        name="cinder",
        availability_policy="always_on",
        capabilities=["exec.full"],
        status="online",
        auth_secret_hash="secret-hash",
        runner_metadata={"install_mode": "desktop"},
    )
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return user, runner


def _seed_managed_local_session(db, *, runner: Runner, provider: str = "claude") -> AgentSession:
    session_id = uuid4()
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project="hiring",
        device_id=runner.name,
        cwd="/Users/davidrose/git/zeta/hiring",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
        thread_root_session_id=session_id,
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode="assist",
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name="lh-hiring-chat-1234",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_chat_with_session_routes_managed_local_without_cloud_continuation(monkeypatch, tmp_path, provider):
    session_local = _make_db(tmp_path)
    calls: list[dict[str, object]] = []

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider=provider)
        client, api_app_ref = _make_client(db, user)

        async def fake_wait_for_events(**_kwargs):
            return [
                SimpleNamespace(
                    id=101,
                    role="assistant",
                    content_text="Local tmux reply",
                    tool_name=None,
                    tool_call_id=None,
                )
            ]

        def fail_cloud_target(*_args, **_kwargs):
            raise AssertionError("managed_local chat should not create cloud continuations")

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            calls.append(
                {
                    "owner_id": owner_id,
                    "session_id": str(session.id),
                    "runner_id": session.source_runner_id,
                    "text": text,
                    "commis_id": commis_id,
                    "timeout_secs": timeout_secs,
                }
            )
            return SimpleNamespace(ok=True, exit_code=0, error=None)

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)
        monkeypatch.setattr(
            "zerg.routers.session_chat._await_managed_local_turn_events",
            fake_wait_for_events,
        )
        monkeypatch.setattr(
            session_chat.AgentsStore,
            "ensure_cloud_continuation_target",
            fail_cloud_target,
        )

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Local tmux reply" in body
            assert '"created_continuation": false' in body
            assert f'"session_id": "{source_session.id}"' in body
            assert f'"shipped_session_id": "{source_session.id}"' in body
            runtime_state = (
                db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == source_session.id).one()
            )
            assert runtime_state.phase == "idle"
            assert runtime_state.phase_source == "semantic"
            assert len(calls) == 1
            assert calls[0]["runner_id"] == runner.id
            assert calls[0]["owner_id"] == user.id
            assert calls[0]["session_id"] == str(source_session.id)
            assert calls[0]["text"] == "continue"
        finally:
            api_app_ref.dependency_overrides = {}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_chat_with_session_reports_managed_local_send_failure(monkeypatch, tmp_path, provider):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        source_session = _seed_managed_local_session(db, runner=runner, provider=provider)
        client, api_app_ref = _make_client(db, user)

        async def fake_send_text(*, db, owner_id, session, text, commis_id=None, timeout_secs=15):
            return SimpleNamespace(ok=False, exit_code=None, error="Runner send failed")

        monkeypatch.setattr("zerg.routers.session_chat.send_text_to_managed_local_session", fake_send_text)

        try:
            response = client.post(
                f"/api/sessions/{source_session.id}/chat",
                json={"message": "continue"},
            )
            assert response.status_code == 200, response.text
            body = response.text
            assert "Runner send failed" in body
            assert '"persisted_events": 0' in body
            assert '"created_continuation": false' in body
        finally:
            api_app_ref.dependency_overrides = {}
