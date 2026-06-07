"""Correctness-gate tests for raw reclaim (closeout PR3).

These prove the invariants that must hold before any source_lines raw payload
can be dropped from the monolith:
  1. the slim source_lines row is written even under archive-only ingest
     (write_legacy_raw=False), so the ordering/branch index survives reclaim;
  2. archive byte lookup is keyed by line_hash, so rewrites at the same offset
     reconstruct the correct revision (not whichever hash-seq sorts highest);
  3. the row-level verifier flags any source_lines row lacking a byte-identical
     archive record.
"""

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.archive_reclaim_verifier import verify_session_archive_coverage
from zerg.services.archive_shadow import write_ingest_shadow_archive
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.archive_transcript import load_session_source_line_bytes


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ingest(store, session_id, lines, *, write_legacy_raw=True):
    data = SessionIngest(
        id=session_id,
        provider="claude",
        environment="production",
        started_at="2026-01-01T00:00:00Z",
        source_lines=[SourceLineIngest(source_path=p, source_offset=o, raw_json=r) for p, o, r in lines],
    )
    return store.ingest_session(data, write_legacy_raw=write_legacy_raw)


def test_slim_row_written_when_raw_disabled(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid4()
    lines = [("/tmp/s.jsonl", 0, '{"type":"user"}'), ("/tmp/s.jsonl", 40, '{"type":"assistant"}')]
    with factory() as db:
        store = AgentsStore(db)
        _ingest(store, session_id, lines, write_legacy_raw=False)
        db.commit()

    with factory() as db:
        rows = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).all()
        # The slim index rows must exist...
        assert len(rows) == 2
        # ...with metadata populated but NO raw payload retained.
        for row in rows:
            assert row.line_hash
            assert row.source_path == "/tmp/s.jsonl"
            assert row.raw_json_z is None
            assert (row.raw_json or "") == ""


def test_archive_lookup_disambiguates_revisions_by_line_hash(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "default")

    factory = _factory(tmp_path)
    session_id = uuid4()
    v1 = '{"type":"assistant","rev":1}'
    v2 = '{"type":"assistant","rev":2}'

    # Ingest two revisions at the SAME offset, and archive both.
    with factory() as db:
        store = AgentsStore(db)
        _ingest(store, session_id, [("/tmp/s.jsonl", 10, v1)])
        _ingest(store, session_id, [("/tmp/s.jsonl", 10, v2)])
        for raw in (v1, v2):
            write_ingest_shadow_archive(
                db,
                data=SessionIngest(
                    id=session_id,
                    provider="claude",
                    environment="production",
                    started_at="2026-01-01T00:00:00Z",
                    source_lines=[SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=10, raw_json=raw)],
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

    with factory() as db:
        archived = load_session_source_line_bytes(db, session_id)
        import hashlib

        h1 = hashlib.sha256(v1.encode()).hexdigest()
        h2 = hashlib.sha256(v2.encode()).hexdigest()
        # Both revisions are retrievable by their distinct line_hash keys.
        assert archived[("/tmp/s.jsonl", 10, h1)] == v1
        assert archived[("/tmp/s.jsonl", 10, h2)] == v2


def test_verifier_flags_uncovered_rows(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(archive_root))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "default")

    factory = _factory(tmp_path)
    session_id = uuid4()
    lines = [("/tmp/s.jsonl", 0, '{"type":"user"}'), ("/tmp/s.jsonl", 40, '{"type":"assistant"}')]

    # Ingest two rows but archive only the FIRST one.
    with factory() as db:
        store = AgentsStore(db)
        _ingest(store, session_id, lines)
        write_ingest_shadow_archive(
            db,
            data=SessionIngest(
                id=session_id,
                provider="claude",
                environment="production",
                started_at="2026-01-01T00:00:00Z",
                source_lines=[SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=0, raw_json='{"type":"user"}')],
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

    with factory() as db:
        report = verify_session_archive_coverage(db, session_id)
        assert report.rows_checked == 2
        assert report.rows_covered == 1
        assert not report.fully_covered
        assert len(report.missing) == 1
        assert report.missing[0][1] == 40  # the un-archived offset
