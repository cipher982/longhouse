from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
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
from zerg.models.agents import SessionObservation
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_control import ManagedLocalPhaseUpdate
from zerg.services.managed_local_control import await_managed_local_hook_phase_update
from zerg.services.managed_local_control import await_managed_local_turn_events
from zerg.services.managed_local_control import await_managed_local_turn_terminal
from zerg.services.managed_local_control import get_managed_local_latest_hook_observation_id
from zerg.services.managed_local_control import interrupt_managed_local_session
from zerg.services.managed_local_control import send_text_to_managed_local_session
from zerg.services.managed_local_control import steer_text_to_managed_local_session
from zerg.services.managed_local_control import validate_managed_local_chat_done_payload
from zerg.services.session_observations import record_runtime_observation
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.session_execution_home import ManagedSessionTransport


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_control.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _managed_transport_for_provider(provider: str) -> str:
    if provider == "codex":
        return ManagedSessionTransport.CODEX_APP_SERVER.value
    return ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value


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
        cwd="/Users/example/git/zerg",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
        thread_root_session_id=None,
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        execution_home="managed_local",
        managed_transport=_managed_transport_for_provider(provider),
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name="lh-zerg-managed-local",
        loop_mode="assist",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return user, runner, session


@dataclass(frozen=True)
class _HookRuntimeRecord:
    id: int
    occurred_at: datetime


def _hook_runtime_ingest(
    *,
    session: AgentSession,
    phase: str,
    occurred_at: datetime,
    source: str = "claude_hook",
    tool_name: str | None = None,
    dedupe_suffix: str | None = None,
) -> RuntimeEventIngest:
    dedupe_tail = dedupe_suffix or f"{phase}:{occurred_at.timestamp()}"
    return RuntimeEventIngest(
        runtime_key=f"{session.provider}:{session.id}",
        session_id=session.id,
        provider=session.provider,
        device_id=session.device_id,
        source=source,
        kind="phase_signal",
        phase=phase,
        tool_name=tool_name,
        occurred_at=occurred_at,
        freshness_ms=90_000,
        dedupe_key=f"hook:{session.id}:{dedupe_tail}",
        payload={},
    )


def _hook_observation_record(db, *, event: RuntimeEventIngest) -> _HookRuntimeRecord:
    observation_id = f"runtime:{event.source}:{event.dedupe_key}"
    row = db.query(SessionObservation).filter(SessionObservation.observation_id == observation_id).one()
    return _HookRuntimeRecord(id=int(row.id), occurred_at=event.occurred_at)


def _record_hook_observation_only(
    db,
    *,
    session: AgentSession,
    phase: str,
    occurred_at: datetime,
    source: str = "claude_hook",
    tool_name: str | None = None,
    dedupe_suffix: str,
) -> _HookRuntimeRecord:
    event = _hook_runtime_ingest(
        session=session,
        phase=phase,
        occurred_at=occurred_at,
        source=source,
        tool_name=tool_name,
        dedupe_suffix=dedupe_suffix,
    )
    record_runtime_observation(db, event)
    db.flush()
    return _hook_observation_record(db, event=event)


def _materialize_hook_runtime_state(
    db,
    *,
    session: AgentSession,
    phase: str,
    occurred_at: datetime,
    source: str = "claude_hook",
    event_id: int | None = None,
    tool_name: str | None = None,
):
    del event_id
    event = _hook_runtime_ingest(
        session=session,
        phase=phase,
        occurred_at=occurred_at,
        source=source,
        tool_name=tool_name,
    )
    ingest_runtime_events(db, [event])
    db.flush()
    return _hook_observation_record(db, event=event)


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
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

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
        command = str(dispatcher.calls[0]["command"])
        assert "exec longhouse claude-channel send --session-id" in command
        assert "--text continue" in command


def test_interrupt_managed_local_session_uses_claude_channel_command(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")

        result = asyncio.run(
            interrupt_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                commis_id="managed-local-interrupt-test",
            )
        )

        assert result.ok is True
        assert result.exit_code == 0
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-interrupt-test"
        command = str(dispatcher.calls[0]["command"])
        assert "exec longhouse claude-channel interrupt --session-id" in command
        assert str(session.id) in command


