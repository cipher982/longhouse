from datetime import datetime
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest


def _make_store(tmp_path):
    db_path = tmp_path / "source_lines.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def test_export_session_jsonl_uses_source_lines_archive(tmp_path):
    """Export should replay full source_lines, including metadata-only rows."""
    db = _make_store(tmp_path)
    try:
        store = AgentsStore(db)
        source_path = "/tmp/claude-session.jsonl"
        lines = [
            '{"type":"file-history-snapshot","snapshot":{"timestamp":"2026-03-03T00:00:00Z"}}',
            '{"type":"user","timestamp":"2026-03-03T00:00:01Z","message":{"role":"user","content":"hello"}}',
        ]

        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello",
                        timestamp=datetime(2026, 3, 3, 0, 0, 1, tzinfo=timezone.utc),
                        source_path=source_path,
                        source_offset=80,
                        raw_json=lines[1],
                    )
                ],
                source_lines=[
                    SourceLineIngest(source_path=source_path, source_offset=0, raw_json=lines[0]),
                    SourceLineIngest(source_path=source_path, source_offset=80, raw_json=lines[1]),
                ],
            )
        )

        exported = store.export_session_jsonl(result.session_id)
        assert exported is not None
        content_bytes, _session = exported
        assert content_bytes.decode("utf-8") == "\n".join(lines) + "\n"
    finally:
        db.close()


def test_export_session_jsonl_falls_back_to_event_raw_json_when_no_source_lines(tmp_path):
    """Backward compatibility: old shippers without source_lines still export."""
    db = _make_store(tmp_path)
    try:
        store = AgentsStore(db)
        source_path = "/tmp/codex-session.jsonl"
        line = '{"type":"response_item","timestamp":"2026-03-03T00:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}'

        result = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello",
                        timestamp=datetime(2026, 3, 3, 0, 0, 1, tzinfo=timezone.utc),
                        source_path=source_path,
                        source_offset=0,
                        raw_json=line,
                    )
                ],
                source_lines=[],
            )
        )

        exported = store.export_session_jsonl(result.session_id)
        assert exported is not None
        content_bytes, _session = exported
        assert content_bytes.decode("utf-8") == line + "\n"
    finally:
        db.close()
