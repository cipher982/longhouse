from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionLivePreview
from zerg.models.agents import SessionObservation
from zerg.services import provisional_events as provisional_events_service
from zerg.services import session_runtime as session_runtime_service
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.provisional_events import cleanup_bridge_transcript_preview_observations
from zerg.services.provisional_events import load_active_provisional_preview_map
from zerg.services.session_observation_reducers import reduce_bridge_transcript_observation
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.session_execution_home import SessionExecutionHome


def _make_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_initialized_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    initialize_database(engine)
    return engine, sessionmaker(bind=engine)


def _seed_managed_codex_session(db, *, started_at: datetime) -> AgentSession:
    session = AgentSession(
        provider="codex",
        environment="test",
        project="live-overlay-contract",
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


def _ingest_durable_session(db, *, session: AgentSession, now: datetime) -> None:
    source_path = "/tmp/codex-rollout.jsonl"
    events = [
        EventIngest(
            role="assistant",
            content_text="I am using noteit.",
            timestamp=now + timedelta(seconds=1),
            source_path=source_path,
            source_offset=100,
            raw_json='{"type":"event_msg","payload":{"type":"agent_message","message":"I am using noteit."}}',
        ),
        EventIngest(
            role="assistant",
            tool_name="apply_patch",
            tool_input_json={"patch": "*** Begin Patch"},
            tool_call_id="call-1",
            timestamp=now + timedelta(seconds=2),
            source_path=source_path,
            source_offset=200,
            raw_json='{"type":"function_call","call_id":"call-1","name":"apply_patch"}',
        ),
        EventIngest(
            role="tool",
            tool_output_text="Success. Updated note.",
            tool_call_id="call-1",
            timestamp=now + timedelta(seconds=3),
            source_path=source_path,
            source_offset=300,
            raw_json='{"type":"function_call_output","call_id":"call-1","output":"Success. Updated note."}',
        ),
        EventIngest(
            role="assistant",
            content_text="Updated the existing note.",
            timestamp=now + timedelta(seconds=4),
            source_path=source_path,
            source_offset=400,
            raw_json='{"type":"event_msg","payload":{"type":"agent_message","message":"Updated the existing note."}}',
        ),
    ]
    source_lines = [
        SourceLineIngest(
            source_path=source_path, source_offset=event.source_offset or 0, raw_json=event.raw_json or "{}"
        )
        for event in events
    ]
    AgentsStore(db).ingest_session(
        SessionIngest(
            id=session.id,
            provider="codex",
            environment="test",
            project=session.project,
            device_id=session.device_id,
            cwd=session.cwd,
            started_at=session.started_at,
            events=events,
            source_lines=source_lines,
        )
    )


def test_live_bridge_snapshots_store_observations_not_events(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_observations.db")
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
                _bridge_transcript_event(
                    session_id=session.id, occurred_at=now + timedelta(milliseconds=40), seq=1, live_text="h"
                ),
            ],
        )
        db.commit()

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()
        observations = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session.id)
            .order_by(SessionObservation.id)
            .all()
        )
        visible = AgentsStore(db).get_session_events(session.id)
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert result.accepted == 2
    assert result.duplicates == 1
    assert rows == []
    assert visible == []
    assert [observation.kind for observation in observations] == [
        OBS_KIND_BRIDGE_TRANSCRIPT_DELTA,
        OBS_KIND_BRIDGE_TRANSCRIPT_DELTA,
    ]
    assert observations[0].source_domain == "runtime"
    assert observations[0].source == "codex_bridge_live"
    assert json.loads(observations[1].payload_json or "{}")["payload"]["live_text"] == "hello"
    assert preview.text == "hello"
    assert preview.provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:2"
    assert preview.provisional_complete is False


def test_live_bridge_ingest_uses_observation_write_fast_path(monkeypatch, tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_fast_path.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    load_flags: list[bool] = []
    original_record_runtime_observation = session_runtime_service.record_runtime_observation

    def _record_runtime_observation_spy(db, event, **kwargs):
        load_flags.append(bool(kwargs.get("load_observation", True)))
        return original_record_runtime_observation(db, event, **kwargs)

    monkeypatch.setattr(
        session_runtime_service,
        "record_runtime_observation",
        _record_runtime_observation_spy,
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))

        result = ingest_runtime_events(
            db,
            [_bridge_transcript_event(session_id=session.id, occurred_at=now, seq=1, live_text="fast preview")],
        )
        db.commit()

        observation = db.query(SessionObservation).filter(SessionObservation.session_id == session.id).one()
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert load_flags == [False]
    assert result.accepted == 1
    assert result.updated_runtime_keys == [f"codex:{session.id}"]
    assert observation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
    assert preview.text == "fast preview"


