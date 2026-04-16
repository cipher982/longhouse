from __future__ import annotations

import asyncio
import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurn
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services import session_turns as session_turns_service
from zerg.services.session_turns import SESSION_TURN_CONFIDENCE_EXACT
from zerg.services.session_turns import SESSION_TURN_SOURCE_MANAGED_LIVE
from zerg.services.session_turns import SESSION_TURN_STATE_ACTIVE
from zerg.services.session_turns import SESSION_TURN_STATE_DURABLE
from zerg.services.session_turns import SESSION_TURN_STATE_FAILED
from zerg.services.session_turns import SESSION_TURN_STATE_TERMINAL
from zerg.services.session_turns import create_session_turn
from zerg.services.session_turns import execute_session_turn_write
from zerg.services.session_turns import get_session_turn_snapshot
from zerg.services.session_turns import mark_session_turn_active
from zerg.services.session_turns import mark_session_turn_failed
from zerg.services.session_turns import mark_session_turn_send_accepted
from zerg.services.session_turns import mark_session_turn_terminal
from zerg.services.session_turns import maybe_mark_session_turn_durable
from zerg.services.write_serializer import WriteSerializer


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_session_turns.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_session(db):
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="zerg",
        cwd="/Users/davidrose/git/zerg",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
        continuation_kind="local",
        origin_label="cinder",
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        execution_home="managed_local",
        managed_transport="claude_channel_bridge",
        loop_mode="manual",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_session_turn_lifecycle_tracks_active_terminal_and_durable_events(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        turn = create_session_turn(
            db,
            session_id=session.id,
            request_id="req-1234",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
            baseline_event_id=0,
            baseline_runtime_cursor=9,
        )
        assert turn.id is not None
        assert turn.baseline_event_id is None
        assert turn.baseline_runtime_cursor == 9

        assert mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-1234")
        assert mark_session_turn_active(db, session_id=session.id, request_id="req-1234")
        assert mark_session_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-1234",
            phase="idle",
            terminal_at=datetime.now(timezone.utc),
        )

        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="done",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_session_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.user_event_id is not None
        assert durable_turn.durable_assistant_event_id is not None
        assert durable_turn.durable_at is not None
        assert durable_turn.state == SESSION_TURN_STATE_DURABLE

        db.commit()

        snapshot = get_session_turn_snapshot(
            db_bind=db.get_bind(),
            session_id=session.id,
            request_id="req-1234",
        )
        assert snapshot is not None
        assert snapshot.active_phase_observed_at is not None
        assert snapshot.terminal_phase == "idle"
        assert snapshot.durable_assistant_event_id == durable_turn.durable_assistant_event_id


