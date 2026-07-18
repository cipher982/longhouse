from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionTurn
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import set_thread_execution_target
from zerg.services.console_turns import begin_console_turn_drain
from zerg.services.console_turns import claim_next_console_turn
from zerg.services.console_turns import ConsoleTurnConflict
from zerg.services.console_turns import ConsoleTurnUnavailable
from zerg.services.console_turns import enqueue_console_turn
from zerg.services.console_turns import dispatch_next_console_turn
from zerg.services.console_turns import dispatch_catalog_claimed_turn
from zerg.services.console_turns import mark_console_turn_active
from zerg.services.console_turns import interrupt_console_turn
from zerg.services.console_turns import settle_console_turn
from zerg.services.console_sessions import create_empty_console_session
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_COMPLETED
from zerg.services.session_turns import SESSION_TURN_STATE_DRAINING
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_QUEUED
from zerg.services.session_turns import SESSION_TURN_STATE_STARTING


def _db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'console-turns.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)()


def _session(db):
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="longhouse",
        started_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    return session


@pytest.mark.asyncio
async def test_create_empty_console_session_has_target_but_no_run(tmp_path):
    db = _db(tmp_path)

    created = await create_empty_console_session(
        db,
        owner_id=1,
        provider="codex",
        device_id="cinder",
        cwd="/tmp/longhouse",
    )

    session = db.get(AgentSession, created.session_id)
    thread = ensure_primary_thread(db, session)
    assert created.created is True
    assert thread.id == created.thread_id
    assert thread.device_id == "cinder"
    assert thread.cwd == "/tmp/longhouse"
    assert db.query(SessionRun).count() == 0


def test_enqueue_console_turn_creates_linked_input_and_turn_atomically(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()

    result = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Inspect the failing test",
        client_request_id="request-1",
    )

    input_row = db.get(SessionInput, result.input_id)
    turn = db.get(SessionTurn, result.turn_id)
    assert result.created is True
    assert result.state == SESSION_TURN_STATE_QUEUED
    assert input_row.thread_id == thread.id
    assert turn.thread_id == thread.id
    assert turn.session_input_id == input_row.id
    assert turn.run_id is None
    assert turn.state == SESSION_TURN_STATE_QUEUED


def test_enqueue_console_turn_replays_same_client_request(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()

    first = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Continue",
        client_request_id="request-1",
    )
    replay = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Continue",
        client_request_id="request-1",
    )

    assert replay.input_id == first.input_id
    assert replay.turn_id == first.turn_id
    assert replay.state == first.state
    assert replay.created is False
    assert db.query(SessionInput).count() == 1
    assert db.query(SessionTurn).count() == 1


def test_enqueue_console_turn_rejects_request_id_reuse_with_different_text(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="First",
        client_request_id="request-1",
    )

    with pytest.raises(ConsoleTurnConflict, match="different text"):
        enqueue_console_turn(
            db,
            session=session,
            owner_id=1,
            message="Second",
            client_request_id="request-1",
        )


def test_enqueue_console_turn_requires_explicit_execution_target(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    db.commit()

    with pytest.raises(ConsoleTurnUnavailable) as exc_info:
        enqueue_console_turn(
            db,
            session=session,
            owner_id=1,
            message="Start",
            client_request_id="request-1",
        )
    assert exc_info.value.code == "execution_target_missing"


def test_console_turn_claim_and_settle_serializes_fifo_execution(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(
        thread,
        device_id="cinder",
        cwd="/tmp/longhouse",
        provider_config={"permission_mode": "bypass"},
    )
    record_thread_alias(
        db,
        thread=thread,
        provider="codex",
        alias_kind="provider_session_id",
        alias_value="codex-thread-1",
    )
    db.commit()
    first = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="First",
        client_request_id="request-1",
    )
    second = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Second",
        client_request_id="request-2",
    )

    claimed = claim_next_console_turn(db, thread_id=thread.id)
    assert claimed is not None
    assert claimed.turn_id == first.turn_id
    assert claimed.message == "First"
    assert claimed.provider_config == {"permission_mode": "bypass"}
    assert claimed.resume_provider_thread_id == "codex-thread-1"
    assert db.get(SessionTurn, first.turn_id).state == SESSION_TURN_STATE_STARTING
    assert db.get(SessionInput, first.input_id).status == INPUT_STATUS_DELIVERING
    assert db.get(SessionTurn, second.turn_id).state == SESSION_TURN_STATE_QUEUED
    assert claim_next_console_turn(db, thread_id=thread.id) is None

    mark_console_turn_active(db, turn_id=first.turn_id)
    assert db.get(SessionTurn, first.turn_id).state == SESSION_TURN_STATE_ACTIVE
    assert db.get(SessionInput, first.input_id).status == INPUT_STATUS_DELIVERED

    begin_console_turn_drain(db, turn_id=first.turn_id, terminal_phase="completed")
    assert db.get(SessionTurn, first.turn_id).state == SESSION_TURN_STATE_DRAINING
    settle_console_turn(
        db,
        turn_id=first.turn_id,
        outcome=SESSION_TURN_STATE_COMPLETED,
        exit_status="0",
    )
    assert db.get(SessionTurn, first.turn_id).state == SESSION_TURN_STATE_COMPLETED
    run = db.get(SessionRun, claimed.run_id)
    assert run.ended_at is not None
    assert run.exit_status == "0"

    next_claim = claim_next_console_turn(db, thread_id=thread.id)
    assert next_claim is not None
    assert next_claim.turn_id == second.turn_id


