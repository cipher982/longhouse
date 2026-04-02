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
from zerg.models.agents import SessionPresence
from zerg.services.presence_cache import get_presence_cache
from zerg.tools.builtin import session_coordination_tools

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())


def _make_engine(tmp_path, filename: str):
    engine = make_engine(f"sqlite:///{tmp_path / filename}")
    initialize_database(engine)
    return engine


def _patch_db_session(monkeypatch, engine):
    SessionLocal = sessionmaker(bind=engine)

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
    execution_home: str = "legacy",
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
        cwd="/Users/davidrose/git/zerg",
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
        loop_mode="manual",
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
    updated_at = datetime.now(timezone.utc)
    row = SessionPresence(
        session_id=str(session.id),
        state=state,
        tool_name=None,
        device_id=session.device_id,
        cwd=session.cwd,
        project=session.project,
        provider=session.provider,
        updated_at=updated_at,
    )
    db.add(row)
    db.commit()

    cache = get_presence_cache()
    cache._entries.clear()  # type: ignore[attr-defined]
    cache.upsert(
        str(session.id),
        state,
        device_id=session.device_id,
        cwd=session.cwd,
        project=session.project,
        provider=session.provider,
        updated_at=updated_at,
    )


def test_list_session_peers_returns_live_repo_matches(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "peers.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        source = _seed_session(db, device_id="shipper-laptop", device_name="laptop")
        peer = _seed_session(db, device_id="shipper-cube", device_name="cube", git_branch="feature/messaging")
        _seed_session(db, device_id="shipper-idle", device_name="idle-box", git_repo="git@github.com:other/repo.git")
        _seed_presence(db, session=source, state="idle")
        _seed_presence(db, session=peer, state="thinking")

    result = session_coordination_tools.list_session_peers(
        repo="longhouse",
        exclude_session_id=str(source.id),
        active_only=True,
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["peers"][0]["session_id"] == str(peer.id)
    assert data["peers"][0]["presence_state"] == "thinking"


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


def test_get_session_tail_returns_recent_events_in_order(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "tail.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        session = _seed_session(db)
        _seed_event(db, session=session, role="user", content_text="first", minutes_offset=-2)
        _seed_event(db, session=session, role="assistant", content_text="second", minutes_offset=-1)
        _seed_event(db, session=session, role="assistant", content_text="third", minutes_offset=0)
        session_id = str(session.id)

    result = session_coordination_tools.get_session_tail(session_id=session_id, limit=2)

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 2
    assert [event["content"] for event in data["events"]] == ["second", "third"]


def test_message_session_creates_stored_only_message_for_legacy_target(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "message.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-cube")

    result = asyncio.run(
        session_coordination_tools.message_session_async(
            from_session_id=str(from_session.id),
            to_session_id=str(to_session.id),
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


def test_check_session_messages_filters_unacknowledged(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "check_messages.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-cube")
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

    result = session_coordination_tools.check_session_messages(session_id=str(to_session.id))

    assert result["ok"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["messages"][0]["text"] == "unacked"


def test_acknowledge_session_message_marks_target_message(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path, "ack_message.db")
    SessionLocal = _patch_db_session(monkeypatch, engine)

    with SessionLocal() as db:
        from_session = _seed_session(db, device_id="shipper-laptop")
        to_session = _seed_session(db, device_id="shipper-cube")
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

    result = session_coordination_tools.acknowledge_session_message(message_id=message_id, session_id=to_session_id)

    assert result["ok"] is True
    data = result["data"]
    assert data["acknowledged_at"] is not None

    with SessionLocal() as db:
        refreshed = db.query(SessionMessage).filter(SessionMessage.id == message_id).one()
        assert refreshed.acknowledged_at is not None
