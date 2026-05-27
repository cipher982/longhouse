from __future__ import annotations

import asyncio
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from zerg.cli.main import app as cli_app
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.raw_json_compression import decode_raw_json
from zerg.services.session_observation_rebuild import SessionObservationRebuildCoverageError
from zerg.services.session_observation_rebuild import rebuild_session_observation_projections
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import SOURCE_DOMAIN_TRANSCRIPT
from zerg.services.session_observations import record_session_observation
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.timeline_session_listing import TimelineSessionListParams
from zerg.services.timeline_session_listing import list_timeline_sessions_for_browser
from zerg.session_execution_home import SessionExecutionHome


def _make_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_initialized_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return sessionmaker(bind=engine)


def _api_client(SessionLocal) -> TestClient:
    def override_get_db():
        with SessionLocal() as db:
            yield db

    def override_verify_agents_token():
        return SimpleNamespace(device_id="observation-rebuild-test", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


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


def _terminal_event(*, session_id, occurred_at: datetime) -> RuntimeEventIngest:
    return RuntimeEventIngest(
        runtime_key=f"codex:{session_id}",
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge",
        kind="terminal_signal",
        occurred_at=occurred_at,
        dedupe_key=f"terminal:{session_id}:1",
        payload={
            "terminal_state": "session_ended",
            "terminal_reason": "process_exit",
            "terminal_source": "codex_bridge",
        },
    )


def _event_snapshot(db, session_id) -> list[dict]:
    rows = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc()).all()
    return [
        {
            "role": row.role,
            "content_text": row.content_text,
            "tool_name": row.tool_name,
            "tool_input_json": row.tool_input_json,
            "tool_output_text": row.tool_output_text,
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


def _product_surface_snapshot(db, session_id) -> dict:
    store = AgentsStore(db)
    session = store.get_session(session_id)
    assert session is not None
    visible_events = store.get_session_events(session_id, limit=20)
    export_result = store.export_session_jsonl(session_id)
    query_sessions, query_total = store.list_sessions(
        include_test=True,
        project="observation-rebuild",
        provider="codex",
        query="durable",
        limit=10,
    )
    timeline_result = asyncio.run(
        list_timeline_sessions_for_browser(
            db=db,
            params=TimelineSessionListParams(
                project="observation-rebuild",
                provider="codex",
                environment=None,
                include_test=True,
                hide_autonomous=True,
                device_id=None,
                days_back=14,
                query=None,
                limit=10,
                offset=0,
                sort=None,
                mode="lexical",
                context_mode="forensic",
            ),
        )
    )
    assert export_result is not None
    assert not timeline_result.compatibility_raw
    assert hasattr(timeline_result.response, "sessions")
    cards = timeline_result.response.sessions
    card = next(card for card in cards if card.head.id == str(session_id))
    transcript_preview = card.head.transcript_preview
    runtime_display = card.head.runtime_display
    return {
        "visible_events": [
            {
                "role": event.role,
                "content_text": event.content_text,
                "event_origin": event.event_origin,
                "provisional_state": event.provisional_state,
            }
            for event in visible_events
        ],
        "visible_event_count": store.count_session_events(session_id),
        "session_counts": {
            "user_messages": session.user_messages,
            "assistant_messages": session.assistant_messages,
            "tool_calls": session.tool_calls,
            "last_activity_at": session.last_activity_at.isoformat() if session.last_activity_at else None,
            "transcript_revision": session.transcript_revision,
        },
        "fts_row_count": int(
            db.execute(text("SELECT count(*) FROM events_fts JOIN events e ON e.id = events_fts.rowid WHERE e.session_id = :sid"), {"sid": str(session_id)}).scalar()
            or 0
        ),
        "query_total": query_total,
        "query_session_ids": [str(session.id) for session in query_sessions],
        "export_jsonl": export_result[0].decode("utf-8"),
        "timeline": {
            "total": timeline_result.response.total,
            "thread_id": card.thread_id,
            "head_id": card.head.id,
            "timeline_anchor_at": card.timeline_anchor_at.isoformat() if card.timeline_anchor_at else None,
            "preview_text": transcript_preview.text if transcript_preview else None,
            "preview_is_provisional": transcript_preview.is_provisional if transcript_preview else None,
            "display_phase": card.head.display_phase,
            "runtime_status": card.head.status,
            "runtime_phase": card.head.runtime_phase,
            "runtime_display_lifecycle": runtime_display.lifecycle if runtime_display else None,
            "timeline_status_label": card.head.timeline_card.status.label if card.head.timeline_card.status else None,
            "timeline_border_tone": card.head.timeline_card.border_tone,
        },
    }


def _api_surface_snapshot(client: TestClient, session_id) -> dict:
    headers = {"X-Agents-Token": "dev"}
    list_response = client.get(
        "/agents/sessions?include_test=true&hide_autonomous=false&project=observation-rebuild&provider=codex&query=durable&limit=10",
        headers=headers,
    )
    detail_response = client.get(f"/agents/sessions/{session_id}", headers=headers)
    events_response = client.get(f"/agents/sessions/{session_id}/events?limit=20", headers=headers)
    export_response = client.get(f"/agents/sessions/{session_id}/export", headers=headers)

    assert list_response.status_code == 200, list_response.text
    assert detail_response.status_code == 200, detail_response.text
    assert events_response.status_code == 200, events_response.text
    assert export_response.status_code == 200, export_response.text

    list_payload = list_response.json()
    session_row = next(row for row in list_payload["sessions"] if row["id"] == str(session_id))
    detail_payload = detail_response.json()
    events_payload = events_response.json()
    return {
        "list_total": list_payload["total"],
        "list_session": {
            "id": session_row["id"],
            "project": session_row["project"],
            "provider": session_row["provider"],
            "transcript_preview": session_row.get("transcript_preview"),
            "display_phase": session_row.get("display_phase"),
            "status": session_row.get("status"),
            "runtime_phase": session_row.get("runtime_phase"),
        },
        "detail": {
            "id": detail_payload["id"],
            "project": detail_payload["project"],
            "provider": detail_payload["provider"],
            "transcript_preview": detail_payload.get("transcript_preview"),
            "display_phase": detail_payload.get("display_phase"),
            "status": detail_payload.get("status"),
            "runtime_phase": detail_payload.get("runtime_phase"),
        },
        "events": {
            "total": events_payload["total"],
            "items": [
                {
                    "role": event["role"],
                    "content_text": event["content_text"],
                    "event_origin": event.get("event_origin"),
                    "provisional_state": event.get("provisional_state"),
                }
                for event in events_payload["events"]
            ],
        },
        "export_jsonl": export_response.content.decode("utf-8"),
    }


def _damage_session_metadata(db, session_id) -> None:
    session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
    session.user_messages = 0
    session.assistant_messages = 0
    session.tool_calls = 0
    session.transcript_revision = 0
    session.needs_embedding = 0
    session.last_activity_at = session.started_at
    db.flush()


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


def test_session_observation_rebuild_preserves_product_surface_parity(tmp_path):
    SessionLocal = _make_initialized_sessionmaker(tmp_path, "observation_rebuild_surface_parity.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    assistant_line = (
        '{"type":"response_item","timestamp":"2026-05-12T12:00:02Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"durable searchable transcript"}]}}'
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now,
                    live_text="durable searchable transcript",
                )
            ],
        )
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
                        content_text="durable searchable transcript",
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

        before = _product_surface_snapshot(db, session.id)
        _damage_session_metadata(db, session.id)
        result = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=f"codex:{session.id}")
        db.commit()
        after = _product_surface_snapshot(db, session.id)

    assert result.reducer_errors == ()
    assert after == before
    assert before["visible_events"] == [
        {
            "role": "assistant",
            "content_text": "durable searchable transcript",
            "event_origin": "durable",
            "provisional_state": None,
        }
    ]
    assert before["query_session_ids"] == [str(session.id)]
    assert before["export_jsonl"] == assistant_line + "\n"


