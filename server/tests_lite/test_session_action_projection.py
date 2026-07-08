from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.session_workspace import build_session_mobile_tail


def _make_db(tmp_path):
    db_path = tmp_path / "session_action_projection.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ingest_codex_session(db, events: list[EventIngest]):
    session_id = uuid4()
    started_at = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    payload = SessionIngest(
        id=session_id,
        provider="codex",
        environment="test",
        project="longhouse",
        device_id="test-device",
        cwd="/tmp/longhouse",
        started_at=started_at,
        provider_session_id="codex-thread-test",
        events=events,
    )
    store = AgentsStore(db)
    store.ingest_session(payload)
    db.commit()
    return session_id


def _event(
    *,
    role: str,
    content_text: str | None,
    raw_json: str,
    index: int,
) -> EventIngest:
    started_at = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    return EventIngest(
        role=role,
        content_text=content_text,
        timestamp=started_at + timedelta(seconds=index),
        source_path="/tmp/codex-rollout.jsonl",
        source_offset=index * 100,
        raw_json=raw_json,
    )


def _typed_turn_aborted_raw() -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "timestamp": "2026-07-08T20:00:01Z",
            "payload": {
                "type": "turn_aborted",
                "turn_id": "turn_123",
                "reason": "interrupted",
            },
        }
    )


def _marker_turn_aborted_raw(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "timestamp": "2026-07-08T20:00:01Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


def _user_raw(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "timestamp": "2026-07-08T20:00:02Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


def test_mobile_tail_projects_typed_codex_interrupt_as_action(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    try:
        session_id = _ingest_codex_session(
            db,
            [
                _event(
                    role="system",
                    content_text="User interrupted the turn",
                    raw_json=_typed_turn_aborted_raw(),
                    index=1,
                ),
                _event(
                    role="user",
                    content_text="resume with real work",
                    raw_json=_user_raw("resume with real work"),
                    index=2,
                ),
            ],
        )

        tail = build_session_mobile_tail(db=db, session_id=session_id)
        assert [item.kind for item in tail.projection.items] == ["action", "event"]
        action = tail.projection.items[0].action
        assert action is not None
        assert action.kind == "turn_interrupted"
        assert action.provider == "codex"
        assert action.provider_reason == "interrupted"
        assert tail.projection.items[1].event is not None
        assert tail.projection.items[1].event.role == "user"
        assert tail.projection.items[1].event.content_text == "resume with real work"
    finally:
        db.close()


def test_mobile_tail_projects_legacy_codex_marker_as_action_not_user_message(tmp_path):
    session_factory = _make_db(tmp_path)
    db = session_factory()
    marker = "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>"
    try:
        session_id = _ingest_codex_session(
            db,
            [
                _event(
                    role="user",
                    content_text=marker,
                    raw_json=_marker_turn_aborted_raw(marker),
                    index=1,
                ),
                _event(
                    role="user",
                    content_text="actual follow-up",
                    raw_json=_user_raw("actual follow-up"),
                    index=2,
                ),
            ],
        )

        tail = build_session_mobile_tail(db=db, session_id=session_id)
        assert [item.kind for item in tail.projection.items] == ["action", "event"]
        action = tail.projection.items[0].action
        assert action is not None
        assert action.provider_reason == "marker_only"
        rendered_events = [item.event for item in tail.projection.items if item.event is not None]
        assert [event.content_text for event in rendered_events] == ["actual follow-up"]
    finally:
        db.close()