def test_interrupt_managed_local_session_uses_codex_bridge_command(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="codex")

        result = asyncio.run(
            interrupt_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                commis_id="managed-local-interrupt-test",
            )
        )

        assert result.ok is True
        assert result.exit_code == 0
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        command = str(dispatcher.calls[0]["command"])
        assert "codex-bridge interrupt --session-id" in command
        assert str(session.id) in command


def test_steer_text_to_managed_local_session_uses_claude_channel_command(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")

        result = asyncio.run(
            steer_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="redirect",
                commis_id="managed-local-steer-test",
            )
        )

        assert result.ok is True
        assert result.exit_code == 0
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-steer-test"
        command = str(dispatcher.calls[0]["command"])
        assert "exec longhouse claude-channel send --session-id" in command
        assert "--text redirect" in command
        assert "--meta intent=steer" in command


def test_interrupt_managed_local_session_reports_nonzero_exit(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    dispatcher.results = deque(
        [
            {
                "ok": True,
                "data": {
                    "exit_code": 7,
                    "stdout": "",
                    "stderr": "interrupt failed",
                },
            }
        ]
    )
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="claude")

        result = asyncio.run(
            interrupt_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
            )
        )

        assert result.ok is False
        assert result.exit_code == 7
        assert result.error == "interrupt failed"
        assert result.stderr == "interrupt failed"


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


