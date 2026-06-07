"""Events-stream archive coverage verifier (Phase E step 2).

Proves the verifier flags durable event rows whose raw bytes are not present in
the events-stream archive — the gate before events.raw_json_z can be dropped.
"""

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.archive_events_verifier import verify_session_event_archive_coverage
from zerg.services.archive_shadow import write_ingest_shadow_archive
from zerg.services.archive_store import FilesystemArchiveStore


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ev_verify.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _session(session_id, events):
    return SessionIngest(
        id=session_id, provider="claude", environment="production",
        started_at="2026-01-01T00:00:00Z",
        events=[
            EventIngest(role=r, content_text=c, timestamp=t, source_path="/tmp/s.jsonl", source_offset=o, raw_json=raw)
            for (r, c, t, o, raw) in events
        ],
    )


def test_verifier_flags_uncovered_event_rows(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "default")

    factory = _factory(tmp_path)
    session_id = uuid4()
    events = [
        ("user", "hello", "2026-01-01T00:00:01Z", 0, '{"type":"user","content":"hello"}'),
        ("assistant", "hi", "2026-01-01T00:00:02Z", 50, '{"type":"assistant","content":"hi"}'),
    ]
    with factory() as db:
        store = AgentsStore(db)
        store.ingest_session(_session(session_id, events))
        # Archive ONLY the first event's bytes.
        write_ingest_shadow_archive(
            db,
            data=_session(session_id, events[:1]),
            result=IngestResult(session_id=session_id, events_inserted=0, events_skipped=0, session_created=False, source_lines_inserted=0),
            archive_store=FilesystemArchiveStore(archive_root),
        )
        db.commit()

    with factory() as db:
        report = verify_session_event_archive_coverage(db, session_id)
        assert report.rows_with_raw == 2
        assert report.rows_covered == 1
        assert not report.fully_covered
        assert len(report.missing) == 1


def test_verifier_full_coverage(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "default")

    factory = _factory(tmp_path)
    session_id = uuid4()
    events = [
        ("user", "hello", "2026-01-01T00:00:01Z", 0, '{"type":"user","content":"hello"}'),
        ("assistant", "hi", "2026-01-01T00:00:02Z", 50, '{"type":"assistant","content":"hi"}'),
    ]
    with factory() as db:
        store = AgentsStore(db)
        store.ingest_session(_session(session_id, events))
        write_ingest_shadow_archive(
            db,
            data=_session(session_id, events),
            result=IngestResult(session_id=session_id, events_inserted=0, events_skipped=0, session_created=False, source_lines_inserted=0),
            archive_store=FilesystemArchiveStore(archive_root),
        )
        db.commit()

    with factory() as db:
        report = verify_session_event_archive_coverage(db, session_id)
        assert report.rows_with_raw == 2
        assert report.fully_covered
        assert report.missing == []
