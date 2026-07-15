from datetime import datetime
from datetime import timezone
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
from zerg.services.console_turns import mark_console_turn_active
from zerg.services.console_turns import settle_console_turn
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_DELIVERING
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_COMPLETED
from zerg.services.session_turns import SESSION_TURN_STATE_DRAINING
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
