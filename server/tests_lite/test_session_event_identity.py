"""Tests for live/archive convergence around session event identity."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _make_sessionmaker(tmp_path, name: str):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_event_only_then_full_archive_converges_on_event_identity_and_backfills_source_lines(tmp_path):
    """EventOnly-style ingest must not create a second event when archive catches up."""

    SessionLocal = _make_sessionmaker(tmp_path, "session_event_identity.db")
    timestamp = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    source_path = "/tmp/codex-rollout.jsonl"
    event_line = (
        '{"type":"agent_message","uuid":"event-1","parentUuid":"parent-1",'
        '"message":{"content":[{"type":"output_text","text":"hello"}]}}'
    )
    context_line = '{"type":"session_meta","id":"thread-1","cwd":"/tmp/project"}'

    with SessionLocal() as db:
        store = AgentsStore(db)

        event_only_result = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="identity",
                device_id="machine-1",
                cwd="/tmp/project",
                started_at=timestamp,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="hello",
                        timestamp=timestamp,
                        source_path=source_path,
                        source_offset=100,
                        raw_json=event_line,
                    )
                ],
            )
        )

        assert event_only_result.events_inserted == 1
        session_id = event_only_result.session_id
        first_event = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).one()
        first_identity = (
            first_event.source_path,
            first_event.source_offset,
            first_event.event_hash,
            first_event.event_uuid,
            first_event.parent_event_uuid,
        )
        assert db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).count() == 1

        full_archive_result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="test",
                project="identity",
                device_id="machine-1",
                cwd="/tmp/project",
                started_at=timestamp,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="hello",
                        timestamp=timestamp.replace(second=30),
                        source_path=source_path,
                        source_offset=100,
                        raw_json=event_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=0, raw_json=context_line),
                    SourceLineIngest(source_path=source_path, source_offset=100, raw_json=event_line),
                ],
            )
        )

        events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id).all()
        source_lines = (
            db.query(AgentSourceLine)
            .filter(AgentSourceLine.session_id == session_id)
            .order_by(AgentSourceLine.source_offset.asc())
            .all()
        )

    assert full_archive_result.events_inserted == 0
    assert full_archive_result.events_skipped == 1
    assert len(events) == 1
    assert (
        events[0].source_path,
        events[0].source_offset,
        events[0].event_hash,
        events[0].event_uuid,
        events[0].parent_event_uuid,
    ) == first_identity
    assert [line.source_offset for line in source_lines] == [0, 100]
