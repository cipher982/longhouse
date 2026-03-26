from __future__ import annotations

import asyncio
import os
from collections import deque
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
from zerg.models.agents import SessionRuntimeEvent
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_control import await_managed_local_hook_phase_update
from zerg.services.managed_local_control import await_managed_local_presence_update
from zerg.services.managed_local_control import await_managed_local_turn_events
from zerg.services.managed_local_control import await_managed_local_turn_terminal
from zerg.services.managed_local_control import build_managed_local_claude_ship_command
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_control import ship_managed_local_claude_transcript
from zerg.services.managed_local_control import validate_managed_local_chat_done_payload
from zerg.services.managed_local_ship_retry import MANAGED_LOCAL_CLAUDE_SHIP_WAIT_READY_MS
from zerg.services.presence_cache import get_presence_cache


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
        self.results: deque[dict[str, object]] | None = None

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
        if self.results:
            return self.results.popleft()
        return {
            "ok": True,
            "data": {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
            },
        }


def test_send_text_to_managed_local_session_returns_baseline_event_id_for_claude(monkeypatch, tmp_path):
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


def test_build_managed_local_claude_ship_command_targets_exact_transcript(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.provider_session_id = "b0c72633-c8b1-46a4-a42a-53a388b69147"
        db.commit()

        command = build_managed_local_claude_ship_command(session=session)

        assert "command -v longhouse-engine" in command
        assert "$HOME/.claude/projects/-Users-davidrose-git-zerg/b0c72633-c8b1-46a4-a42a-53a388b69147.jsonl" in command
        assert f"--session-id {session.id}" in command
        assert f"--wait-ready-ms {MANAGED_LOCAL_CLAUDE_SHIP_WAIT_READY_MS}" in command
        assert 'fresh_reply_shipped=0' in command
        assert '"fresh_reply_shipped"' in command
        assert "--json" in command
        assert "Managed local Claude transcript did not ship a fresh reply event" in command


def test_managed_local_claude_ship_wait_ready_budget_preserves_long_tail_coverage():
    assert MANAGED_LOCAL_CLAUDE_SHIP_WAIT_READY_MS == 8000


def test_validate_managed_local_chat_done_payload_accepts_successful_zero_exit_code():
    session_id = "9aa6380c-ec1d-4a3b-a221-fa7feb96fcb6"

    error = validate_managed_local_chat_done_payload(
        session_id=session_id,
        done_payload={
            "created_continuation": False,
            "shipped_session_id": session_id,
            "persisted_events": 2,
            "sync_status": "complete",
            "persistence_error": None,
            "exit_code": 0,
        },
    )

    assert error is None


def test_validate_managed_local_chat_done_payload_accepts_sync_pending_without_persisted_events():
    session_id = "9aa6380c-ec1d-4a3b-a221-fa7feb96fcb6"

    error = validate_managed_local_chat_done_payload(
        session_id=session_id,
        done_payload={
            "created_continuation": False,
            "shipped_session_id": session_id,
            "persisted_events": 0,
            "sync_status": "pending",
            "persistence_error": None,
            "exit_code": 0,
        },
    )

    assert error is None


def test_validate_managed_local_chat_done_payload_rejects_nonzero_exit_code():
    session_id = "9aa6380c-ec1d-4a3b-a221-fa7feb96fcb6"

    error = validate_managed_local_chat_done_payload(
        session_id=session_id,
        done_payload={
            "created_continuation": False,
            "shipped_session_id": session_id,
            "persisted_events": 2,
            "sync_status": "complete",
            "persistence_error": None,
            "exit_code": 3,
        },
    )

    assert error == "expected exit_code=0, got 3"


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
        assert f"--session-id {session.id}" in command
        assert "--json" in command
        assert "fresh_reply_shipped=0" in command


def test_ship_managed_local_claude_transcript_retries_after_transient_runner_disconnect(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    reconnect_calls = {"count": 0}

    async def fake_wait_for_reconnect(*, owner_id, runner_id, timeout_secs, poll_interval_secs=0.25):
        reconnect_calls["count"] += 1
        assert owner_id > 0
        assert runner_id > 0
        assert timeout_secs > 0
        return True

    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        "zerg.services.managed_local_control._await_managed_local_runner_reconnect",
        fake_wait_for_reconnect,
    )

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.provider_session_id = "b0c72633-c8b1-46a4-a42a-53a388b69147"
        db.commit()

        for transient_error in ("Runner is offline", "Failed to send command to runner"):
            dispatcher = _FakeDispatcher()
            dispatcher.results = deque(
                [
                    {
                        "ok": False,
                        "error": {
                            "message": transient_error,
                        },
                    },
                    {
                        "ok": True,
                        "data": {
                            "exit_code": 0,
                            "stdout": "",
                            "stderr": "",
                        },
                    },
                ]
            )
            monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

            result = asyncio.run(
                ship_managed_local_claude_transcript(
                    db=db,
                    owner_id=user.id,
                    session=session,
                    commis_id="managed-local-claude-retry",
                )
            )

            assert result.ok is True
            assert len(dispatcher.calls) == 2
            assert dispatcher.calls[0]["runner_id"] == runner.id
            assert dispatcher.calls[1]["runner_id"] == runner.id
            assert dispatcher.calls[0]["commis_id"] == "managed-local-claude-retry"
            assert dispatcher.calls[1]["commis_id"] == "managed-local-claude-retry"

        assert reconnect_calls["count"] == 2


def test_send_text_to_managed_local_session_can_require_active_hook_phase(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(
        *,
        db_bind,
        session_id,
        after_runtime_event_id,
        after_presence_updated_at,
        phases,
        timeout_secs,
        poll_interval_secs=1.0,
    ):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_runtime_event_id >= 0
        assert after_presence_updated_at is None
        assert phases == {"thinking", "running"}
        return SessionRuntimeEvent(
            id=after_runtime_event_id + 1,
            runtime_key=f"claude:{session_id}",
            session_id=session_id,
            provider="claude",
            device_id="cinder",
            source="claude_hook",
            kind="phase_signal",
            phase="thinking",
            tool_name=None,
            occurred_at=datetime.now(timezone.utc),
            freshness_ms=90_000,
            dedupe_key=f"hook:{session_id}:thinking",
            payload_json="{}",
        )

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

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


def test_send_text_to_managed_local_session_reports_verification_failure_without_runtime_signal(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(**_kwargs):
        return None

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

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
        assert result.error == "Managed local session did not acknowledge the prompt after send"


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


def test_await_managed_local_hook_phase_update_prefers_presence_cache(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        cache = get_presence_cache()
        baseline = datetime.now(timezone.utc)
        cache.upsert(
            str(session.id),
            "idle",
            provider="claude",
            cwd=session.cwd,
            project=session.project,
            updated_at=baseline,
        )

        async def _update_later():
            await asyncio.sleep(0.05)
            cache.upsert(
                str(session.id),
                "thinking",
                provider="claude",
                cwd=session.cwd,
                project=session.project,
                updated_at=datetime.now(timezone.utc),
            )

        async def _run_wait():
            writer = asyncio.create_task(_update_later())
            try:
                return await await_managed_local_hook_phase_update(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_runtime_event_id=0,
                    after_presence_updated_at=baseline,
                    phases={"thinking", "running"},
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is not None
        assert result.phase == "thinking"
        assert result.source == "presence_cache"


def test_await_managed_local_turn_terminal_accepts_presence_cache_terminal_without_active_phase(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        cache = get_presence_cache()
        baseline = datetime.now(timezone.utc)
        cache.upsert(
            str(session.id),
            "idle",
            provider="claude",
            cwd=session.cwd,
            project=session.project,
            updated_at=baseline,
        )

        async def _update_later():
            await asyncio.sleep(0.05)
            cache.upsert(
                str(session.id),
                "needs_user",
                provider="claude",
                cwd=session.cwd,
                project=session.project,
                updated_at=datetime.now(timezone.utc),
            )

        async def _run_wait():
            writer = asyncio.create_task(_update_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_runtime_event_id=0,
                    after_presence_updated_at=baseline,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is not None
        assert result.phase == "needs_user"
        assert result.control_status == "needs_user"
        assert result.runtime_event_id == 0


def test_send_text_to_managed_local_session_verifies_codex_via_hook_activity(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(
        *,
        db_bind,
        session_id,
        after_runtime_event_id,
        after_presence_updated_at,
        phases,
        timeout_secs,
        poll_interval_secs=1.0,
    ):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_runtime_event_id >= 0
        assert after_presence_updated_at is None
        assert phases == {"thinking", "running"}
        return SessionRuntimeEvent(
            id=after_runtime_event_id + 1,
            runtime_key=f"codex:{session_id}",
            session_id=session_id,
            provider="codex",
            device_id="cinder",
            source="claude_hook",
            kind="phase_signal",
            phase="thinking",
            tool_name=None,
            occurred_at=datetime.now(timezone.utc),
            freshness_ms=90_000,
            dedupe_key=f"hook:{session_id}:thinking",
            payload_json="{}",
        )

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
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


def test_send_text_to_managed_local_session_reports_codex_hook_verification_failure(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_local_control.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(**_kwargs):
        return None

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
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
        assert result.error == "Managed local session did not acknowledge the prompt after send"


def test_await_managed_local_turn_terminal_returns_blocked_after_active_hook_phase(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")

        async def _insert_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                event_db.add_all(
                    [
                        SessionRuntimeEvent(
                            runtime_key=f"claude:{session.id}",
                            session_id=session.id,
                            provider="claude",
                            device_id="cinder",
                            source="claude_hook",
                            kind="phase_signal",
                            phase="thinking",
                            tool_name=None,
                            occurred_at=datetime.now(timezone.utc),
                            freshness_ms=90_000,
                            dedupe_key=f"hook:{session.id}:thinking",
                            payload_json="{}",
                        ),
                        SessionRuntimeEvent(
                            runtime_key=f"claude:{session.id}",
                            session_id=session.id,
                            provider="claude",
                            device_id="cinder",
                            source="claude_hook",
                            kind="phase_signal",
                            phase="blocked",
                            tool_name="Bash",
                            occurred_at=datetime.now(timezone.utc),
                            freshness_ms=86_400_000,
                            dedupe_key=f"hook:{session.id}:blocked",
                            payload_json="{}",
                        ),
                    ]
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_runtime_event_id=0,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is not None
        assert result.phase == "blocked"
        assert result.control_status == "blocked"


def test_await_managed_local_turn_terminal_ignores_terminal_before_runtime_cursor(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        stale_event = SessionRuntimeEvent(
            runtime_key=f"claude:{session.id}",
            session_id=session.id,
            provider="claude",
            device_id="cinder",
            source="claude_hook",
            kind="phase_signal",
            phase="idle",
            tool_name=None,
            occurred_at=datetime.now(timezone.utc),
            freshness_ms=600_000,
            dedupe_key=f"hook:{session.id}:idle",
            payload_json="{}",
        )
        db.add(stale_event)
        db.commit()
        baseline_runtime_event_id = int(stale_event.id)

        result = asyncio.run(
            await_managed_local_turn_terminal(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_runtime_event_id=baseline_runtime_event_id,
                timeout_secs=0.1,
                poll_interval_secs=0.02,
            )
        )

        assert result is None


def test_await_managed_local_turn_terminal_accepts_runtime_terminal_without_active_hook_phase_after_cursor(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")

        async def _insert_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                event_db.add(
                    SessionRuntimeEvent(
                        runtime_key=f"claude:{session.id}",
                        session_id=session.id,
                        provider="claude",
                        device_id="cinder",
                        source="claude_hook",
                        kind="phase_signal",
                        phase="idle",
                        tool_name=None,
                        occurred_at=datetime.now(timezone.utc),
                        freshness_ms=600_000,
                        dedupe_key=f"hook:{session.id}:idle",
                        payload_json="{}",
                    )
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_runtime_event_id=0,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())

        assert result is not None
        assert result.phase == "idle"
        assert result.control_status == "completed"