def test_session_observation_rebuild_preserves_agent_api_surface_parity(tmp_path):
    SessionLocal = _make_initialized_sessionmaker(tmp_path, "observation_rebuild_api_surface_parity.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-api-rollout.jsonl"
    assistant_line = (
        '{"type":"response_item","timestamp":"2026-05-12T12:00:02Z",'
        '"payload":{"type":"message","role":"assistant",'
        '"content":[{"type":"output_text","text":"durable API transcript"}]}}'
    )

    try:
        with SessionLocal() as db:
            session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
            ingest_runtime_events(
                db,
                [
                    _bridge_transcript_event(
                        session_id=session.id,
                        occurred_at=now,
                        live_text="durable API transcript",
                    )
                ],
            )
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
                            content_text="durable API transcript",
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
            session_id = session.id

        client = _api_client(SessionLocal)
        before = _api_surface_snapshot(client, session_id)
        with SessionLocal() as db:
            _damage_session_metadata(db, session_id)
            result = rebuild_session_observation_projections(db, session_id=session_id, runtime_key=f"codex:{session_id}")
            db.commit()
        after = _api_surface_snapshot(client, session_id)
    finally:
        api_app.dependency_overrides.clear()

    assert result.reducer_errors == ()
    assert after == before
    assert before["events"]["items"] == [
        {
            "role": "assistant",
            "content_text": "durable API transcript",
            "event_origin": "durable",
            "provisional_state": None,
        }
    ]
    assert before["export_jsonl"] == assistant_line + "\n"


def test_session_observation_rebuild_preserves_multi_batch_transcript_revision(tmp_path):
    SessionLocal = _make_initialized_sessionmaker(tmp_path, "observation_rebuild_multi_batch_revision.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/multi-batch.jsonl"
    first_line = '{"type":"assistant","text":"first batch"}'
    same_batch_line = '{"type":"assistant","text":"same ingest batch"}'
    second_line = '{"type":"assistant","text":"second batch"}'

    with SessionLocal() as db:
        created = AgentsStore(db).ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="first batch",
                        timestamp=now + timedelta(seconds=1),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=first_line,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="same ingest batch",
                        timestamp=now + timedelta(seconds=2),
                        source_path=source_path,
                        source_offset=20,
                        raw_json=same_batch_line,
                    ),
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=10, raw_json=first_line),
                    SourceLineIngest(source_path=source_path, source_offset=20, raw_json=same_batch_line),
                ],
            )
        )
        session_id = created.session_id
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="second batch",
                        timestamp=now + timedelta(seconds=3),
                        source_path=source_path,
                        source_offset=30,
                        raw_json=second_line,
                    )
                ],
                source_lines=[SourceLineIngest(source_path=source_path, source_offset=30, raw_json=second_line)],
            )
        )
        before_revision = db.query(AgentSession.transcript_revision).filter(AgentSession.id == session_id).scalar()
        _damage_session_metadata(db, session_id)

        result = rebuild_session_observation_projections(db, session_id=session_id)
        db.commit()
        after_revision = db.query(AgentSession.transcript_revision).filter(AgentSession.id == session_id).scalar()

    assert result.reducer_errors == ()
    assert before_revision == 2
    assert after_revision == before_revision


