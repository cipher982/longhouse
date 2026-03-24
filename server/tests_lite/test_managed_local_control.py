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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_control import await_managed_local_presence_update
from zerg.services.managed_local_control import await_managed_local_turn_events
from zerg.services.managed_local_control import build_managed_local_claude_ship_command
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_control import ship_managed_local_claude_transcript


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_control.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user_runner_and_session(db, *, provider: str = "claude"):
    user = User(email="managed-local-control@test.local", role=UserRole.USER.value)
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
        provider=provider,
        environment="development",
        project="zerg",
        device_id=runner.name,
        cwd="/Users/davidrose/git/zerg",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
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
        managed_session_name="lh-zerg-managed-local",
        managed_tmux_tmpdir="/tmp/lh-managed-control",
        loop_mode="manual",
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
                "stdout": "",
                "stderr": "",
            },
        }


def test_send_text_to_managed_local_session_emits_thinking_runtime_signal_for_claude(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")
        existing_event = AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="baseline",
            timestamp=datetime.now(timezone.utc),
        )
        db.add(existing_event)
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-control-test",
            )
        )
        assert result.ok is True
        assert result.baseline_event_id == existing_event.id

        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert runtime_state.phase == "thinking"
        assert runtime_state.phase_source == "semantic"
        assert runtime_state.last_runtime_signal_at is not None
        assert runtime_state.device_id == runner.name

        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-control-test"
        assert "export TMUX_TMPDIR=/tmp/lh-managed-control" in str(dispatcher.calls[0]["command"])
        assert "send-keys -t lh-zerg-managed-local -l -- continue" in str(dispatcher.calls[0]["command"])


def test_await_managed_local_turn_events_returns_new_persisted_events(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        baseline = AgentEvent(
            session_id=session.id,
            role="assistant",
            content_text="before",
            timestamp=datetime.now(timezone.utc),
        )
        db.add(baseline)
        db.commit()

        async def _insert_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                event_db.add(
                    AgentEvent(
                        session_id=session.id,
                        role="assistant",
                        content_text="after",
                        timestamp=datetime.now(timezone.utc),
                    )
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_events(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_event_id=baseline.id,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        events = asyncio.run(_run_wait())
        assert [event.content_text for event in events] == ["after"]


def test_send_text_to_managed_local_session_uses_bracketed_paste_for_codex(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="codex")

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
            )
        )

        assert result.ok is True
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        command = str(dispatcher.calls[0]["command"])
        assert "set-buffer -b send-lh-zerg-managed-local continue" in command
        assert "paste-buffer -dpr -b send-lh-zerg-managed-local -t lh-zerg-managed-local" in command
        assert "send-keys -t lh-zerg-managed-local Enter" in command


def test_send_text_to_managed_local_session_supports_repeated_claude_sends(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")

        first = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue alpha",
                commis_id="managed-local-control-first",
            )
        )
        second = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="status? [ok]",
                commis_id="managed-local-control-second",
            )
        )

        assert first.ok is True
        assert second.ok is True
        assert len(dispatcher.calls) == 2
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[1]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-control-first"
        assert dispatcher.calls[1]["commis_id"] == "managed-local-control-second"
        first_command = str(dispatcher.calls[0]["command"])
        second_command = str(dispatcher.calls[1]["command"])
        assert "send-keys -t lh-zerg-managed-local -l --" in first_command
        assert "continue alpha" in first_command
        assert "send-keys -t lh-zerg-managed-local -l --" in second_command
        assert "status? [ok]" in second_command

        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert runtime_state.phase == "thinking"
        assert runtime_state.phase_source == "semantic"
        assert runtime_state.last_runtime_signal_at is not None
        assert runtime_state.device_id == runner.name


