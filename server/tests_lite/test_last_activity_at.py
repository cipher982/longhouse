"""Verify the denormalized last_activity_at column is stamped correctly."""

from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore, EventIngest, SessionIngest


def _make_db(tmp_path):
    db_path = tmp_path / "activity.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _ingest(store, events, started_at=None):
    return store.ingest_session(
        SessionIngest(
            provider="claude",
            environment="test",
            project="zerg",
            device_id="dev",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=started_at or events[0].timestamp,
            events=events,
            source_lines=[],
        )
    )


def _event(text, ts):
    return EventIngest(
        role="user",
        content_text=text,
        timestamp=ts,
        source_path="/tmp/session.jsonl",
        source_offset=hash(text) % 100000,
        raw_json=f'{{"type":"user","message":{{"role":"user","content":"{text}"}},"timestamp":"{ts.isoformat()}"}}',
    )


def test_last_activity_at_set_on_first_ingest(tmp_path):
    db = _make_db(tmp_path)
    store = AgentsStore(db)

    t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 1, 10, 10, 0, tzinfo=timezone.utc)

    result = _ingest(store, [_event("a", t1), _event("b", t2), _event("c", t3)])
    db.commit()

    session = db.query(AgentSession).filter(AgentSession.id == result.session_id).one()
    # Should be max of event timestamps (t3), normalized to naive UTC
    assert session.last_activity_at is not None
    assert session.last_activity_at.replace(tzinfo=None) == t3.replace(tzinfo=None)


def test_last_activity_at_advances_on_later_events(tmp_path):
    db = _make_db(tmp_path)
    store = AgentsStore(db)

    t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    result = _ingest(store, [_event("first", t1)])
    db.commit()

    t4 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    store.ingest_session(
        SessionIngest(
            id=result.session_id,
            provider="claude",
            environment="test",
            project="zerg",
            device_id="dev",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=t1,
            ended_at=None,
            events=[_event("later", t4)],
            source_lines=[],
        )
    )
    db.commit()

    session = db.query(AgentSession).filter(AgentSession.id == result.session_id).one()
    assert session.last_activity_at.replace(tzinfo=None) == t4.replace(tzinfo=None)


def test_last_activity_at_does_not_regress(tmp_path):
    db = _make_db(tmp_path)
    store = AgentsStore(db)

    t3 = datetime(2026, 1, 1, 10, 10, 0, tzinfo=timezone.utc)
    result = _ingest(store, [_event("late", t3)])
    db.commit()

    # Ingest earlier event — should NOT move last_activity_at backwards
    t0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    store.ingest_session(
        SessionIngest(
            id=result.session_id,
            provider="claude",
            environment="test",
            project="zerg",
            device_id="dev",
            cwd="/tmp",
            git_repo=None,
            git_branch=None,
            started_at=t0,
            ended_at=None,
            events=[_event("early", t0)],
            source_lines=[],
        )
    )
    db.commit()

    session = db.query(AgentSession).filter(AgentSession.id == result.session_id).one()
    assert session.last_activity_at.replace(tzinfo=None) == t3.replace(tzinfo=None)