@pytest.mark.asyncio
async def test_dispatch_next_console_turn_uses_run_id_as_durable_command_id(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    queued = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Continue exactly once",
        client_request_id="request-dispatch",
    )

    class Registry:
        command = None

        def supports(self, **kwargs):
            return kwargs["capability"] == "codex.turn_start"

        async def send_command(self, **kwargs):
            self.command = kwargs
            return SimpleNamespace(transport_ok=True, message={"ok": True}, error=None)

    registry = Registry()
    result = await dispatch_next_console_turn(db, owner_id=1, thread_id=thread.id, registry=registry)

    assert result.turn_id == queued.turn_id
    assert result.state == SESSION_TURN_STATE_ACTIVE
    assert registry.command["command_type"] == "session.turn.start"
    assert registry.command["command_id"] == str(result.run_id)
    assert registry.command["payload"]["run_id"] == str(result.run_id)
    assert registry.command["payload"]["turn_id"] == str(queued.turn_id)
    assert registry.command["payload"]["client_request_id"] == "request-dispatch"
    assert registry.command["payload"]["message"] == "Continue exactly once"


@pytest.mark.asyncio
async def test_interrupt_console_turn_targets_exact_active_run(tmp_path, monkeypatch):
    db = _db(tmp_path)
    session = _session(db)
    session.provider = "cursor"
    thread = ensure_primary_thread(db, session)
    thread.provider = "cursor"
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    enqueue_console_turn(db, session=session, owner_id=1, message="Work", client_request_id="interrupt-me")

    class Registry:
        command = None

        def supports(self, **kwargs):
            return kwargs["capability"] in {"cursor.turn_start", "cursor.turn_interrupt"}

        async def send_command(self, **kwargs):
            self.command = kwargs
            return SimpleNamespace(transport_ok=True, message={"ok": True}, error=None)

    registry = Registry()
    dispatched = await dispatch_next_console_turn(db, owner_id=1, thread_id=thread.id, registry=registry)
    monkeypatch.setattr("zerg.database.live_catalog_enabled", lambda: False)
    result = await interrupt_console_turn(db, owner_id=1, session_id=session.id, registry=registry)

    assert result.dispatched is True
    assert result.run_id == dispatched.run_id
    assert registry.command["command_type"] == "session.turn.interrupt"
    assert registry.command["payload"]["provider"] == "cursor"
    assert registry.command["payload"]["run_id"] == str(dispatched.run_id)
    assert registry.command["command_id"] == f"{dispatched.run_id}:interrupt"


@pytest.mark.asyncio
async def test_opencode_console_dispatch_resumes_native_session_and_interrupts_exact_run(tmp_path, monkeypatch):
    db = _db(tmp_path)
    session = _session(db)
    session.provider = "opencode"
    thread = ensure_primary_thread(db, session)
    thread.provider = "opencode"
    set_thread_execution_target(
        thread,
        device_id="cinder",
        cwd="/tmp/longhouse",
        provider_config={"permission_mode": "bypass"},
    )
    record_thread_alias(
        db,
        thread=thread,
        provider="opencode",
        alias_kind="provider_session_id",
        alias_value="ses_native_resume",
    )
    db.commit()
    enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Continue OpenCode",
        client_request_id="opencode-resume",
    )

    class Registry:
        commands = []

        def supports(self, **kwargs):
            return kwargs["capability"] in {"opencode.turn_start", "opencode.turn_interrupt"}

        async def send_command(self, **kwargs):
            self.commands.append(kwargs)
            return SimpleNamespace(transport_ok=True, message={"ok": True}, error=None)

    registry = Registry()
    dispatched = await dispatch_next_console_turn(db, owner_id=1, thread_id=thread.id, registry=registry)
    monkeypatch.setattr("zerg.database.live_catalog_enabled", lambda: False)
    interrupted = await interrupt_console_turn(db, owner_id=1, session_id=session.id, registry=registry)

    assert dispatched.state == SESSION_TURN_STATE_ACTIVE
    assert registry.commands[0]["command_type"] == "session.turn.start"
    assert registry.commands[0]["payload"]["provider"] == "opencode"
    assert registry.commands[0]["payload"]["resume_provider_thread_id"] == "ses_native_resume"
    assert interrupted.dispatched is True
    assert registry.commands[1]["command_type"] == "session.turn.interrupt"
    assert registry.commands[1]["payload"]["provider"] == "opencode"
    assert registry.commands[1]["payload"]["run_id"] == str(dispatched.run_id)


