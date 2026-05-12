from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.session_execution_home import SessionExecutionHome


def _make_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_managed_codex_session(db, *, started_at: datetime) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="provisional-events",
        device_id="cinder",
        cwd="/tmp/project",
        started_at=started_at,
        last_activity_at=started_at,
        execution_home=SessionExecutionHome.MANAGED_LOCAL.value,
        managed_transport="codex_app_server",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _live_event(
    *,
    session_id,
    occurred_at: datetime,
    seq: int,
    live_text: str,
    delta: str | None = None,
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
        dedupe_key=f"bridge:live:{session_id}:thread-1:turn-1:{seq}",
        payload={
            "progress_kind": "bridge_live_transcript_delta",
            "managed_transport": "codex_app_server",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "seq": seq,
            "method": "item/agentMessage/delta",
            "delta": delta if delta is not None else live_text[-1:],
            "live_text": live_text,
            "turn_completed": turn_completed,
        },
    )


def test_live_bridge_snapshots_upsert_one_active_provisional_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_upsert.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))

        result = ingest_runtime_events(
            db,
            [
                _live_event(session_id=session.id, occurred_at=now, seq=1, live_text="hel"),
                _live_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=20),
                    seq=2,
                    live_text="hello",
                ),
                _live_event(session_id=session.id, occurred_at=now + timedelta(milliseconds=40), seq=1, live_text="h"),
            ],
        )
        db.commit()

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()
        visible = AgentsStore(db).get_session_events(session.id)

    assert result.accepted == 2
    assert result.duplicates == 1
    assert len(rows) == 1
    assert rows[0].event_origin == "live_provisional"
    assert rows[0].provisional_state == "active"
    assert rows[0].provisional_key == f"codex_bridge_live:{session.id}:thread-1:turn-1"
    assert rows[0].provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:2"
    assert rows[0].provisional_seq == 2
    assert rows[0].content_text == "hello"
    assert [event.id for event in visible] == [rows[0].id]


def test_durable_ingest_reconciles_matching_provisional_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_reconcile.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    assistant_line = (
        '{"type":"response_item","timestamp":"2026-05-11T12:00:02Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"hello world"}]}}'
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _live_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=3,
                    live_text="hello world",
                    turn_completed=True,
                )
            ],
        )
        db.commit()

        ingest_result = AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="codex",
                environment="test",
                project="provisional-events",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now - timedelta(minutes=1),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="hello world",
                        timestamp=now + timedelta(seconds=2),
                        source_path=source_path,
                        source_offset=100,
                        raw_json=assistant_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=100, raw_json=assistant_line),
                ],
            )
        )

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).order_by(AgentEvent.id.asc()).all()
        visible = AgentsStore(db).get_session_events(session.id)

    assert ingest_result.events_inserted == 1
    assert [row.event_origin for row in rows] == ["live_provisional", "durable"]
    assert rows[0].provisional_state == "reconciled"
    assert rows[0].reconciled_event_id == rows[1].id
    assert rows[1].content_text == "hello world"
    assert [event.id for event in visible] == [rows[1].id]


def test_durable_ingest_supersedes_unmatched_older_provisional_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_supersede.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    assistant_line = (
        '{"type":"response_item","timestamp":"2026-05-11T12:00:04Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"fresh durable reply"}]}}'
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _live_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=1,
                    live_text="older partial",
                )
            ],
        )
        db.commit()

        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="codex",
                environment="test",
                project="provisional-events",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now - timedelta(minutes=1),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="fresh durable reply",
                        timestamp=now + timedelta(seconds=4),
                        source_path=source_path,
                        source_offset=100,
                        raw_json=assistant_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=100, raw_json=assistant_line),
                ],
            )
        )

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).order_by(AgentEvent.id.asc()).all()
        visible = AgentsStore(db).get_session_events(session.id)

    assert [row.event_origin for row in rows] == ["live_provisional", "durable"]
    assert rows[0].provisional_state == "superseded"
    assert rows[0].reconciled_event_id is None
    assert [event.content_text for event in visible] == ["fresh durable reply"]