def test_build_managed_local_claude_ship_command_targets_exact_transcript(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.provider_session_id = "b0c72633-c8b1-46a4-a42a-53a388b69147"
        db.commit()

        command = build_managed_local_claude_ship_command(session=session)

        assert "command -v longhouse-engine" in command
        assert "$HOME/.claude/projects/-Users-davidrose-git-zerg/b0c72633-c8b1-46a4-a42a-53a388b69147.jsonl" in command
        assert f'--session-id {session.id}' in command
        assert "for delay in 0 1 2 4 8" in command
        assert "--json" in command
        assert "events_shipped" in command
        assert "Managed local Claude transcript did not ship new events" in command


def test_ship_managed_local_claude_transcript_dispatches_runner_job(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.provider_session_id = "b0c72633-c8b1-46a4-a42a-53a388b69147"
        db.commit()

        result = asyncio.run(
            ship_managed_local_claude_transcript(
                db=db,
                owner_id=user.id,
                session=session,
                commis_id="managed-local-claude-ship",
            )
        )

        assert result.ok is True
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-claude-ship"
        command = str(dispatcher.calls[0]["command"])
        assert "command -v longhouse-engine" in command
        assert "$HOME/.claude/projects/-Users-davidrose-git-zerg/b0c72633-c8b1-46a4-a42a-53a388b69147.jsonl" in command
        assert f'--session-id {session.id}' in command
        assert "--json" in command


def test_send_text_to_managed_local_session_can_require_persisted_events(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_events(*, db_bind, session_id, after_event_id, timeout_secs, poll_interval_secs=1.0):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_event_id >= 0
        return [
            AgentEvent(
                id=after_event_id + 1,
                session_id=session_id,
                role="assistant",
                content_text="verified",
                timestamp=datetime.now(timezone.utc),
            )
        ]

    monkeypatch.setattr("zerg.services.managed_local_control.await_managed_local_turn_events", _fake_wait_for_events)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-control-verified",
                verify_turn_started=True,
                verification_timeout_secs=2.5,
            )
        )

        assert result.ok is True
        assert result.verified_turn_started is True
        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert runtime_state.phase == "thinking"
        assert runtime_state.device_id == runner.name


def test_send_text_to_managed_local_session_reports_verification_failure_without_runtime_signal(
    monkeypatch, tmp_path
):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_events(**_kwargs):
        return []

    monkeypatch.setattr("zerg.services.managed_local_control.await_managed_local_turn_events", _fake_wait_for_events)

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="claude")

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-control-verify-fail",
                verify_turn_started=True,
                verification_timeout_secs=1.0,
            )
        )

        assert result.ok is False
        assert result.verified_turn_started is False
        assert result.error == "Managed local session did not produce new timeline events after send"
        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).all()
        assert runtime_state == []


def test_await_managed_local_presence_update_returns_newer_row(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="codex")
        baseline = datetime.now(timezone.utc)
        db.add(
            SessionPresence(
                session_id=str(session.id),
                state="idle",
                provider="codex",
                cwd=session.cwd,
                project=session.project,
                updated_at=baseline,
            )
        )
        db.commit()

        async def _update_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                row = event_db.query(SessionPresence).filter(SessionPresence.session_id == str(session.id)).one()
                row.state = "thinking"
                row.updated_at = datetime.now(timezone.utc)
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_update_later())
            try:
                return await await_managed_local_presence_update(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_updated_at=baseline,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        row = asyncio.run(_run_wait())
        assert row is not None
        assert row.state == "thinking"


def test_send_text_to_managed_local_session_verifies_codex_via_presence(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_presence(*, db_bind, session_id, after_updated_at, timeout_secs, poll_interval_secs=1.0):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_updated_at is not None
        return SessionPresence(
            session_id=str(session_id),
            state="thinking",
            provider="codex",
            updated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_presence_update",
        _fake_wait_for_presence,
    )

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="codex")
        db.add(
            SessionPresence(
                session_id=str(session.id),
                state="idle",
                provider="codex",
                cwd=session.cwd,
                project=session.project,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-codex-verified",
                verify_turn_started=True,
                verification_timeout_secs=2.5,
            )
        )

        assert result.ok is True
        assert result.verified_turn_started is True
        runtime_state = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session.id).one()
        assert runtime_state.phase == "thinking"
        assert runtime_state.device_id == runner.name


def test_send_text_to_managed_local_session_reports_codex_presence_verification_failure(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_presence(**_kwargs):
        return None

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_presence_update",
        _fake_wait_for_presence,
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="codex")
        db.add(
            SessionPresence(
                session_id=str(session.id),
                state="idle",
                provider="codex",
                cwd=session.cwd,
                project=session.project,
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-codex-verify-fail",
                verify_turn_started=True,
                verification_timeout_secs=1.0,
            )
        )

        assert result.ok is False
        assert result.verified_turn_started is False
        assert result.error == "Managed local Codex session did not acknowledge the prompt after send"
