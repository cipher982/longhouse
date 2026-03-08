from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _make_db(tmp_path):
    db_path = tmp_path / "lineage.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _local_ingest(*, session_id, started_at, event_timestamp, source_offset, content_text, raw_line, provider_session_id="prov-1"):
    return SessionIngest(
        id=session_id,
        provider="claude",
        environment="Cinder",
        project="zerg",
        device_id="shipper-cinder",
        cwd="/tmp/zerg",
        git_repo="git@github.com:cipher982/longhouse.git",
        git_branch="main",
        started_at=started_at,
        provider_session_id=provider_session_id,
        events=[
            EventIngest(
                role="user",
                content_text=content_text,
                timestamp=event_timestamp,
                source_path="/tmp/session.jsonl",
                source_offset=source_offset * 100,
            )
        ],
        source_lines=[
            SourceLineIngest(
                source_path="/tmp/task.txt",
                source_offset=source_offset,
                raw_json=raw_line,
            )
        ],
    )


def test_ingest_sets_lineage_defaults_for_root_session(tmp_path):
    Session = _make_db(tmp_path)
    root_id = uuid4()
    started_at = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)

    with Session() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=started_at,
                event_timestamp=started_at,
                source_offset=0,
                content_text="root",
                raw_line='{"line":"root"}',
            )
        )

        assert result.session_id == root_id
        session = store.get_session(root_id)
        assert session is not None
        assert session.thread_root_session_id == root_id
        assert session.continuation_kind == "local"
        assert session.origin_label == "Cinder"
        assert session.is_writable_head == 1
        assert store.get_thread_head(root_id).id == root_id


def test_create_continuation_session_marks_new_head(tmp_path):
    Session = _make_db(tmp_path)
    root_id = uuid4()
    started_at = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)

    with Session() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=started_at,
                event_timestamp=started_at,
                source_offset=0,
                content_text="root",
                raw_line='{"line":"root"}',
            )
        )

        child = store.create_continuation_session(
            root_id,
            continuation_kind="cloud",
            origin_label="Cloud",
            branched_from_event_id=store.get_latest_event_id(root_id),
        )
        db.commit()
        db.refresh(child)
        root = store.get_session(root_id)
        assert root is not None
        assert root.is_writable_head == 0
        assert child.thread_root_session_id == root_id
        assert child.continued_from_session_id == root_id
        assert child.is_writable_head == 1
        assert store.get_thread_head(root_id).id == child.id


def test_stale_local_ingest_creates_local_child_and_reuses_it(tmp_path):
    Session = _make_db(tmp_path)
    root_id = uuid4()
    root_started = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)
    cloud_started = datetime(2026, 3, 8, 20, 10, tzinfo=timezone.utc)
    local_continue_at = datetime(2026, 3, 8, 20, 20, tzinfo=timezone.utc)
    local_follow_up_at = datetime(2026, 3, 8, 20, 21, tzinfo=timezone.utc)

    with Session() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=root_started,
                event_timestamp=root_started,
                source_offset=0,
                content_text="root",
                raw_line='{"line":"root"}',
            )
        )
        cloud = store.create_continuation_session(
            root_id,
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
            device_id="zerg-commis-cloud",
            started_at=cloud_started,
            branched_from_event_id=store.get_latest_event_id(root_id),
        )
        db.commit()

        first_local = store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=root_started,
                event_timestamp=local_continue_at,
                source_offset=1,
                content_text="local after cloud",
                raw_line='{"line":"local-1"}',
            )
        )

        assert first_local.session_id not in {root_id, cloud.id}
        local_child = store.get_session(first_local.session_id)
        assert local_child is not None
        assert local_child.continued_from_session_id == root_id
        assert local_child.thread_root_session_id == root_id
        assert local_child.continuation_kind == "local"
        assert local_child.origin_label == "Cinder"
        assert store.get_thread_head(root_id).id == local_child.id

        second_local = store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=root_started,
                event_timestamp=local_follow_up_at,
                source_offset=2,
                content_text="local follow up",
                raw_line='{"line":"local-2"}',
            )
        )

        assert second_local.session_id == local_child.id
        sessions = db.query(AgentSession).filter(AgentSession.thread_root_session_id == root_id).all()
        assert len(sessions) == 3


def test_explicit_child_ingest_becomes_new_head(tmp_path):
    Session = _make_db(tmp_path)
    root_id = uuid4()
    child_id = uuid4()
    started_at = datetime(2026, 3, 8, 20, 0, tzinfo=timezone.utc)
    child_started = datetime(2026, 3, 8, 20, 5, tzinfo=timezone.utc)

    with Session() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _local_ingest(
                session_id=root_id,
                started_at=started_at,
                event_timestamp=started_at,
                source_offset=0,
                content_text="root",
                raw_line='{"line":"root"}',
            )
        )

        result = store.ingest_session(
            SessionIngest(
                id=child_id,
                provider="claude",
                environment="Cloud",
                project="zerg",
                device_id="zerg-commis-cloud",
                cwd="/tmp/zerg",
                git_repo="git@github.com:cipher982/longhouse.git",
                git_branch="main",
                started_at=child_started,
                provider_session_id="prov-1",
                thread_root_session_id=root_id,
                continued_from_session_id=root_id,
                continuation_kind="cloud",
                origin_label="Cloud",
                branched_from_event_id=store.get_latest_event_id(root_id),
                events=[
                    EventIngest(
                        role="user",
                        content_text="cloud continue",
                        timestamp=child_started,
                        source_path="/tmp/session.jsonl",
                        source_offset=200,
                    )
                ],
                source_lines=[
                    SourceLineIngest(
                        source_path="/tmp/task.txt",
                        source_offset=1,
                        raw_json='{"line":"cloud-1"}',
                    )
                ],
            )
        )

        assert result.session_id == child_id
        child = store.get_session(child_id)
        root = store.get_session(root_id)
        assert child is not None and root is not None
        assert child.is_writable_head == 1
        assert root.is_writable_head == 0
        assert store.get_thread_head(root_id).id == child_id