def test_active_preview_loader_caps_observations_per_session(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_DISABLE_LIVE_PREVIEW_PROJECTION", "1")
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_preview_cap.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    seen_row_ids: list[int] = []
    original_candidate_loader = provisional_events_service._preview_candidate_from_bridge_observation

    def _candidate_loader_spy(row):
        seen_row_ids.append(int(row.id))
        return original_candidate_loader(row)

    monkeypatch.setattr(provisional_events_service, "MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION", 3)
    monkeypatch.setattr(
        provisional_events_service,
        "_preview_candidate_from_bridge_observation",
        _candidate_loader_spy,
    )

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=idx),
                    seq=idx,
                    live_text=f"preview {idx}",
                )
                for idx in range(1, 9)
            ],
        )
        db.commit()

        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert preview.text == "preview 8"
    assert len(seen_row_ids) == 3
    assert seen_row_ids == sorted(seen_row_ids, reverse=True)


def test_active_preview_loader_uses_session_activity_without_scanning_events(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_preview_no_event_scan.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="newer durable row that should not be scanned by the preview loader",
                timestamp=now + timedelta(minutes=5),
                event_origin="durable",
            )
        )
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=1,
                    live_text="bounded preview",
                )
            ],
        )
        db.commit()

        statements: list[str] = []

        def _collect_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        bind = db.get_bind()
        sqlalchemy_event.listen(bind, "before_cursor_execute", _collect_statement)
        try:
            preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]
        finally:
            sqlalchemy_event.remove(bind, "before_cursor_execute", _collect_statement)

    assert preview.text == "bounded preview"
    assert not any("FROM events" in statement for statement in statements)
    assert not any("FROM session_observations" in statement for statement in statements)


def test_active_preview_loader_does_not_scan_observations_when_projection_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("LONGHOUSE_DISABLE_LIVE_PREVIEW_PROJECTION", raising=False)
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_no_projection_fallback.db")
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
                    live_text="observation only preview",
                )
            ],
        )
        db.query(SessionLivePreview).filter(SessionLivePreview.session_id == session.id).delete()
        db.commit()

        projection_preview = load_active_provisional_preview_map(db, [session.id])
        monkeypatch.setenv("LONGHOUSE_DISABLE_LIVE_PREVIEW_PROJECTION", "1")
        observation_preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert projection_preview == {}
    assert observation_preview.text == "observation only preview"


def test_live_preview_cleanup_keeps_bounded_latest_window(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_preview_retention.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=idx),
                    seq=idx,
                    live_text=f"preview {idx}",
                )
                for idx in range(1, 9)
            ],
        )
        db.commit()

        removed = cleanup_bridge_transcript_preview_observations(
            db,
            session_ids=[session.id],
            keep_per_session=3,
            batch_size=10,
        )
        remaining = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session.id)
            .order_by(SessionObservation.observed_at.asc(), SessionObservation.id.asc())
            .all()
        )
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert removed == 5
    assert [json.loads(row.payload_json or "{}")["payload"]["seq"] for row in remaining] == [6, 7, 8]
    assert preview.text == "preview 8"


def test_live_preview_cleanup_removes_rows_covered_by_durable_activity(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_preview_durable_retention.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(session_id=session.id, occurred_at=now, seq=1, live_text="old preview"),
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=10),
                    seq=2,
                    live_text="current preview",
                ),
            ],
        )
        _ingest_durable_session(db, session=session, now=now)
        db.commit()

        removed = cleanup_bridge_transcript_preview_observations(
            db,
            session_ids=[session.id],
            keep_per_session=10,
            batch_size=10,
        )
        remaining = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session.id)
            .filter(SessionObservation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
            .all()
        )
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert removed == 0
    assert len(remaining) == 1
    assert preview.text == "current preview"


