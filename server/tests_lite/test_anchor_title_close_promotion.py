"""Closing a session must not let a drifting summary replace its AI title."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionRuntimeState
from zerg.database import make_engine
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session


def _make_db(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


def _seed(db, **overrides) -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="runtime",
        started_at=now,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        **overrides,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _terminal(session_id, occurred_at):
    return RuntimeEventIngest(
        runtime_key=runtime_key_for_session("codex", str(session_id)),
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge",
        kind="terminal_signal",
        occurred_at=occurred_at,
        dedupe_key=f"terminal:{session_id}",
        payload={
            "terminal_state": "session_ended",
            "terminal_reason": "process_exit",
            "terminal_source": "codex_bridge",
        },
    )


def test_close_keeps_initial_ai_title_when_summary_changes(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "anchor_close_promote.db")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        # A later summary is useful for search, but not allowed to rename this session.
        session = _seed(db, anchor_title="Initial Setup", summary_title="Fix Refresh Token Rotation")
        ingest_runtime_events(db, [_terminal(session.id, now)])
        db.commit()

        stored = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        assert stored.ended_at is not None
        assert stored.anchor_title == "Initial Setup"
    engine.dispose()


def test_close_does_not_create_anchor_from_summary(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "anchor_close_sanitize.db")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        session = _seed(db, anchor_title=None, summary_title='"""\nDebug Bedrock Channel Race')
        ingest_runtime_events(db, [_terminal(session.id, now)])
        db.commit()
        stored = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        assert stored.anchor_title is None
    engine.dispose()


def test_close_keeps_anchor_when_no_usable_final_title(tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "anchor_close_keep.db")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        # Final title sanitizes to nothing -> don't clobber a good anchor.
        session = _seed(db, anchor_title="Good Anchor", summary_title="[Image #1]")
        ingest_runtime_events(db, [_terminal(session.id, now)])
        db.commit()
        stored = db.query(AgentSession).filter(AgentSession.id == session.id).one()
        assert stored.anchor_title == "Good Anchor"
    engine.dispose()
