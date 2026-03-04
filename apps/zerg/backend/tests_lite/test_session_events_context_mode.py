from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def _make_store(tmp_path):
    db_path = tmp_path / "context_mode.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    return db, AgentsStore(db)


def _ts(second: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def test_active_context_mode_uses_latest_compaction_boundary(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/claude-session.jsonl"
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="My favorite color is yellow.",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=10,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:01Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="Noted.",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=20,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}',
                    ),
                    EventIngest(
                        role="system",
                        content_text="Conversation compacted [trigger=auto pre_tokens=155708]",
                        timestamp=_ts(3),
                        source_path=source_path,
                        source_offset=30,
                        raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-01-01T00:00:03Z"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="What is my favorite color?",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=40,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:04Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="You said yellow.",
                        timestamp=_ts(5),
                        source_path=source_path,
                        source_offset=50,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:05Z"}',
                    ),
                ],
                source_lines=[],
            )
        )

        forensic_events = store.get_session_events(result.session_id, context_mode="forensic", limit=100)
        assert len(forensic_events) == 5

        active_events = store.get_session_events(result.session_id, context_mode="active_context", limit=100)
        assert len(active_events) == 3
        assert active_events[0].role == "system"
        assert active_events[1].content_text == "What is my favorite color?"
        assert active_events[2].content_text == "You said yellow."
        assert store.count_session_events(result.session_id, context_mode="active_context") == 3
    finally:
        db.close()


def test_active_context_mode_falls_back_to_summary_marker(tmp_path):
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/claude-summary-only.jsonl"
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="system",
                        content_text="Session compacted to summary",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=0,
                        raw_json='{"type":"summary","summary":"Session compacted to summary","leafUuid":"leaf-1"}',
                    ),
                    EventIngest(
                        role="user",
                        content_text="Continue from compacted state.",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=10,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:02Z"}',
                    ),
                ],
                source_lines=[],
            )
        )

        active_events = store.get_session_events(result.session_id, context_mode="active_context", limit=100)
        assert len(active_events) == 2
        assert active_events[0].role == "system"
        assert active_events[1].role == "user"
    finally:
        db.close()


def test_active_context_mode_prefers_source_offset_on_same_path(tmp_path):
    """Late-arriving stale lines before boundary offset stay out of active context."""
    db, store = _make_store(tmp_path)
    try:
        source_path = "/tmp/rewind-like.jsonl"
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=_ts(0),
                events=[
                    EventIngest(
                        role="user",
                        content_text="yellow in old context",
                        timestamp=_ts(1),
                        source_path=source_path,
                        source_offset=100,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:01Z"}',
                    ),
                    EventIngest(
                        role="system",
                        content_text="Conversation compacted [trigger=auto]",
                        timestamp=_ts(2),
                        source_path=source_path,
                        source_offset=200,
                        raw_json='{"type":"system","subtype":"compact_boundary","timestamp":"2026-01-01T00:00:02Z"}',
                    ),
                    EventIngest(
                        role="assistant",
                        content_text="post-compact response",
                        timestamp=_ts(3),
                        source_path=source_path,
                        source_offset=300,
                        raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:03Z"}',
                    ),
                    # Rewind-like stale row: newer ingest id/timestamp, older source offset.
                    EventIngest(
                        role="user",
                        content_text="yellow stale rewind branch",
                        timestamp=_ts(4),
                        source_path=source_path,
                        source_offset=150,
                        raw_json='{"type":"user","timestamp":"2026-01-01T00:00:04Z"}',
                    ),
                ],
                source_lines=[],
            )
        )

        active_events = store.get_session_events(result.session_id, context_mode="active_context", limit=100)
        assert [event.content_text for event in active_events if event.content_text] == [
            "Conversation compacted [trigger=auto]",
            "post-compact response",
        ]
        assert store.count_session_events(result.session_id, context_mode="active_context", query="yellow") == 0
    finally:
        db.close()