def test_session_turn_partial_unique_request_id_allows_null_and_rejects_duplicates(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        db.add_all(
            [
                SessionTurn(
                    session_id=session.id,
                    request_id=None,
                    source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
                    timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
                    state="created",
                    user_submitted_at=datetime.now(timezone.utc),
                ),
                SessionTurn(
                    session_id=session.id,
                    request_id=None,
                    source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
                    timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
                    state="created",
                    user_submitted_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        db.add(
            SessionTurn(
                session_id=session.id,
                request_id="dup-request",
                source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
                timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
                state="created",
                user_submitted_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

        db.add(
            SessionTurn(
                session_id=session.id,
                request_id="dup-request",
                source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
                timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
                state="created",
                user_submitted_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_session_turn_durable_matching_uses_last_assistant_reply_after_user_event(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        turn = create_session_turn(
            db,
            session_id=session.id,
            request_id="req-tool-turn",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-tool-turn")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="Let me check.",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text=None,
                    tool_name="Read",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="tool",
                    tool_name="Read",
                    tool_output_text="contents",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="Done.",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_session_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.id == turn.id

        final_reply = (
            db.query(AgentEvent)
            .filter(
                AgentEvent.session_id == session.id,
                AgentEvent.role == "assistant",
                AgentEvent.content_text == "Done.",
            )
            .one()
        )
        assert durable_turn.durable_assistant_event_id == final_reply.id


def test_session_turn_durable_heals_timeout_style_failure_when_events_arrive(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-timeout-heal",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-timeout-heal")
        assert mark_session_turn_failed(
            db,
            session_id=session.id,
            request_id="req-timeout-heal",
            error_code="verification_timeout",
        )
        row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-timeout-heal").one()
        assert row.state == SESSION_TURN_STATE_FAILED

        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="late but valid reply",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_session_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.error_code is None
        assert durable_turn.state == SESSION_TURN_STATE_DURABLE


def test_agents_store_ingest_marks_canonical_session_turn_durable(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-ingest",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
            baseline_event_id=0,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-ingest")
        db.commit()

        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=session.id,
                provider="claude",
                environment="development",
                project="zerg",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg",
                started_at=session.started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="continue",
                        timestamp=datetime.now(timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="done",
                        timestamp=datetime.now(timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                    ),
                ],
            )
        )

        assert result.events_inserted == 2

        row = (
            db.query(SessionTurn)
            .filter(SessionTurn.session_id == session.id, SessionTurn.request_id == "req-ingest")
            .one()
        )
        assert row.user_event_id is not None
        assert row.durable_assistant_event_id is not None
        assert row.durable_at is not None
        assert row.state == SESSION_TURN_STATE_DURABLE


def test_mark_session_turn_failed_does_not_overwrite_durable_state(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-durable",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-durable")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="done",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_session_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.state == SESSION_TURN_STATE_DURABLE
        assert mark_session_turn_failed(
            db,
            session_id=session.id,
            request_id="req-durable",
            error_code="verification_timeout",
        )

        row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-durable").one()
        assert row.state == SESSION_TURN_STATE_DURABLE
        assert row.error_code is None


def test_mark_session_turn_failed_does_not_overwrite_terminal_state(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-terminal",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-terminal")
        mark_session_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-terminal",
            phase="idle",
            terminal_at=datetime.now(timezone.utc),
        )

        assert mark_session_turn_failed(
            db,
            session_id=session.id,
            request_id="req-terminal",
            error_code="turn_timeout",
        )

        row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-terminal").one()
        assert row.state == SESSION_TURN_STATE_TERMINAL
        assert row.terminal_phase == "idle"
        assert row.error_code is None


def test_session_turn_milestones_are_idempotent_and_do_not_regress_state(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        accepted_at = datetime.now(timezone.utc)
        active_at = datetime.now(timezone.utc)
        terminal_at = datetime.now(timezone.utc)

        assert mark_session_turn_send_accepted(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            accepted_at=accepted_at,
        )
        assert mark_session_turn_send_accepted(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            accepted_at=accepted_at,
        )
        assert mark_session_turn_active(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            observed_at=active_at,
        )
        assert mark_session_turn_active(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            observed_at=active_at,
        )
        assert mark_session_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            phase="idle",
            terminal_at=terminal_at,
        )
        assert mark_session_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-idempotent",
            phase="idle",
            terminal_at=terminal_at,
        )

        row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-idempotent").one()
        assert row.state == SESSION_TURN_STATE_TERMINAL
        assert row.send_accepted_at == accepted_at
        assert row.active_phase_observed_at == active_at
        assert row.terminal_at == terminal_at


def test_mark_session_turn_active_does_not_revive_failed_turn(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-failed",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-failed")
        mark_session_turn_failed(
            db,
            session_id=session.id,
            request_id="req-failed",
            error_code="verification_timeout",
        )

        assert mark_session_turn_active(
            db,
            session_id=session.id,
            request_id="req-failed",
            observed_at=datetime.now(timezone.utc),
        )

        row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-failed").one()
        assert row.state == SESSION_TURN_STATE_FAILED
        assert row.error_code == "verification_timeout"
        assert row.active_phase_observed_at is None


def test_session_turn_durable_matching_uses_turn_submission_windows_for_multiple_pending_turns(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        turn1_submitted_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)
        turn2_submitted_at = datetime(2026, 4, 16, 12, 1, 0, tzinfo=timezone.utc)
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-pending-1",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
            user_submitted_at=turn1_submitted_at,
        )
        create_session_turn(
            db,
            session_id=session.id,
            request_id="req-pending-2",
            source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
            timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
            user_submitted_at=turn2_submitted_at,
        )
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-pending-1")
        mark_session_turn_send_accepted(db, session_id=session.id, request_id="req-pending-2")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="first",
                    timestamp=datetime(2026, 4, 16, 12, 0, 5, tzinfo=timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="second",
                    timestamp=datetime(2026, 4, 16, 12, 1, 5, tzinfo=timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="reply to second",
                    timestamp=datetime(2026, 4, 16, 12, 1, 6, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_session_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.request_id == "req-pending-2"

        first_row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-pending-1").one()
        second_row = db.query(SessionTurn).filter(SessionTurn.request_id == "req-pending-2").one()
        assert first_row.durable_at is None
        assert first_row.durable_assistant_event_id is None
        assert second_row.durable_at is not None
        assert second_row.state == SESSION_TURN_STATE_DURABLE


def test_execute_session_turn_write_uses_bound_database_when_serializer_is_configured(tmp_path, monkeypatch):
    primary_engine = make_engine(f"sqlite:///{tmp_path / 'primary_turns.db'}")
    secondary_engine = make_engine(f"sqlite:///{tmp_path / 'secondary_turns.db'}")
    initialize_database(primary_engine)
    initialize_database(secondary_engine)
    PrimarySession = make_sessionmaker(primary_engine)
    SecondarySession = make_sessionmaker(secondary_engine)

    session_id = uuid4()
    with PrimarySession() as primary_db:
        primary_session = AgentSession(
            id=session_id,
            provider="claude",
            environment="development",
            project="zerg",
            cwd="/Users/davidrose/git/zerg",
            started_at=datetime.now(timezone.utc),
            provider_session_id=str(uuid4()),
            continuation_kind="local",
            origin_label="cinder",
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            loop_mode="manual",
        )
        primary_db.add(primary_session)
        primary_db.commit()
    with SecondarySession() as secondary_db:
        secondary_session = AgentSession(
            id=session_id,
            provider="claude",
            environment="development",
            project="zerg",
            cwd="/Users/davidrose/git/zerg",
            started_at=datetime.now(timezone.utc),
            provider_session_id=str(uuid4()),
            continuation_kind="local",
            origin_label="cinder",
            user_messages=0,
            assistant_messages=0,
            tool_calls=0,
            execution_home="managed_local",
            managed_transport="claude_channel_bridge",
            loop_mode="manual",
        )
        secondary_db.add(secondary_session)
        secondary_db.commit()

    serializer = WriteSerializer()
    serializer.configure(SecondarySession)
    monkeypatch.setattr(session_turns_service, "get_write_serializer", lambda: serializer)

    asyncio.run(
        execute_session_turn_write(
            db_bind=primary_engine,
            label="session-turn-active",
            fn=lambda turn_db: create_session_turn(
                turn_db,
                session_id=session_id,
                request_id="req-bound-db",
                source_kind=SESSION_TURN_SOURCE_MANAGED_LIVE,
                timing_confidence=SESSION_TURN_CONFIDENCE_EXACT,
            ),
        )
    )

    with PrimarySession() as primary_db:
        assert primary_db.query(SessionTurn).filter(SessionTurn.request_id == "req-bound-db").count() == 1
    with SecondarySession() as secondary_db:
        assert secondary_db.query(SessionTurn).filter(SessionTurn.request_id == "req-bound-db").count() == 0