def test_runtime_ingest_prunes_bridge_preview_window_on_write(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_preview_write_retention.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    keep = provisional_events_service.BRIDGE_TRANSCRIPT_OBSERVATION_KEEP_PER_SESSION

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=idx),
                    seq=idx,
                    live_text=f"preview {idx}",
                )
                for idx in range(1, keep + 8)
            ],
        )
        db.commit()
        remaining = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session.id)
            .filter(SessionObservation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
            .order_by(SessionObservation.observed_at.asc(), SessionObservation.id.asc())
            .all()
        )
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert len(remaining) == keep
    assert json.loads(remaining[0].payload_json or "{}")["payload"]["seq"] == 8
    assert preview.text == f"preview {keep + 7}"


def test_bridge_transcript_observation_rebuild_does_not_create_event(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_rebuild.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [_bridge_transcript_event(session_id=session.id, occurred_at=now, seq=7, live_text="preview only")],
        )
        db.commit()

        observation = db.query(SessionObservation).filter(SessionObservation.session_id == session.id).one()
        rebuilt = reduce_bridge_transcript_observation(db, observation)
        db.commit()

        rows = db.query(AgentEvent).filter(AgentEvent.session_id == session.id).all()
        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert rebuilt is None
    assert rows == []
    assert preview.text == "preview only"


def test_cumulative_live_snapshot_does_not_merge_durable_tool_sequence(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_merge_regression.db")
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
                    live_text="I am using noteit.Updated the existing note.",
                    turn_completed=True,
                )
            ],
        )
        _ingest_durable_session(db, session=session, now=now)
        db.commit()

        events = AgentsStore(db).get_session_events(session.id)
        count = AgentsStore(db).count_session_events(session.id)
        rows = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session.id)
            .order_by(AgentEvent.timestamp, AgentEvent.id)
            .all()
        )
        previews = load_active_provisional_preview_map(db, [session.id])

    assert len(rows) == 4
    assert count == 4
    assert [(event.role, event.content_text, event.tool_name, event.tool_call_id) for event in events] == [
        ("assistant", "I am using noteit.", None, None),
        ("assistant", None, "apply_patch", "call-1"),
        ("tool", None, None, "call-1"),
        ("assistant", "Updated the existing note.", None, None),
    ]
    assert previews == {}


def test_bridge_preview_uses_only_live_deltas_after_latest_durable_activity(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "live_overlay_after_durable.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [_bridge_transcript_event(session_id=session.id, occurred_at=now, seq=1, live_text="old preview")],
        )
        _ingest_durable_session(db, session=session, now=now)
        ingest_runtime_events(
            db,
            [
                _bridge_transcript_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=5),
                    seq=2,
                    live_text="new preview",
                )
            ],
        )
        db.commit()

        preview = load_active_provisional_preview_map(db, [session.id])[str(session.id)]

    assert preview.text == "new preview"
    assert preview.provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1:2"


def test_cross_session_search_ignores_live_preview_text(tmp_path):
    _, SessionLocal = _make_initialized_sessionmaker(tmp_path, "live_overlay_search.db")
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
                    session_id=session.id, occurred_at=now, seq=1, live_text="only live preview needle"
                )
            ],
        )
        db.commit()

        store = AgentsStore(db)
        preview_sessions, preview_total = store.list_sessions(include_test=True, query="needle")
        assert preview_sessions == []
        assert preview_total == 0

        store.ingest_session(
            SessionIngest(
                id=session.id,
                provider="codex",
                environment="test",
                project="live-overlay-contract",
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


def test_initialize_database_deletes_legacy_live_provisional_rows(tmp_path):
    engine, SessionLocal = _make_initialized_sessionmaker(tmp_path, "live_overlay_cleanup.db")
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_managed_codex_session(db, started_at=now - timedelta(minutes=1))
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="legacy live preview",
                timestamp=now,
                event_origin="live_provisional",
                provisional_state="active",
                provisional_key=f"codex_bridge_live:{session.id}:thread-1:turn-1",
            )
        )
        db.add(
            AgentEvent(
                session_id=session.id,
                role="assistant",
                content_text="durable text",
                timestamp=now + timedelta(seconds=1),
                event_origin="durable",
            )
        )
        db.commit()

    initialize_database(engine)

    with SessionLocal() as db:
        rows = db.query(AgentEvent).order_by(AgentEvent.timestamp).all()

    assert [(row.event_origin, row.content_text) for row in rows] == [("durable", "durable text")]
