from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
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


def _make_initialized_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
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


def _bridge_transcript_event(
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
                _bridge_transcript_event(session_id=session.id, occurred_at=now, seq=1, live_text="hel"),
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=20),
                    seq=2,
                    live_text="hello",
                ),
                _bridge_transcript_event(session_id=session.id, occurred_at=now + timedelta(milliseconds=40), seq=1, live_text="h"),
            ],
        )
        db.commit()

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()
        observations = db.query(SessionObservation).filter(SessionObservation.session_id == session.id).order_by(SessionObservation.id).all()
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
    assert [observation.kind for observation in observations] == ["bridge_transcript_delta", "bridge_transcript_delta"]
    assert observations[0].source_domain == "runtime"
    assert observations[0].source == "codex_bridge_live"
    assert json.loads(observations[1].payload_json or "{}")["payload"]["live_text"] == "hello"
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
                _bridge_transcript_event(
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
        observations = db.query(SessionObservation).filter(SessionObservation.session_id == session.id).order_by(SessionObservation.id.asc()).all()
        visible = AgentsStore(db).get_session_events(session.id)

    assert ingest_result.events_inserted == 1
    assert [row.event_origin for row in rows] == ["live_provisional", "durable"]
    assert rows[0].provisional_state == "reconciled"
    assert rows[0].reconciled_event_id == rows[1].id
    assert rows[1].content_text == "hello world"
    kinds = [observation.kind for observation in observations]
    assert "bridge_transcript_delta" in kinds
    assert "provider_source_line" in kinds
    source_observation = next(observation for observation in observations if observation.kind == "provider_source_line")
    assert source_observation.source_domain == "transcript"
    assert source_observation.source_path == source_path
    assert source_observation.source_offset == 100
    assert json.loads(source_observation.payload_json or "{}")["raw_json"] == assistant_line
    assert [event.id for event in visible] == [rows[1].id]


def test_late_live_snapshot_does_not_reactivate_reconciled_provisional_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_reconcile_late_live.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    assistant_line = '{"type":"response_item","payload":{"type":"message","role":"assistant"}}'

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=3,
                    live_text="hello world",
                    turn_completed=True,
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
        db.commit()

        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now - timedelta(seconds=1),
                    seq=4,
                    live_text="stale bridge text after durable",
                )
            ],
        )
        db.commit()

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).order_by(AgentEvent.id.asc()).all()
        visible = AgentsStore(db).get_session_events(session.id)

    assert [row.event_origin for row in rows] == ["live_provisional", "durable"]
    assert rows[0].provisional_state == "reconciled"
    assert rows[0].reconciled_event_id == rows[1].id
    assert rows[0].content_text == "hello world"
    assert [event.id for event in visible] == [rows[1].id]


def test_null_seq_live_snapshot_does_not_replace_known_newer_snapshot(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_null_seq.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(session_id=session.id, occurred_at=now, seq=5, live_text="newer text"),
                RuntimeEventIngest(
                    runtime_key=f"codex:{session.id}",
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge_live",
                    kind="progress_signal",
                    occurred_at=now + timedelta(milliseconds=10),
                    dedupe_key=f"bridge:live:{session.id}:thread-1:turn-1:null",
                    payload={
                        "progress_kind": "bridge_live_transcript_delta",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "method": "item/agentMessage/delta",
                        "live_text": "unknown older text",
                    },
                ),
            ],
        )
        db.commit()

        row = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).one()

    assert row.provisional_state == "active"
    assert row.provisional_seq == 5
    assert row.content_text == "newer text"


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
                _bridge_transcript_event(
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


def test_cross_session_search_ignores_provisional_only_text(tmp_path):
    SessionLocal = _make_initialized_sessionmaker(tmp_path, "provisional_search.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    durable_line = (
        '{"type":"response_item","timestamp":"2026-05-11T12:00:04Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"durable searchable text"}]}}'
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=1,
                    live_text="only provisional needle",
                )
            ],
        )
        db.commit()

        store = AgentsStore(db)
        provisional_sessions, provisional_total = store.list_sessions(include_test=True, query="needle")
        assert provisional_sessions == []
        assert provisional_total == 0

        store.ingest_session(
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
                        content_text="durable searchable text",
                        timestamp=now + timedelta(seconds=4),
                        source_path=source_path,
                        source_offset=100,
                        raw_json=durable_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=100, raw_json=durable_line),
                ],
            )
        )

        durable_sessions, durable_total = store.list_sessions(include_test=True, query="searchable")

    assert durable_total == 1
    assert [session.id for session in durable_sessions] == [session.id]


def test_closed_terminal_signal_supersedes_active_provisional_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "provisional_terminal.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=1,
                    live_text="unfinished bridge output",
                )
            ],
        )
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"codex:{session.id}",
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now + timedelta(seconds=10),
                    dedupe_key=f"bridge:terminal:{session.id}:1",
                    payload={
                        "terminal_state": "process_gone",
                        "terminal_reason": "bridge_stop",
                        "terminal_source": "codex_bridge",
                    },
                )
            ],
        )
        db.commit()

        row = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).one()
        visible = AgentsStore(db).get_session_events(session.id)

    assert row.provisional_state == "superseded"
    assert visible == []
