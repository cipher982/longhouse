from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_codex_exec import build_codex_exec_resume_command
from zerg.services.managed_local_codex_exec import run_codex_exec_resume_for_managed_local_session


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_codex_exec.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user_runner_and_session(db):
    user = User(email="managed-local-codex-exec@test.local", role=UserRole.USER.value)
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

    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="development",
        project="zerg",
        device_id=runner.name,
        cwd="/Users/davidrose/git/zerg",
        started_at=datetime.now(timezone.utc),
        provider_session_id="019c638d-0000-0000-0000-000000000999",
        thread_root_session_id=None,
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name="lh-zerg-managed-local-codex",
        managed_tmux_tmpdir="/tmp/lh-managed-control",
        loop_mode="assist",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return user, runner, session


class _FakeDispatcher:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    async def dispatch_job(self, *, db, owner_id, runner_id, command, timeout_secs, commis_id, run_id):
        self.calls.append(
            {
                "owner_id": owner_id,
                "runner_id": runner_id,
                "command": command,
                "timeout_secs": timeout_secs,
                "commis_id": commis_id,
            }
        )
        return {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "__LONGHOUSE_CODEX_EXEC_STARTED__\n",
                "stderr": "",
            },
        }


def test_build_codex_exec_resume_command_uses_mapping_file_and_launches_detached_exec():
    command = build_codex_exec_resume_command(
        session_id="11111111-2222-3333-4444-555555555555",
        cwd="/Users/davidrose/git/zerg",
        prompt="continue with targeted verification",
    )

    assert "codex exec resume --json --skip-git-repo-check --full-auto" in command
    assert "export LONGHOUSE_SESSION_ID=11111111-2222-3333-4444-555555555555" in command
    assert 'MAPPING_FILE="$MANAGED_DIR/$LONGHOUSE_SESSION_ID.codex-session-id"' in command
    assert 'NATIVE_SESSION_ID=$(tr -d' in command
    assert "nohup env LONGHOUSE_SESSION_ID=" in command
    assert "longhouse-engine ship --file" in command
    assert '--provider codex --session-id "$LONGHOUSE_SESSION_ID"' in command
    assert "working directory does not exist" in command
    assert "continue with targeted verification" in command


def test_run_codex_exec_resume_for_managed_local_session_marks_thinking_runtime_signal(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_codex_exec.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db)

        result = asyncio.run(
            run_codex_exec_resume_for_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-codex-exec-test",
            )
        )
        assert result.ok is True

        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert runtime_state.phase == "thinking"
        assert runtime_state.phase_source == "semantic"
        assert runtime_state.last_runtime_signal_at is not None
        assert runtime_state.device_id == runner.name

        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-codex-exec-test"
        assert dispatcher.calls[0]["timeout_secs"] == 300
        assert "codex exec resume --json --skip-git-repo-check --full-auto" in str(dispatcher.calls[0]["command"])
        assert ".codex-session-id" in str(dispatcher.calls[0]["command"])
        assert "nohup env LONGHOUSE_SESSION_ID=" in str(dispatcher.calls[0]["command"])
        assert f"export LONGHOUSE_SESSION_ID={session.id}" in str(dispatcher.calls[0]["command"])


def test_run_codex_exec_resume_requires_codex_provider(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db)
        session.provider = "claude"
        db.commit()
        db.refresh(session)

        result = asyncio.run(
            run_codex_exec_resume_for_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
            )
        )
        assert result.ok is False
        assert result.error == "Session is not a managed-local Codex session"


def test_run_codex_exec_resume_does_not_require_provider_session_id(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_codex_exec.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db)
        session.provider_session_id = None
        db.commit()
        db.refresh(session)

        result = asyncio.run(
            run_codex_exec_resume_for_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
            )
        )

        assert result.ok is True
        assert len(dispatcher.calls) == 1
