from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _make_store(tmp_path):
    db_path = tmp_path / "rewind_branch_projection.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    return db, AgentsStore(db)


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def test_rewind_branch_head_vs_forensic_projection(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/rewind-session.jsonl"
        line0 = '{"type":"user","text":"start"}'
        line10_old = '{"type":"assistant","text":"old middle"}'
        line20_old = '{"type":"assistant","text":"old tail"}'
        line10_new = '{"type":"assistant","text":"rewritten middle"}'
        line30_new = '{"type":"assistant","text":"new tail"}'

        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="start",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=0,
                        raw_json=line0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old middle",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_old,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old tail",
                        timestamp=_ts(3),
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

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="rewritten middle",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_new,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="new tail",
                        timestamp=_ts(5),
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

        head_events = store.get_session_events(session_id, branch_mode="head", limit=100)
        assert [event.content_text for event in head_events if event.content_text] == [
            "start",
            "rewritten middle",
            "new tail",
        ]

        forensic_events = store.get_session_events(session_id, branch_mode="all", limit=100)
        assert any(event.content_text == "old tail" for event in forensic_events)
        assert len(forensic_events) > len(head_events)
        assert store.count_session_events(session_id, branch_mode="all") > store.count_session_events(
            session_id, branch_mode="head"
        )

        head_export, _ = store.export_session_jsonl(session_id, branch_mode="head")
        assert head_export.decode("utf-8") == "\n".join([line0, line10_new, line30_new]) + "\n"

        all_export, _ = store.export_session_jsonl(session_id, branch_mode="all")
        assert all_export.decode("utf-8").splitlines() == [
            line0,
            line10_old,
            line20_old,
            line10_new,
            line30_new,
        ]
    finally:
        db.close()


def test_lineage_divergence_forks_branch_without_offset_rewrite(tmp_path):
    """Rewind branch can be inferred from parentUuid divergence even on append-only source offsets."""
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/rewind-lineage.jsonl"
        line0 = '{"uuid":"u-root","type":"user","text":"start"}'
        line10_old = '{"uuid":"u-old-1","parentUuid":"u-root","type":"assistant","text":"old middle"}'
        line20_old = '{"uuid":"u-old-2","parentUuid":"u-old-1","type":"assistant","text":"old tail"}'
        line30_new = '{"uuid":"u-new-1","parentUuid":"u-root","type":"assistant","text":"new tail"}'

        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="start",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=0,
                        raw_json=line0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old middle",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_old,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old tail",
                        timestamp=_ts(3),
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

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="new tail",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=30,
                        raw_json=line30_new,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=30, raw_json=line30_new),
                ],
            )
        )

        head_events = store.get_session_events(session_id, branch_mode="head", limit=100)
        assert [event.content_text for event in head_events if event.content_text] == [
            "start",
            "new tail",
        ]

        forensic_events = store.get_session_events(session_id, branch_mode="all", limit=100)
        assert any(event.content_text == "old middle" for event in forensic_events)
        assert any(event.content_text == "old tail" for event in forensic_events)
    finally:
        db.close()


def test_leaf_uuid_realigns_head_branch(tmp_path):
    """Summary leafUuid should move active head to the matching branch."""
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/rewind-leaf.jsonl"
        line0 = '{"uuid":"u-root","type":"user","text":"start"}'
        line10_old = '{"uuid":"u-old-1","parentUuid":"u-root","type":"assistant","text":"old middle"}'
        line20_old = '{"uuid":"u-old-2","parentUuid":"u-old-1","type":"assistant","text":"old tail"}'
        line30_new = '{"uuid":"u-new-1","parentUuid":"u-root","type":"assistant","text":"new tail"}'
        summary_line = '{"type":"summary","summary":"Compacted","leafUuid":"u-old-2"}'

        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="start",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=0,
                        raw_json=line0,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old middle",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=10,
                        raw_json=line10_old,
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="old tail",
                        timestamp=_ts(3),
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

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="new tail",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=30,
                        raw_json=line30_new,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=30, raw_json=line30_new),
                ],
            )
        )

        old_branch_id = (
            db.query(AgentEvent.branch_id)
            .filter(AgentEvent.session_id == session_id)
            .filter(AgentEvent.event_uuid == "u-old-2")
            .order_by(AgentEvent.id.asc())
            .limit(1)
            .scalar()
        )
        assert old_branch_id is not None
        old_branch_id = int(old_branch_id)

        head_before_summary = store.get_head_branch_id(session_id)
        assert head_before_summary is not None
        assert int(head_before_summary) != old_branch_id

        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="system",
                        content_text="Compacted",
                        timestamp=_ts(5),
                        source_path=source_path,
                        source_offset=40,
                        raw_json=summary_line,
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=40, raw_json=summary_line),
                ],
            )
        )

        head_after_summary = store.get_head_branch_id(session_id)
        assert head_after_summary is not None
        assert int(head_after_summary) == old_branch_id
    finally:
        db.close()
