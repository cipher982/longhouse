from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSessionBranch
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_store(tmp_path):
    db_path = tmp_path / "event_lineage.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    return db, AgentsStore(db)


def test_ingest_extracts_event_uuid_and_parent_uuid(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        ts = datetime(2026, 3, 4, tzinfo=timezone.utc)
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(
                        role="user",
                        content_text="start",
                        timestamp=ts,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"uuid":"u-root","type":"user"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="reply",
                        timestamp=ts,
                        source_path="/tmp/session.jsonl",
                        source_offset=1,
                        raw_json='{"uuid":"u-child","parentUuid":"u-root","type":"assistant"}',
                    ),
                ],
            )
        )

        rows = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == result.session_id)
            .order_by(AgentEvent.id.asc())
            .all()
        )
        assert [row.event_uuid for row in rows] == ["u-root", "u-child"]
        assert [row.parent_event_uuid for row in rows] == [None, "u-root"]
    finally:
        db.close()


def test_event_uuid_dedup_works_without_source_path(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        ts = datetime(2026, 3, 4, tzinfo=timezone.utc)
        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="lineage event",
                        timestamp=ts,
                        source_path=None,
                        source_offset=None,
                        raw_json='{"uuid":"u-lineage","type":"assistant"}',
                    )
                ],
            )
        )
        assert first.events_inserted == 1

        second = store.ingest_session(
            SessionIngest(
                id=first.session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="lineage event duplicate",
                        timestamp=ts,
                        source_path=None,
                        source_offset=None,
                        raw_json='{"uuid":"u-lineage","type":"assistant"}',
                    )
                ],
            )
        )
        assert second.events_inserted == 0
        assert second.events_skipped == 1
    finally:
        db.close()


def test_event_uuid_allows_branch_prefix_copy(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        ts = datetime(2026, 3, 4, tzinfo=timezone.utc)
        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(
                        role="user",
                        content_text="start",
                        timestamp=ts,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"uuid":"u-root","type":"user"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old middle",
                        timestamp=ts,
                        source_path="/tmp/session.jsonl",
                        source_offset=10,
                        raw_json='{"uuid":"u-old","parentUuid":"u-root","type":"assistant"}',
                    ),
                ],
            )
        )

        second = store.ingest_session(
            SessionIngest(
                id=first.session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=ts,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="rewritten middle",
                        timestamp=ts,
                        source_path="/tmp/session.jsonl",
                        source_offset=10,
                        raw_json='{"uuid":"u-new","parentUuid":"u-root","type":"assistant"}',
                    )
                ],
            )
        )
        assert second.events_inserted == 1

        branches = (
            db.query(AgentSessionBranch)
            .filter(AgentSessionBranch.session_id == first.session_id)
            .order_by(AgentSessionBranch.id.asc())
            .all()
        )
        assert len(branches) == 2
        assert branches[0].is_head == 0
        assert branches[1].is_head == 1

        all_rows = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == first.session_id)
            .order_by(AgentEvent.branch_id.asc(), AgentEvent.id.asc())
            .all()
        )
        assert any(row.branch_id == branches[1].id and row.event_uuid == "u-root" for row in all_rows)
        assert any(row.branch_id == branches[0].id and row.event_uuid == "u-root" for row in all_rows)
    finally:
        db.close()
