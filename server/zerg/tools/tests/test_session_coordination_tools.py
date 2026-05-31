"""Tests for session coordination tools."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionMessage
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session
from zerg.tools.builtin import session_coordination_tools

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())


def _make_engine(tmp_path, filename: str):
    engine = make_engine(f"sqlite:///{tmp_path / filename}")
    initialize_database(engine)
    return engine


def _patch_db_session(monkeypatch, engine):
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _db_session():
        with SessionLocal() as db:
            yield db
            db.commit()

    monkeypatch.setattr(session_coordination_tools, "db_session", _db_session)
    return SessionLocal


def _seed_session(
    db,
    *,
    project: str = "zerg",
    git_repo: str = "git@github.com:cipher982/longhouse.git",
    git_branch: str = "main",
    execution_home: str = "unmanaged_local",
    device_id: str = "shipper-laptop",
    device_name: str = "laptop",
) -> AgentSession:
    session_id = uuid4()
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="development",
        project=project,
        device_id=device_id,
        device_name=device_name,
        cwd="/Users/example/git/zerg",
        git_repo=git_repo,
        git_branch=git_branch,
        started_at=now,
        last_activity_at=now,
        provider_session_id=str(session_id),
        thread_root_session_id=session_id,
        continuation_kind="local",
        origin_label=device_id,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
        loop_mode="assist",
        execution_home=execution_home,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_event(
    db,
    *,
    session: AgentSession,
    role: str,
    content_text: str | None,
    tool_name: str | None = None,
    tool_output_text: str | None = None,
    minutes_offset: int = 0,
) -> AgentEvent:
    event = AgentEvent(
        session_id=session.id,
        role=role,
        content_text=content_text,
        tool_name=tool_name,
        tool_output_text=tool_output_text,
        timestamp=datetime.now(timezone.utc) + timedelta(minutes=minutes_offset),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _seed_presence(db, *, session: AgentSession, state: str) -> None:
    now = datetime.now(timezone.utc)
    runtime_key = runtime_key_for_session(str(session.provider or "claude"), str(session.id))
    freshness_ms = phase_freshness_ms(state) or int(timedelta(minutes=5).total_seconds() * 1000)
    freshness_expires_at = now + timedelta(milliseconds=freshness_ms)
    existing = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).first()
    if existing is None:
        db.add(
            SessionRuntimeState(
                runtime_key=runtime_key,
                session_id=session.id,
                provider=str(session.provider or "claude"),
                device_id=session.device_id,
                phase=state,
                phase_source="semantic",
                active_tool=None,
                phase_started_at=now,
                last_runtime_signal_at=now,
                last_progress_at=now,
                last_live_at=now,
                timeline_anchor_at=now,
                freshness_expires_at=freshness_expires_at,
                runtime_version=1,
            )
        )
    else:
        existing.phase = state
        existing.phase_source = "semantic"
        existing.active_tool = None
        existing.phase_started_at = now
        existing.last_runtime_signal_at = now
        existing.last_progress_at = now
        existing.last_live_at = now
        existing.timeline_anchor_at = now
        existing.freshness_expires_at = freshness_expires_at
        existing.runtime_version = int(getattr(existing, "runtime_version", 0) or 0) + 1
    db.commit()


def test_peers_returns_live_repo_matches(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "peers.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        source = _seed_session(db, device_id="shipper-laptop", device_name="laptop")
        peer = _seed_session(db, device_id="shipper-demo-machine", device_name="demo-machine", git_branch="feature/messaging")
        _seed_session(db, device_id="shipper-idle", device_name="idle-box", git_repo="git@github.com:other/repo.git")
        _seed_presence(db, session=source, state="idle")
        _seed_presence(db, session=peer, state="thinking")
        source_id = str(source.id)
        peer_id = str(peer.id)

    result = session_coordination_tools.peers(
        repo="longhouse",
        exclude_session_id=source_id,
        active_only=True,
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["peers"][0]["session_id"] == peer_id
    assert data["peers"][0]["presence_state"] == "thinking"
    assert data["peers"][0]["pending_inbound_messages"] == 0


def test_get_session_events_applies_filters(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "events.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        session = _seed_session(db)
        _seed_event(db, session=session, role="assistant", content_text="alpha planning note")
        _seed_event(
            db,
            session=session,
            role="tool",
            content_text="tool event",
            tool_name="Bash",
            tool_output_text="ok",
        )
        session_id = str(session.id)

    result = session_coordination_tools.get_session_events(
        session_id=session_id,
        roles=["tool"],
        tool_name="Bash",
        limit=20,
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["events"][0]["tool_name"] == "Bash"
    assert data["events"][0]["role"] == "tool"


def test_session_tail_returns_recent_events_in_order(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "tail.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        session = _seed_session(db)
        _seed_event(db, session=session, role="user", content_text="first", minutes_offset=-2)
        _seed_event(db, session=session, role="assistant", content_text="second", minutes_offset=-1)
        _seed_event(db, session=session, role="assistant", content_text="third", minutes_offset=0)
        session_id = str(session.id)

    result = session_coordination_tools.session_tail(session_id=session_id, limit=2)

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 2
    assert [event["content"] for event in data["events"]] == ["second", "third"]


def test_message_session_creates_stored_only_message_for_legacy_target(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "message.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-demo-machine")
        from_session_id = str(from_session.id)
        to_session_id = str(to_session.id)

    result = asyncio.run(
        session_coordination_tools.message_session_async(
            from_session_id=from_session_id,
            to_session_id=to_session_id,
            text="please check the deploy logs",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["delivery_status"] == "stored_only"

    with SessionLocal() as db:
        message = db.query(SessionMessage).one()
        assert message.body == "please check the deploy logs"
        assert message.delivery_status == "stored_only"


def test_check_messages_filters_unacknowledged(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "check_messages.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-demo-machine")
        db.add_all(
            [
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="unacked",
                    delivery_status="stored_only",
                ),
                SessionMessage(
                    from_session_id=from_session.id,
                    to_session_id=to_session.id,
                    body="acked",
                    delivery_status="stored_only",
                    acknowledged_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()
        to_session_id = str(to_session.id)

    result = session_coordination_tools.check_messages(session_id=to_session_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["messages"][0]["text"] == "unacked"


def test_ack_message_marks_target_message(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "ack_message.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-demo-machine")
        message = SessionMessage(
            from_session_id=from_session.id,
            to_session_id=to_session.id,
            body="needs ack",
            delivery_status="stored_only",
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        message_id = int(message.id)
        to_session_id = str(to_session.id)

    result = session_coordination_tools.ack_message(message_id=message_id, session_id=to_session_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["acknowledged_at"] is not None

    with SessionLocal() as db:
        refreshed = db.query(SessionMessage).filter(SessionMessage.id == message_id).one()
        assert refreshed.acknowledged_at is not None


def test_tool_registry_uses_canonical_coordination_names():
    names = {tool.name for tool in session_coordination_tools.TOOLS}

    assert {"peers", "get_session_events", "session_tail", "message_session", "check_messages", "ack_message"} <= names
    assert "list_session_peers" not in names
    assert "get_session_tail" not in names
    assert "check_session_messages" not in names
    assert "acknowledge_session_message" not in names
