from __future__ import annotations

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
from zerg.models.agents import ManagedLocalTurn
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.managed_local_turns import attach_review_to_managed_local_turn
from zerg.services.managed_local_turns import create_managed_local_turn
from zerg.services.managed_local_turns import get_managed_local_turn
from zerg.services.managed_local_turns import get_managed_local_turn_snapshot
from zerg.services.managed_local_turns import mark_managed_local_turn_send_accepted
from zerg.services.managed_local_turns import mark_managed_local_turn_terminal
from zerg.services.managed_local_turns import maybe_mark_managed_local_turn_durable


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test_managed_local_turns.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _seed_user_runner_and_session(db):
    user = User(email="managed-local-turns@test.local", role=UserRole.USER.value)
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
        provider="claude",
        environment="development",
        project="zerg",
        device_id=runner.name,
        cwd="/Users/davidrose/git/zerg",
        started_at=datetime.now(timezone.utc),
        provider_session_id=str(uuid4()),
        continuation_kind="local",
        origin_label=runner.name,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        execution_home="managed_local",
        managed_transport="tmux",
        source_runner_id=runner.id,
        source_runner_name=runner.name,
        managed_session_name="lh-zerg-turns",
        loop_mode="manual",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_managed_local_turn_lifecycle_binds_durable_events_and_review(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_user_runner_and_session(db)
        turn = create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-1234",
            baseline_event_id=0,
            baseline_runtime_event_id=9,
            expected_user_text="continue",
        )
        db.commit()

        assert turn.id is not None
        assert mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-1234")
        assert mark_managed_local_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-1234",
            phase="idle",
            terminal_at=datetime.now(timezone.utc),
            terminal_runtime_event_id=11,
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

        durable_turn = maybe_mark_managed_local_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.durable_user_event_id is not None
        assert durable_turn.durable_assistant_event_id is not None
        assert durable_turn.durable_at is not None

        attached = attach_review_to_managed_local_turn(
            db,
            session_id=session.id,
            assistant_event_id=int(durable_turn.durable_assistant_event_id),
            review_id=77,
        )
        assert attached is True
        db.commit()

        refreshed = get_managed_local_turn(db, session_id=session.id, request_id="req-1234")
        assert refreshed is not None
        assert refreshed.send_accepted_at is not None
        assert refreshed.terminal_phase == "idle"
        assert refreshed.terminal_runtime_event_id == 11
        assert refreshed.review_id == 77


def test_managed_local_turn_durability_ignores_old_assistant_before_current_prompt(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_user_runner_and_session(db)
        create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-older-assistant",
            baseline_event_id=0,
            baseline_runtime_event_id=0,
            expected_user_text="continue",
        )
        mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-older-assistant")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="older reply",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text="continue",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        assert maybe_mark_managed_local_turn_durable(db, session_id=session.id) is None
        row = db.query(ManagedLocalTurn).filter(ManagedLocalTurn.request_id == "req-older-assistant").one()
        assert row.durable_at is None


def test_managed_local_turn_durability_clears_timeout_once_events_arrive(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_user_runner_and_session(db)
        turn = create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-timeout-heal",
            baseline_event_id=0,
            baseline_runtime_event_id=0,
            expected_user_text="continue",
        )
        mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-timeout-heal")
        turn.error_code = "turn_timeout"
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

        durable_turn = maybe_mark_managed_local_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.durable_at is not None
        assert durable_turn.error_code is None


def test_managed_local_turn_durability_tracks_last_assistant_reply_after_tool_use(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_user_runner_and_session(db)
        create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-tool-turn",
            baseline_event_id=0,
            baseline_runtime_event_id=0,
            expected_user_text="continue",
        )
        mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-tool-turn")
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
                    content_text="Let me check that.",
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
                    content_text=None,
                    tool_name="Read",
                    tool_output_text="file contents",
                    timestamp=datetime.now(timezone.utc),
                ),
                AgentEvent(
                    session_id=session.id,
                    role="assistant",
                    content_text="Done. Here is the answer.",
                    timestamp=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        durable_turn = maybe_mark_managed_local_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.durable_user_event_id is not None
        assert durable_turn.durable_assistant_event_id is not None

        final_reply = (
            db.query(AgentEvent)
            .filter(
                AgentEvent.session_id == session.id,
                AgentEvent.role == "assistant",
                AgentEvent.content_text == "Done. Here is the answer.",
            )
            .one()
        )
        assert durable_turn.durable_assistant_event_id == final_reply.id


def test_managed_local_turn_snapshot_reads_committed_shadow_state(tmp_path):
    SessionLocal = _make_db(tmp_path)
    db_bind = None
    session_id = None

    with SessionLocal() as db:
        db_bind = db.get_bind()
        session = _seed_user_runner_and_session(db)
        session_id = session.id
        create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-snapshot",
            baseline_event_id=12,
            baseline_runtime_event_id=34,
            expected_user_text="continue",
        )
        mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-snapshot")
        mark_managed_local_turn_terminal(
            db,
            session_id=session.id,
            request_id="req-snapshot",
            phase="needs_user",
            terminal_at=datetime.now(timezone.utc),
            terminal_runtime_event_id=55,
        )
        db.commit()

    snapshot = get_managed_local_turn_snapshot(
        db_bind=db_bind,
        session_id=session_id,
        request_id="req-snapshot",
    )
    assert snapshot is not None
    assert snapshot.request_id == "req-snapshot"
    assert snapshot.baseline_event_id == 12
    assert snapshot.baseline_runtime_event_id == 34
    assert snapshot.send_accepted_at is not None
    assert snapshot.terminal_phase == "needs_user"
    assert snapshot.terminal_runtime_event_id == 55


def test_managed_local_turn_durability_matches_native_claude_channel_wrapper(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_user_runner_and_session(db)
        create_managed_local_turn(
            db,
            session_id=session.id,
            request_id="req-channel-turn",
            baseline_event_id=0,
            baseline_runtime_event_id=0,
            expected_user_text="continue",
        )
        mark_managed_local_turn_send_accepted(db, session_id=session.id, request_id="req-channel-turn")
        db.add_all(
            [
                AgentEvent(
                    session_id=session.id,
                    role="user",
                    content_text=(
                        "<channel source=\"longhouse-channel\" injected_by=\"longhouse\">\n"
                        "continue\n"
                        "</channel>"
                    ),
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

        durable_turn = maybe_mark_managed_local_turn_durable(db, session_id=session.id)
        assert durable_turn is not None
        assert durable_turn.durable_user_event_id is not None
        assert durable_turn.durable_assistant_event_id is not None