def test_session_observation_rebuild_preserves_source_line_revision_history(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_source_revision.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/revision-session.jsonl"
    first_line = '{"type":"assistant","text":"first revision"}'
    second_line = '{"type":"assistant","text":"second revision"}'

    with SessionLocal() as db:
        result = AgentsStore(db).ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=10, raw_json=first_line),
                    SourceLineIngest(source_path=source_path, source_offset=10, raw_json=second_line),
                ],
            )
        )
        session_id = result.session_id
        db.commit()

        before_source_lines = _source_line_snapshot(db, session_id)
        result = rebuild_session_observation_projections(db, session_id=session_id)
        db.commit()
        after_source_lines = _source_line_snapshot(db, session_id)

    assert result.reducer_errors == ()
    assert after_source_lines == before_source_lines
    assert [(row["revision"], row["raw_json"]) for row in before_source_lines] == [
        (1, first_line),
        (2, second_line),
    ]


def test_session_observation_rebuild_preserves_tool_call_pairing_fields(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_tool_pairing.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/tool-session.jsonl"

    with SessionLocal() as db:
        result = AgentsStore(db).ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(
                        role="assistant",
                        tool_name="Bash",
                        tool_input_json={"command": "ls -la"},
                        tool_call_id="toolu_rebuild",
                        timestamp=now + timedelta(seconds=1),
                        source_path=source_path,
                        source_offset=10,
                        raw_json='{"type":"assistant","tool":"Bash"}',
                    ),
                    EventIngest(
                        role="tool",
                        tool_name="Bash",
                        tool_output_text="total 8",
                        tool_call_id="toolu_rebuild",
                        timestamp=now + timedelta(seconds=2),
                        source_path=source_path,
                        source_offset=20,
                        raw_json='{"type":"tool_result","content":"total 8"}',
                    ),
                ],
            )
        )
        session_id = result.session_id
        db.commit()

        before_events = _event_snapshot(db, session_id)
        rebuild_result = rebuild_session_observation_projections(db, session_id=session_id)
        db.commit()
        after_events = _event_snapshot(db, session_id)

    assert rebuild_result.reducer_errors == ()
    assert after_events == before_events
    assert [
        {
            "role": event["role"],
            "tool_name": event["tool_name"],
            "tool_input_json": event["tool_input_json"],
            "tool_output_text": event["tool_output_text"],
            "tool_call_id": event["tool_call_id"],
        }
        for event in before_events
    ] == [
        {
            "role": "assistant",
            "tool_name": "Bash",
            "tool_input_json": {"command": "ls -la"},
            "tool_output_text": None,
            "tool_call_id": "toolu_rebuild",
        },
        {
            "role": "tool",
            "tool_name": "Bash",
            "tool_input_json": None,
            "tool_output_text": "total 8",
            "tool_call_id": "toolu_rebuild",
        },
    ]


