from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionTurn
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import set_thread_execution_target
from zerg.services.console_turns import ConsoleTurnConflict
from zerg.services.console_turns import ConsoleTurnUnavailable
from zerg.services.console_turns import enqueue_console_turn
from zerg.services.session_turns import SESSION_TURN_STATE_QUEUED


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