@pytest.mark.asyncio
async def test_dispatch_timeout_keeps_durable_claim_starting_and_fifo_blocked(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    first = enqueue_console_turn(db, session=session, owner_id=1, message="First", client_request_id="timeout-first")
    second = enqueue_console_turn(db, session=session, owner_id=1, message="Second", client_request_id="timeout-second")

    class Registry:
        def supports(self, **_kwargs):
            return True

        async def send_command(self, **_kwargs):
            return SimpleNamespace(transport_ok=False, message=None, error="reply timeout")

    result = await dispatch_next_console_turn(db, owner_id=1, thread_id=thread.id, registry=Registry())

    assert result.turn_id == first.turn_id
    assert result.state == SESSION_TURN_STATE_STARTING
    assert db.get(SessionTurn, first.turn_id).state == SESSION_TURN_STATE_STARTING
    assert db.get(SessionTurn, second.turn_id).state == SESSION_TURN_STATE_QUEUED
    assert claim_next_console_turn(db, thread_id=thread.id) is None


@pytest.mark.asyncio
async def test_dispatch_next_console_turn_fails_typed_when_adapter_is_missing(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    queued = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Do not fake support",
        client_request_id="request-unsupported",
    )

    class Registry:
        def supports(self, **_kwargs):
            return False

    result = await dispatch_next_console_turn(db, owner_id=1, thread_id=thread.id, registry=Registry())

    assert result.state == SESSION_TURN_STATE_FAILED
    assert "does not advertise" in result.error
    assert db.get(SessionTurn, queued.turn_id).state == SESSION_TURN_STATE_FAILED
    assert db.get(SessionRun, result.run_id).ended_at is not None


def test_run_terminal_event_settles_console_turn_after_output_drain(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    queued = enqueue_console_turn(
        db,
        session=session,
        owner_id=1,
        message="Finish cleanly",
        client_request_id="request-terminal",
    )
    claimed = claim_next_console_turn(db, thread_id=thread.id)
    assert claimed is not None
    mark_console_turn_active(db, turn_id=queued.turn_id)

    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=f"codex:{session.id}",
                session_id=session.id,
                thread_id=thread.id,
                run_id=claimed.run_id,
                provider="codex",
                device_id="cinder",
                source="codex_exec",
                kind="terminal_signal",
                occurred_at=datetime.now(timezone.utc),
                dedupe_key=f"terminal:{claimed.run_id}",
                payload={"terminal_state": "run_completed", "exit_code": 0},
            )
        ],
    )
    db.commit()

    turn = db.get(SessionTurn, queued.turn_id)
    assert turn.state == SESSION_TURN_STATE_COMPLETED
    assert turn.terminal_phase == "run_completed"
    assert turn.durable_at is not None
    assert db.get(SessionRun, claimed.run_id).exit_status == "exit_0"


@pytest.mark.asyncio
async def test_catalog_dispatch_failure_releases_and_attempts_next_claimed_turn():
    first_run = uuid4()
    second_run = uuid4()
    first_turn = uuid4()
    second_turn = uuid4()
    session_id = uuid4()
    thread_id = uuid4()

    def payload(turn_id, run_id, message):
        return {
            "turn_id": str(turn_id),
            "run_id": str(run_id),
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "provider": "codex",
            "device_id": "offline",
            "cwd": "/tmp/longhouse",
            "message": message,
            "provider_config": {},
        }

    class Registry:
        def supports(self, **_kwargs):
            return False

    class Catalog:
        calls = []

        async def call(self, _method, params):
            self.calls.append(params["turn"])
            if len(self.calls) == 1:
                return {"next_turn": payload(second_turn, second_run, "second")}
            return {"next_turn": None}

    catalog = Catalog()
    result = await dispatch_catalog_claimed_turn(
        owner_id=1,
        turn=payload(first_turn, first_run, "first"),
        client=catalog,
        registry=Registry(),
    )

    assert result.state == SESSION_TURN_STATE_FAILED
    assert [call["run_id"] for call in catalog.calls] == [str(first_run), str(second_run)]
