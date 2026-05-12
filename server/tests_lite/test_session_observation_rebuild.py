from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_observation_rebuild import rebuild_session_observation_projections
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import record_session_observation
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
        project="observation-rebuild",
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


def _bridge_transcript_event(*, session_id, occurred_at: datetime, live_text: str) -> RuntimeEventIngest:
    return RuntimeEventIngest(
        runtime_key=f"codex:{session_id}",
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge_live",
        kind="progress_signal",
        occurred_at=occurred_at,
        dedupe_key=f"bridge:live:{session_id}:thread-1:turn-1:3",
        payload={
            "progress_kind": "bridge_live_transcript_delta",
            "managed_transport": "codex_app_server",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "seq": 3,
            "method": "item/agentMessage/delta",
            "delta": live_text[-1:],
            "live_text": live_text,
            "turn_completed": True,
        },
    )


def _phase_event(*, session_id, occurred_at: datetime) -> RuntimeEventIngest:
    return RuntimeEventIngest(
        runtime_key=f"codex:{session_id}",
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge",
        kind="phase_signal",
        phase="running",
        tool_name="Bash",
        occurred_at=occurred_at,
        dedupe_key=f"phase:{session_id}:1",
        payload={"managed_transport": "codex_app_server"},
    )


def _event_snapshot(db, session_id) -> list[dict]:
    rows = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc()).all()
    return [
        {
            "role": row.role,
            "content_text": row.content_text,
            "tool_name": row.tool_name,
            "tool_call_id": row.tool_call_id,
            "timestamp": row.timestamp.isoformat(),
            "source_path": row.source_path,
            "source_offset": row.source_offset,
            "event_hash": row.event_hash,
            "branch_id": row.branch_id,
            "event_uuid": row.event_uuid,
            "parent_event_uuid": row.parent_event_uuid,
            "event_origin": row.event_origin,
            "provisional_state": row.provisional_state,
            "provisional_key": row.provisional_key,
            "provisional_seq": row.provisional_seq,
        }
        for row in rows
    ]


def _source_line_snapshot(db, session_id) -> list[dict]:
    rows = (
        db.query(AgentSourceLine)
        .filter(AgentSourceLine.session_id == session_id)
        .order_by(AgentSourceLine.branch_id.asc(), AgentSourceLine.source_path.asc(), AgentSourceLine.source_offset.asc())
        .all()
    )
    return [
        {
            "source_path": row.source_path,
            "source_offset": row.source_offset,
            "branch_id": row.branch_id,
            "revision": row.revision,
            "is_branch_copy": row.is_branch_copy,
            "line_hash": row.line_hash,
            "raw_json": decode_raw_json(row),
        }
        for row in rows
    ]


def _runtime_snapshot(db, session_id) -> list[dict]:
    rows = db.query(SessionRuntimeState).filter(SessionRuntimeState.session_id == session_id).order_by(SessionRuntimeState.runtime_key).all()
    return [
        {
            "runtime_key": row.runtime_key,
            "provider": row.provider,
            "device_id": row.device_id,
            "phase": row.phase,
            "phase_source": row.phase_source,
            "active_tool": row.active_tool,
            "last_runtime_signal_at": row.last_runtime_signal_at.isoformat() if row.last_runtime_signal_at else None,
            "last_progress_at": row.last_progress_at.isoformat() if row.last_progress_at else None,
            "last_live_at": row.last_live_at.isoformat() if row.last_live_at else None,
            "terminal_state": row.terminal_state,
            "terminal_reason": row.terminal_reason,
            "terminal_source": row.terminal_source,
            "runtime_version": row.runtime_version,
        }
        for row in rows
    ]


def test_session_observation_rebuild_recovers_transcript_archive_and_runtime(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    assistant_line = (
        '{"type":"response_item","timestamp":"2026-05-12T12:00:02Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"hello from durable"}]}}'
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(db, [_bridge_transcript_event(session_id=session.id, occurred_at=now, live_text="hello from durable")])
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session.id,
                provider="codex",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now - timedelta(minutes=1),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="hello from durable",
                        timestamp=now + timedelta(seconds=2),
                        source_path=source_path,
                        source_offset=100,
                        raw_json=assistant_line,
                    )
                ],
                source_lines=[SourceLineIngest(source_path=source_path, source_offset=100, raw_json=assistant_line)],
            )
        )
        ingest_runtime_events(db, [_phase_event(session_id=session.id, occurred_at=now + timedelta(seconds=5))])
        db.commit()

        observation_kinds = [
            row.kind for row in db.query(SessionObservation).filter(SessionObservation.session_id == session.id).order_by(SessionObservation.id).all()
        ]
        before_events = _event_snapshot(db, session.id)
        before_source_lines = _source_line_snapshot(db, session.id)
        before_runtime = _runtime_snapshot(db, session.id)

        result = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=f"codex:{session.id}")
        db.commit()

        after_events = _event_snapshot(db, session.id)
        after_source_lines = _source_line_snapshot(db, session.id)
        after_runtime = _runtime_snapshot(db, session.id)

    assert "bridge_transcript_delta" in observation_kinds
    assert "provider_event" in observation_kinds
    assert "provider_source_line" in observation_kinds
    assert "runtime_signal" in observation_kinds
    assert result.reducer_errors == ()
    assert result.provider_events_reduced == 1
    assert result.bridge_events_reduced == 1
    assert result.source_lines_reduced == 1
    assert result.runtime_signals_reduced >= 1
    assert after_events == before_events
    assert after_source_lines == before_source_lines
    assert after_runtime == before_runtime


def test_session_observation_rebuild_is_idempotent(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_idempotent.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(db, [_bridge_transcript_event(session_id=session.id, occurred_at=now, live_text="same after replay")])
        db.commit()

        first = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=f"codex:{session.id}")
        second = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=f"codex:{session.id}")
        db.commit()

        events = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()

    assert first.agent_events == 1
    assert second.agent_events == 1
    assert len(events) == 1
    assert events[0].content_text == "same after replay"


def test_session_observation_rebuild_reports_reducer_errors(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_errors.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        record_session_observation(
            db,
            observation_id=f"provider_event:bad:{session.id}",
            session_id=session.id,
            runtime_key=None,
            provider="codex",
            device_id="cinder",
            source_domain="transcript",
            source="test",
            kind=OBS_KIND_PROVIDER_EVENT,
            observed_at=now,
            payload={"branch_id": 1},
        )
        db.commit()

        result = rebuild_session_observation_projections(db, session_id=session.id)

    assert len(result.reducer_errors) == 1
    assert result.reducer_errors[0].kind == "provider_event"
    assert "missing required reducer payload" in result.reducer_errors[0].error
    assert result.agent_events == 0