def test_session_observation_rebuild_cli_restores_projection_rows(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'observation_rebuild_cli.db'}"
    engine = make_engine(db_url)
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/cli-rebuild.jsonl"
    raw_line = '{"type":"assistant","text":"restored by cli"}'

    with SessionLocal() as db:
        result = AgentsStore(db).ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="restored by cli",
                        timestamp=now + timedelta(seconds=1),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=raw_line,
                    )
                ],
                source_lines=[SourceLineIngest(source_path=source_path, source_offset=10, raw_json=raw_line)],
            )
        )
        session_id = result.session_id
        _damage_session_metadata(db, session_id)
        db.query(AgentEvent).filter(AgentEvent.session_id == session_id).delete(synchronize_session=False)
        db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).delete(synchronize_session=False)
        db.commit()

    cli_result = CliRunner().invoke(cli_app, ["rebuild-session", str(session_id), "--database-url", db_url, "--json"])
    assert cli_result.exit_code == 0, cli_result.output
    payload = json.loads(cli_result.output)
    assert payload["session_id"] == str(session_id)
    assert payload["agent_events"] == 1
    assert payload["source_lines"] == 1
    assert payload["reducer_errors"] == []

    with SessionLocal() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        events = _event_snapshot(db, session_id)
        source_lines = _source_line_snapshot(db, session_id)

    assert session.assistant_messages == 1
    assert session.transcript_revision == 1
    assert events[0]["content_text"] == "restored by cli"
    assert source_lines[0]["raw_json"] == raw_line


def test_session_observation_rebuild_refuses_uncovered_transcript_projection(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_uncovered_transcript.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="direct projection row",
                timestamp=now,
            )
        )
        db.commit()

        with pytest.raises(SessionObservationRebuildCoverageError, match="no transcript observations"):
            rebuild_session_observation_projections(db, session_id=session.id)

        events = _event_snapshot(db, session.id)

    assert [event["content_text"] for event in events] == ["direct projection row"]


def test_session_observation_rebuild_refuses_newer_observation_coverage(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_newer_coverage.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="older direct projection row",
                timestamp=now,
            )
        )
        record_session_observation(
            db,
            observation_id=f"provider_event:newer:{session.id}",
            session_id=session.id,
            runtime_key=None,
            provider="codex",
            device_id="cinder",
            source_domain=SOURCE_DOMAIN_TRANSCRIPT,
            source="test",
            kind=OBS_KIND_PROVIDER_EVENT,
            observed_at=now + timedelta(seconds=1),
            payload={"branch_id": 1},
        )
        db.commit()

        with pytest.raises(SessionObservationRebuildCoverageError, match="newer than oldest transcript projection"):
            rebuild_session_observation_projections(db, session_id=session.id)

        events = _event_snapshot(db, session.id)

    assert [event["content_text"] for event in events] == ["older direct projection row"]


