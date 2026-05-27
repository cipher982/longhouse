from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLivePreview
from zerg.services.session_live_previews import load_session_live_preview_map
from zerg.services.session_live_previews import supersede_session_live_preview
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events


def _make_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_session(db, *, started_at: datetime) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="live-preview-projection",
        device_id="cinder",
        cwd="/tmp/project",
        started_at=started_at,
        last_activity_at=started_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _bridge_event(
    *,
    session_id,
    occurred_at: datetime,
    seq: int,
    live_text: str,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    turn_completed: bool = False,
) -> RuntimeEventIngest:
    return RuntimeEventIngest(
        runtime_key=f"codex:{session_id}",
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge_live",
        kind="progress_signal",
        occurred_at=occurred_at,
        dedupe_key=f"bridge:live:{session_id}:{thread_id}:{turn_id}:{seq}:{live_text}",
        payload={
            "progress_kind": "bridge_live_transcript_delta",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "seq": seq,
            "live_text": live_text,
            "turn_completed": turn_completed,
        },
    )


def test_runtime_ingest_materializes_latest_live_preview_projection(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "latest_live_preview.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))

        result = ingest_runtime_events(
            db,
            [
                _bridge_event(session_id=session.id, occurred_at=now, seq=1, live_text="hel"),
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=20),
                    seq=2,
                    live_text="hello",
                ),
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=40),
                    seq=2,
                    live_text="hello",
                ),
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert result.accepted == 2
    assert result.duplicates == 1
    assert row is not None
    assert row.preview_text == "hello"
    assert row.seq == 2
    assert row.turn_key == f"codex_bridge_live:{session.id}:thread-1:turn-1"
    assert row.provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:2"
    assert row.provisional_complete == 0
    assert row.last_observation_id.endswith(f"bridge:live:{session.id}:thread-1:turn-1:2:hello")
    assert preview.text == "hello"
    assert preview.provisional_complete is False


def test_projection_keeps_higher_seq_for_same_turn(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "same_turn_ordering.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))

        ingest_runtime_events(
            db,
            [
                _bridge_event(session_id=session.id, occurred_at=now, seq=4, live_text="newer by seq"),
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=1),
                    seq=3,
                    live_text="older seq later clock",
                ),
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)

    assert row is not None
    assert row.seq == 4
    assert row.preview_text == "newer by seq"


def test_projection_resets_seq_on_new_turn_when_observed_later(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "new_turn.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))

        ingest_runtime_events(
            db,
            [
                _bridge_event(session_id=session.id, occurred_at=now, seq=99, live_text="old turn", turn_id="turn-1"),
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=1),
                    seq=1,
                    live_text="new turn",
                    turn_id="turn-2",
                    turn_completed=True,
                ),
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert row is not None
    assert row.seq == 1
    assert row.turn_key == f"codex_bridge_live:{session.id}:thread-1:turn-2"
    assert row.preview_text == "new turn"
    assert preview.provisional_complete is True


def test_projection_ignores_empty_live_text(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "empty_live_text.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))

        result = ingest_runtime_events(
            db,
            [_bridge_event(session_id=session.id, occurred_at=now, seq=1, live_text="  ")],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview_map = load_session_live_preview_map(db, [session.id])

    assert result.accepted == 1
    assert row is None
    assert preview_map == {}


def test_superseded_projection_row_is_retained_but_not_loaded(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "superseded.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [_bridge_event(session_id=session.id, occurred_at=now, seq=1, live_text="live text")],
        )

        superseded = supersede_session_live_preview(
            db,
            session_id=session.id,
            durable_at=now + timedelta(seconds=5),
            durable_event_id=123,
        )
        hidden_preview_map = load_session_live_preview_map(db, [session.id])

        ingest_runtime_events(
            db,
            [
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=10),
                    seq=1,
                    live_text="fresh turn",
                    turn_id="turn-2",
                )
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert superseded is True
    assert hidden_preview_map == {}
    assert row is not None
    assert row.superseded_at is None
    assert row.superseded_by_event_id is None
    assert row.superseded_reason is None
    assert preview.text == "fresh turn"
