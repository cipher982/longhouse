"""Archive-backed export parity (closeout PR2).

Proves export_session_jsonl reconstructs byte-identical transcript output from
sealed source_lines archive chunks once the monolith raw payload is absent —
the prerequisite for dropping raw bytes from the monolith without breaking
transcript export / session resume.
"""

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.archive_shadow import write_ingest_shadow_archive
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.raw_json_compression import CODEC_PLAIN


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'transcript.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ingest_with_source_lines(store: AgentsStore, session_id):
    lines = [
        ('{"type":"user","timestamp":"2026-01-01T00:00:01Z"}', 0),
        ('{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}', 50),
        ('{"type":"system","subtype":"compact_boundary"}', 100),
    ]
    data = SessionIngest(
        id=session_id,
        provider="claude",
        environment="production",
        started_at="2026-01-01T00:00:00Z",
        events=[
            EventIngest(
                role="user",
                content_text="hi",
                timestamp="2026-01-01T00:00:01Z",
                source_path="/tmp/s.jsonl",
                source_offset=0,
                raw_json=lines[0][0],
            ),
        ],
        source_lines=[
            SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=off, raw_json=raw) for raw, off in lines
        ],
    )
    return store.ingest_session(data)


def test_export_reconstructs_from_archive_when_raw_dropped(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "default")

    factory = _factory(tmp_path)
    session_id = uuid4()

    # 1. Ingest, then seed the archive with the same source lines (shadow path).
    with factory() as db:
        store = AgentsStore(db)
        result = _ingest_with_source_lines(store, session_id)
        write_ingest_shadow_archive(
            db,
            data=SessionIngest(
                id=session_id,
                provider="claude",
                environment="production",
                started_at="2026-01-01T00:00:00Z",
                source_lines=[
                    SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=0, raw_json='{"type":"user","timestamp":"2026-01-01T00:00:01Z"}'),
                    SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=50, raw_json='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z"}'),
                    SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=100, raw_json='{"type":"system","subtype":"compact_boundary"}'),
                ],
            ),
            result=IngestResult(
                session_id=session_id,
                events_inserted=0,
                events_skipped=0,
                session_created=False,
                source_lines_inserted=0,
            ),
            archive_store=FilesystemArchiveStore(archive_root),
        )
        db.commit()

    # 2. Baseline export from monolith raw bytes.
    with factory() as db:
        baseline = AgentsStore(db).export_session_jsonl(session_id)
        assert baseline is not None
        baseline_bytes = baseline[0]
        assert b'"compact_boundary"' in baseline_bytes

    # 3. Strip raw bytes from the monolith source_lines (simulate reclaim).
    with factory() as db:
        db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).update(
            {
                AgentSourceLine.raw_json: "",
                AgentSourceLine.raw_json_z: None,
                AgentSourceLine.raw_json_codec: CODEC_PLAIN,
            }
        )
        db.commit()

    # 4. Export must now reconstruct byte-identical output from the archive.
    with factory() as db:
        rebuilt = AgentsStore(db).export_session_jsonl(session_id)
        assert rebuilt is not None
        assert rebuilt[0] == baseline_bytes
