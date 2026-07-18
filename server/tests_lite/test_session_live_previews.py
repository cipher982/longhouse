from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

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


def _seed_session(db, *, started_at: datetime, provider: str = "codex") -> AgentSession:
    session = AgentSession(
        provider=provider,
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
    item_id: str | None = None,
    item_seq: int | None = None,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    turn_completed: bool = False,
) -> RuntimeEventIngest:
    payload = {
        "progress_kind": "bridge_live_transcript_delta",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "seq": seq,
        "live_text": live_text,
        "turn_completed": turn_completed,
    }
    if item_id is not None:
        payload["item_id"] = item_id
    if item_seq is not None:
        payload["item_seq"] = item_seq
    return RuntimeEventIngest(
        runtime_key=f"codex:{session_id}",
        session_id=session_id,
        provider="codex",
        device_id="cinder",
        source="codex_bridge_live",
        kind="progress_signal",
        occurred_at=occurred_at,
        dedupe_key=f"bridge:live:{session_id}:{thread_id}:{turn_id}:{item_id or 'legacy'}:{seq}:{live_text}",
        payload=payload,
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
    assert row.last_observation_id.endswith(f"bridge:live:{session.id}:thread-1:turn-1:legacy:2:hello")
    assert preview.text == "hello"
    assert preview.provisional_complete is False


def test_console_tool_event_materializes_truthful_live_tool_preview(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "console_tool_preview.db")
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        event = RuntimeEventIngest(
            runtime_key=f"codex:{session.id}",
            session_id=session.id,
            thread_id=None,
            run_id=None,
            provider="codex",
            device_id="cinder",
            source="codex_console_live",
            kind="progress_signal",
            occurred_at=now,
            dedupe_key="console:tool:run-1:exec-1:2",
            payload={
                "progress_kind": "console_live_tool_item",
                "turn_id": "turn-1",
                "item_id": "exec-1",
                "seq": 2,
                "command": "pwd",
                "output": "/tmp/project\n",
                "status": "completed",
                "completed": True,
            },
        )
        result = ingest_runtime_events(db, [event])
        db.commit()
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert result.accepted == 1
    assert preview.text == "/tmp/project"
    assert preview.tool_name == "exec"
    assert preview.tool_input_json == {"command": "pwd"}
    assert preview.tool_output_text == "/tmp/project\n"
    assert preview.tool_call_id == "exec-1"
    assert preview.tool_call_state == "completed"
    assert preview.provisional_complete is True


def test_cursor_print_assistant_event_materializes_live_preview(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "cursor_print_assistant.db")
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1), provider="cursor")
        thread_id = uuid4()
        run_id = uuid4()
        event = RuntimeEventIngest(
            runtime_key=f"cursor:{session.id}",
            session_id=session.id,
            thread_id=thread_id,
            run_id=run_id,
            provider="cursor",
            device_id="cinder",
            source="cursor_print",
            kind="progress_signal",
            occurred_at=now,
            dedupe_key="cursor:run-1:2",
            payload={
                "progress_kind": "cursor_print_stream",
                "thread_id": str(thread_id),
                "turn_id": "turn-1",
                "seq": 2,
                "event": {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "native Cursor reply"}]},
                },
            },
        )
        result = ingest_runtime_events(db, [event])
        db.commit()
        row = db.get(SessionLivePreview, session.id)
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert result.accepted == 1
    assert preview.text == "native Cursor reply"
    assert row is not None
    assert row.thread_id == str(thread_id)
    assert preview.source == "cursor_print"


def test_cursor_print_tool_event_materializes_live_tool_preview(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "cursor_print_tool.db")
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1), provider="cursor")
        thread_id = uuid4()
        run_id = uuid4()
        event = RuntimeEventIngest(
            runtime_key=f"cursor:{session.id}",
            session_id=session.id,
            thread_id=thread_id,
            run_id=run_id,
            provider="cursor",
            device_id="cinder",
            source="cursor_print",
            kind="progress_signal",
            occurred_at=now,
            dedupe_key="cursor:run-1:3",
            payload={
                "progress_kind": "cursor_print_stream",
                "thread_id": str(thread_id),
                "turn_id": "turn-1",
                "seq": 3,
                "event": {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-1",
                    "tool_call": {
                        "shellToolCall": {
                            "args": {"command": "pwd"},
                            "result": {"success": {"stdout": "/tmp/project\n"}},
                        }
                    },
                },
            },
        )
        result = ingest_runtime_events(db, [event])
        db.commit()
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert result.accepted == 1
    assert preview.text == "/tmp/project"
    assert preview.tool_name == "Shell"
    assert preview.tool_input_json == {"command": "pwd"}
    assert preview.tool_output_text == "/tmp/project\n"
    assert preview.tool_call_id == "call-1"
    assert preview.tool_call_state == "completed"
    assert preview.provisional_complete is True


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


def test_projection_treats_new_item_as_new_preview_even_with_same_turn(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "same_turn_new_item.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))

        ingest_runtime_events(
            db,
            [
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now,
                    seq=1,
                    item_id="item-1",
                    item_seq=1,
                    live_text="first assistant message",
                ),
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(milliseconds=20),
                    seq=2,
                    item_id="item-2",
                    item_seq=1,
                    live_text="second assistant message",
                ),
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview = load_session_live_preview_map(db, [session.id])[str(session.id)]

    assert row is not None
    assert row.seq == 2
    assert row.turn_key == f"codex_bridge_live:{session.id}:thread-1:turn-1#item-2"
    assert row.provisional_cursor == f"codex_bridge_live:{session.id}:thread-1:turn-1#item-2:2"
    assert row.preview_text == "second assistant message"
    assert "first assistant message" not in row.preview_text
    assert preview.text == "second assistant message"


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


def test_late_same_turn_delta_does_not_resurrect_superseded_projection(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "late_superseded_same_turn.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [_bridge_event(session_id=session.id, occurred_at=now, seq=1, live_text="live before durable")],
        )
        supersede_session_live_preview(
            db,
            session_id=session.id,
            durable_at=now + timedelta(seconds=5),
            durable_event_id=123,
        )

        ingest_runtime_events(
            db,
            [
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=2),
                    seq=2,
                    live_text="late stale same turn",
                )
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview_map = load_session_live_preview_map(db, [session.id])

    assert row is not None
    assert row.preview_text == "live before durable"
    assert row.superseded_at is not None
    assert preview_map == {}


def test_late_cross_turn_delta_does_not_resurrect_superseded_projection(tmp_path):
    SessionLocal = _make_sessionmaker(tmp_path, "late_superseded_cross_turn.db")
    now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _seed_session(db, started_at=now - timedelta(minutes=1))
        ingest_runtime_events(
            db,
            [_bridge_event(session_id=session.id, occurred_at=now, seq=1, live_text="live before durable")],
        )
        supersede_session_live_preview(
            db,
            session_id=session.id,
            durable_at=now + timedelta(seconds=5),
            durable_event_id=123,
        )

        ingest_runtime_events(
            db,
            [
                _bridge_event(
                    session_id=session.id,
                    occurred_at=now + timedelta(seconds=2),
                    seq=1,
                    live_text="late stale cross turn",
                    turn_id="turn-2",
                )
            ],
        )
        db.commit()

        row = db.get(SessionLivePreview, session.id)
        preview_map = load_session_live_preview_map(db, [session.id])

    assert row is not None
    assert row.preview_text == "live before durable"
    assert row.superseded_at is not None
    assert preview_map == {}
