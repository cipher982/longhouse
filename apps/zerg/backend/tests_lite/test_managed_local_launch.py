from __future__ import annotations

import os
from types import SimpleNamespace

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
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User


def _make_db(tmp_path):
    db_path = tmp_path / "test_managed_local_launch.db"
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
    user = User(email="managed-local@test.local", role=UserRole.USER.value)
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


class _FakeDispatcher:
    def __init__(self, verify_exit_code: int = 0):
        self.calls: list[dict] = []
        self.verify_exit_code = verify_exit_code

    async def dispatch_job(self, *, db, owner_id, runner_id, command, timeout_secs, commis_id, run_id):
        self.calls.append(
            {
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
            }
        )
        if command.startswith("tmux has-session"):
            return {
                "ok": True,
                "data": {
                    "exit_code": self.verify_exit_code,
                    "stdout": "",
                    "stderr": "" if self.verify_exit_code == 0 else "failed to find session",
                },
            }
        if command.startswith("tmux display-message"):
            return {
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": "claude",
                    "stderr": "",
                },
            }
        return {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            },
        }


def test_launch_managed_local_session_creates_session_and_dispatches_tmux(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher()

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                    "project": "hiring",
                    "display_name": "Hiring session",
                    "loop_mode": "assist",
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["execution_home"] == "managed_local"
            assert payload["managed_transport"] == "tmux"
            assert payload["loop_mode"] == "assist"
            assert payload["source_runner_name"] == "cinder"
            assert payload["attach_command"].startswith("tmux attach -t lh-Hiring-session-")

            session = db.query(AgentSession).filter(AgentSession.id == payload["session_id"]).one()
            assert session.execution_home == "managed_local"
            assert session.managed_transport == "tmux"
            assert session.source_runner_id == runner.id
            assert session.source_runner_name == runner.name
            assert session.provider_session_id == payload["provider_session_id"]
            assert session.managed_session_name == payload["managed_session_name"]
            assert session.continuation_kind == "local"
            assert session.origin_label == runner.name

            assert len(dispatcher.calls) == 4
            assert dispatcher.calls[0]["runner_id"] == runner.id
            assert "command -v tmux" in dispatcher.calls[0]["command"]
            assert "command -v claude-code" in dispatcher.calls[0]["command"]
            assert "tmux new-session -d -s" in dispatcher.calls[1]["command"]
            assert "claude-code --session-id" in dispatcher.calls[1]["command"]
            assert dispatcher.calls[2]["command"] == f"tmux has-session -t {session.managed_session_name}"
            assert (
                dispatcher.calls[3]["command"]
                == f"tmux display-message -p -t {session.managed_session_name} '#{{pane_current_command}}'"
            )
        finally:
            api_app_ref.dependency_overrides = {}


def test_launch_managed_local_session_rolls_back_when_tmux_verify_fails(monkeypatch, tmp_path):
    session_local = _make_db(tmp_path)

    with session_local() as db:
        user, runner = _seed_user_and_runner(db)
        client, api_app_ref = _make_client(db, user)
        dispatcher = _FakeDispatcher(verify_exit_code=1)

        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_connection_manager",
            lambda: SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        )
        monkeypatch.setattr(
            "zerg.services.managed_local_launcher.get_runner_job_dispatcher",
            lambda: dispatcher,
        )

        try:
            response = client.post(
                "/api/sessions/managed-local",
                json={
                    "runner_target": runner.name,
                    "cwd": "/Users/davidrose/git/zeta/hiring",
                },
            )
            assert response.status_code == 502, response.text
            assert "failed to find session" in response.json()["detail"]
            assert db.query(AgentSession).count() == 0
            assert len(dispatcher.calls) == 4
            assert dispatcher.calls[-1]["command"].startswith("tmux kill-session -t lh-hiring-")
        finally:
            api_app_ref.dependency_overrides = {}