def test_send_text_to_managed_local_session_uses_engine_bridge_for_codex(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

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
        assert 'engine="$(command -v longhouse-engine || true)"' in command
        assert '"$engine" codex-bridge send --session-id' in command
        assert "--text continue" in command


def test_send_text_to_managed_local_session_supports_repeated_claude_sends(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

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
        assert "exec longhouse claude-channel send --session-id" in first_command
        assert "continue alpha" in first_command
        assert "exec longhouse claude-channel send --session-id" in second_command
        assert "status? [ok]" in second_command


def test_await_managed_local_hook_phase_update_ignores_stale_active_event_inserted_after_cursor(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        baseline_event = _materialize_hook_runtime_state(
            db,
            session=session,
            phase="thinking",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()
        baseline_observation_id = int(baseline_event.id)
        baseline_occurred_at = baseline_event.occurred_at

        async def _insert_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                _record_hook_observation_only(
                    event_db,
                    session=session,
                    phase="running",
                    tool_name="Bash",
                    occurred_at=baseline_occurred_at,
                    dedupe_suffix="running-stale-after-cursor",
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_hook_phase_update(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_observation_id=baseline_observation_id,
                    phases={"thinking", "running"},
                    timeout_secs=0.2,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is None


def test_await_managed_local_hook_phase_update_accepts_codex_bridge_phase_source(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="codex")
        baseline_event = _materialize_hook_runtime_state(
            db,
            session=session,
            phase="idle",
            source="codex_bridge",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()
        baseline_observation_id = get_managed_local_latest_hook_observation_id(
            db=db,
            session_id=session.id,
        )
        assert baseline_observation_id == baseline_event.id

        _materialize_hook_runtime_state(
            db,
            session=session,
            phase="thinking",
            source="codex_bridge",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()

        result = asyncio.run(
            await_managed_local_hook_phase_update(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_observation_id=baseline_observation_id,
                phases={"thinking", "running"},
                timeout_secs=0.2,
                poll_interval_secs=0.02,
            )
        )

        assert result is not None
        assert result.phase == "thinking"
        assert result.source == "codex_bridge"


def test_send_text_to_managed_local_session_uses_claude_channel_bridge_command(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue from loop",
                commis_id="managed-local-claude-channel",
            )
        )

        assert result.ok is True
        assert len(dispatcher.calls) == 1
        assert dispatcher.calls[0]["runner_id"] == runner.id
        assert dispatcher.calls[0]["commis_id"] == "managed-local-claude-channel"
        command = str(dispatcher.calls[0]["command"])
        assert "exec longhouse claude-channel send --session-id" in command
        assert "--text" in command
        assert "continue from loop" in command


def test_send_text_to_managed_local_session_trusts_engine_turn_start_ack(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatch_calls: list[dict[str, object]] = []

    async def _fake_dispatch_managed_control_command(**kwargs):
        dispatch_calls.append(kwargs)
        return SimpleNamespace(
            ok=True,
            transport="engine_channel",
            data={
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "turn_id": "turn-engine-1",
                "turn_status": "inProgress",
            },
            error=None,
        )

    async def _unexpected_phase_wait(**_kwargs):
        raise AssertionError("engine-channel Codex send should not wait for DB phase verification")

    monkeypatch.setattr(
        "zerg.services.managed_local_control._managed_control_transport_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "zerg.services.managed_local_control.dispatch_managed_control_command",
        _fake_dispatch_managed_control_command,
    )
    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _unexpected_phase_wait,
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="codex")

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="continue",
                commis_id="managed-local-engine-verified",
                verify_turn_started=True,
                verification_timeout_secs=15.0,
            )
        )

        assert result.ok is True
        assert result.verified_turn_started is True
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0]["command_type"] == "session.send_text"
        assert dispatch_calls[0]["payload"] == {"text": "continue"}


def test_validate_managed_local_chat_done_payload_accepts_successful_zero_exit_code():
    session_id = "9aa6380c-ec1d-4a3b-a221-fa7feb96fcb6"

    error = validate_managed_local_chat_done_payload(
        session_id=session_id,
        done_payload={
            "created_branch": False,
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
            "created_branch": False,
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
            "created_branch": False,
            "shipped_session_id": session_id,
            "persisted_events": 2,
            "sync_status": "complete",
            "persistence_error": None,
            "exit_code": 3,
        },
    )

    assert error == "expected exit_code=0, got 3"


def test_send_text_to_managed_local_session_can_require_active_hook_phase_for_codex(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(
        *,
        db_bind,
        session_id,
        after_observation_id,
        phases,
        timeout_secs,
        poll_interval_secs=1.0,
    ):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_observation_id >= 0
        assert phases == {"thinking", "running"}
        return ManagedLocalPhaseUpdate(
            observation_id=after_observation_id + 1,
            phase="thinking",
            source="claude_hook",
            occurred_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="codex")

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


def test_send_text_to_managed_local_session_reports_codex_verification_failure_without_runtime_signal(
    monkeypatch, tmp_path
):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(**_kwargs):
        return None

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="codex")

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


def test_send_text_to_managed_local_session_verifies_codex_via_hook_activity(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(
        *,
        db_bind,
        session_id,
        after_observation_id,
        phases,
        timeout_secs,
        poll_interval_secs=1.0,
    ):
        assert db_bind is not None
        assert timeout_secs == 2.5
        assert after_observation_id >= 0
        assert phases == {"thinking", "running"}
        return ManagedLocalPhaseUpdate(
            observation_id=after_observation_id + 1,
            phase="thinking",
            source="claude_hook",
            occurred_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="codex")

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
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)

    async def _fake_wait_for_hook_phase(**_kwargs):
        return None

    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        _fake_wait_for_hook_phase,
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="codex")

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
                _materialize_hook_runtime_state(
                    event_db,
                    session=session,
                    phase="thinking",
                    occurred_at=datetime.now(timezone.utc),
                )
                _materialize_hook_runtime_state(
                    event_db,
                    session=session,
                    phase="blocked",
                    tool_name="Bash",
                    occurred_at=datetime.now(timezone.utc),
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_observation_id=0,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is not None
        assert result.phase == "blocked"
        assert result.control_status == "blocked"


def test_await_managed_local_turn_terminal_ignores_terminal_before_observation_cursor(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        stale_event = _materialize_hook_runtime_state(
            db,
            session=session,
            phase="idle",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()
        baseline_observation_id = int(stale_event.id)

        result = asyncio.run(
            await_managed_local_turn_terminal(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_observation_id=baseline_observation_id,
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
                _materialize_hook_runtime_state(
                    event_db,
                    session=session,
                    phase="idle",
                    occurred_at=datetime.now(timezone.utc),
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_observation_id=0,
                    timeout_secs=1.0,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())

        assert result is not None
        assert result.phase == "idle"
        assert result.control_status == "completed"


def test_await_managed_local_turn_terminal_accepts_codex_bridge_terminal_phase(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="codex")
        active_event = _materialize_hook_runtime_state(
            db,
            session=session,
            phase="thinking",
            source="codex_bridge",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()

        _materialize_hook_runtime_state(
            db,
            session=session,
            phase="idle",
            source="codex_bridge",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()

        result = asyncio.run(
            await_managed_local_turn_terminal(
                db_bind=db.get_bind(),
                session_id=session.id,
                after_observation_id=active_event.id,
                timeout_secs=0.2,
                poll_interval_secs=0.02,
            )
        )

        assert result is not None
        assert result.phase == "idle"
        assert result.control_status == "completed"


def test_await_managed_local_turn_terminal_ignores_stale_terminal_inserted_after_cursor(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        _user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        baseline_event = _materialize_hook_runtime_state(
            db,
            session=session,
            phase="thinking",
            occurred_at=datetime.now(timezone.utc),
        )
        db.commit()
        baseline_observation_id = int(baseline_event.id)
        baseline_occurred_at = baseline_event.occurred_at

        async def _insert_later():
            await asyncio.sleep(0.05)
            with SessionLocal() as event_db:
                _record_hook_observation_only(
                    event_db,
                    session=session,
                    phase="idle",
                    occurred_at=baseline_occurred_at,
                    dedupe_suffix="idle-stale-after-cursor",
                )
                event_db.commit()

        async def _run_wait():
            writer = asyncio.create_task(_insert_later())
            try:
                return await await_managed_local_turn_terminal(
                    db_bind=db.get_bind(),
                    session_id=session.id,
                    after_observation_id=baseline_observation_id,
                    timeout_secs=0.2,
                    poll_interval_secs=0.02,
                )
            finally:
                await writer

        result = asyncio.run(_run_wait())
        assert result is None


def test_send_text_to_managed_local_session_verifies_claude_channel_bridge_via_persisted_prompt(monkeypatch, tmp_path):
    """Native Claude channel sends verify against the persisted user prompt, not hook phases."""
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)
    persisted_user_event = AgentEvent(
        id=123,
        session_id=uuid4(),
        role="user",
        content_text='<channel source="longhouse">hello channel</channel>',
        timestamp=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_persisted_user_prompt",
        lambda **_kwargs: asyncio.sleep(0, result=persisted_user_event),
    )
    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_hook_phase_update",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("hook phase verification should not run for native Claude")
        ),
    )

    with SessionLocal() as db:
        user, runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.managed_transport = ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value
        # Session-identity-kernel cleanup: ``provider_session_id`` is no
        # longer settable on AgentSession; it derives from session.id.
        session.cwd = "/tmp/demo"
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="hello channel",
                commis_id="channel-test",
                verify_turn_started=True,
                verification_timeout_secs=0.1,
            )
        )

    assert result.ok is True, result.error
    assert result.verified_turn_started is True
    assert len(dispatcher.calls) == 1


def test_send_text_to_managed_local_session_reports_claude_channel_verification_failure(monkeypatch, tmp_path):
    SessionLocal = _make_db(tmp_path)
    dispatcher = _FakeDispatcher()
    monkeypatch.setattr("zerg.services.managed_control_dispatcher.get_runner_job_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        "zerg.services.managed_local_control.await_managed_local_persisted_user_prompt",
        lambda **_kwargs: asyncio.sleep(0, result=None),
    )

    with SessionLocal() as db:
        user, _runner, session = _seed_user_runner_and_session(db, provider="claude")
        session.managed_transport = ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE.value
        # Session-identity-kernel cleanup: ``provider_session_id`` is no
        # longer settable on AgentSession; it derives from session.id.
        session.cwd = "/tmp/demo"
        db.commit()

        result = asyncio.run(
            send_text_to_managed_local_session(
                db=db,
                owner_id=user.id,
                session=session,
                text="hello channel",
                commis_id="channel-test",
                verify_turn_started=True,
                verification_timeout_secs=0.1,
            )
        )

    assert result.ok is False
    assert result.verified_turn_started is False
    assert result.error == "Managed local session did not acknowledge the prompt after send"