def test_session_observation_rebuild_cli_refuses_uncovered_projection_rows(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'observation_rebuild_cli_refuse.db'}"
    engine = make_engine(db_url)
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="keep me",
                timestamp=now,
            )
        )
        db.commit()
        session_id = session.id

    cli_result = CliRunner().invoke(cli_app, ["rebuild-session", str(session_id), "--database-url", db_url, "--json"])
    assert cli_result.exit_code == 1, cli_result.output
    payload = json.loads(cli_result.output)
    assert payload["error"] == "coverage_gap"
    assert "no transcript observations" in payload["detail"]

    with SessionLocal() as db:
        events = _event_snapshot(db, session_id)

    assert [event["content_text"] for event in events] == ["keep me"]


def test_session_observation_rebuild_preserves_rewind_branch_head_projection(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_rewind_branch.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/rewind-session.jsonl"
    line0 = '{"type":"user","text":"start"}'
    line10_old = '{"type":"assistant","text":"old middle"}'
    line20_old = '{"type":"assistant","text":"old tail"}'
    line10_new = '{"type":"assistant","text":"rewritten middle"}'
    line30_new = '{"type":"assistant","text":"new tail"}'

    with SessionLocal() as db:
        first = AgentsStore(db).ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(role="user", content_text="start", timestamp=now, source_path=source_path, source_offset=0, raw_json=line0),
                    EventIngest(
                        role="assistant",
                        content_text="old middle",
                        timestamp=now + timedelta(seconds=1),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_old,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old tail",
                        timestamp=now + timedelta(seconds=2),
                        source_path=source_path,
                        source_offset=20,
                        raw_json=line20_old,
                    ),
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=0, raw_json=line0),
                    SourceLineIngest(source_path=source_path, source_offset=10, raw_json=line10_old),
                    SourceLineIngest(source_path=source_path, source_offset=20, raw_json=line20_old),
                ],
            )
        )
        session_id = first.session_id
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="observation-rebuild",
                device_id="cinder",
                cwd="/tmp/project",
                started_at=now,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="rewritten middle",
                        timestamp=now + timedelta(seconds=3),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_new,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="new tail",
                        timestamp=now + timedelta(seconds=4),
                        source_path=source_path,
                        source_offset=30,
                        raw_json=line30_new,
                    ),
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=10, raw_json=line10_new),
                    SourceLineIngest(source_path=source_path, source_offset=30, raw_json=line30_new),
                ],
            )
        )
        db.commit()

        store = AgentsStore(db)
        before_head_events = [event.content_text for event in store.get_session_events(session_id, branch_mode="head", limit=100)]
        before_head_export = store.export_session_jsonl(session_id, branch_mode="head")[0].decode("utf-8")
        before_all_export = store.export_session_jsonl(session_id, branch_mode="all")[0].decode("utf-8")

        result = rebuild_session_observation_projections(db, session_id=session_id)
        db.commit()

        after_store = AgentsStore(db)
        after_head_events = [event.content_text for event in after_store.get_session_events(session_id, branch_mode="head", limit=100)]
        after_head_export = after_store.export_session_jsonl(session_id, branch_mode="head")[0].decode("utf-8")
        after_all_export = after_store.export_session_jsonl(session_id, branch_mode="all")[0].decode("utf-8")

    assert result.reducer_errors == ()
    assert before_head_events == ["start", "rewritten middle", "new tail"]
    assert after_head_events == before_head_events
    assert after_head_export == before_head_export == "\n".join([line0, line10_new, line30_new]) + "\n"
    assert after_all_export == before_all_export == "\n".join([line0, line10_old, line20_old, line10_new, line30_new]) + "\n"


def test_session_observation_rebuild_preserves_out_of_order_runtime_state(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "observation_rebuild_out_of_order_runtime.db")
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _terminal_event(session_id=session.id, occurred_at=now + timedelta(seconds=10)),
                _phase_event(session_id=session.id, occurred_at=now + timedelta(seconds=5)),
            ],
        )
        db.commit()

        before_runtime = _runtime_snapshot(db, session.id)
        result = rebuild_session_observation_projections(db, session_id=session.id, runtime_key=f"codex:{session.id}")
        db.commit()
        after_runtime = _runtime_snapshot(db, session.id)

    assert result.reducer_errors == ()
    assert after_runtime == before_runtime
    assert after_runtime[0]["terminal_state"] == "session_ended"
    assert after_runtime[0]["phase"] == "finished"


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

    assert first.bridge_events_reduced == 1
    assert second.bridge_events_reduced == 1
    assert first.agent_events == 0
    assert second.agent_events == 0
    assert events == []


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
