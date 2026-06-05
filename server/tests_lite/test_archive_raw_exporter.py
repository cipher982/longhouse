from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ArchiveExportCheckpoint
from zerg.models.agents import ArchiveExportQuarantine
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.legacy_archive_exporter import export_legacy_raw_archive_batch
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json


def test_legacy_exporter_exports_source_lines_and_checkpoints(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_source_line(db, session_id=session_id, raw_json='{"type":"user","message":"hello"}', source_offset=0)
        _add_source_line(db, session_id=session_id, raw_json='{"type":"assistant","message":"done"}', source_offset=100)
        db.commit()

        result = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
        )
        db.commit()

        chunk = db.query(ArchiveChunk).filter(ArchiveChunk.session_id == session_id).one()
        checkpoint = db.query(ArchiveExportCheckpoint).filter(ArchiveExportCheckpoint.session_id == session_id).one()
        records = archive_store.read_chunk(chunk.relative_path)

        assert result.selected_rows == 2
        assert result.rows_exported == 2
        assert result.chunks_written == 1
        assert checkpoint.source_table == "source_lines"
        assert checkpoint.last_rowid == 2
        assert checkpoint.status == "current"
        assert chunk.stream == "source_lines"
        assert [record.raw_bytes.decode("utf-8") for record in records] == [
            '{"type":"user","message":"hello"}',
            '{"type":"assistant","message":"done"}',
        ]
        assert records[0].legacy_ref == {
            "table": "source_lines",
            "rowid": 1,
            "raw_json_codec": CODEC_ZSTD,
            "branch_id": 1,
            "revision": 1,
            "line_hash": "hash-0",
        }
        assert db.query(AgentSourceLine).count() == 2


def test_legacy_exporter_resumes_with_keyset_pagination(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_source_line(db, session_id=session_id, raw_json='{"line":1}', source_offset=0)
        _add_source_line(db, session_id=session_id, raw_json='{"line":2}', source_offset=100)
        db.commit()

        first = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
            batch_size=1,
        )
        db.commit()
        second = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
            batch_size=1,
        )
        db.commit()
        third = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
            batch_size=1,
        )

        checkpoint = db.query(ArchiveExportCheckpoint).filter(ArchiveExportCheckpoint.session_id == session_id).one()
        assert first.selected_rows == 1
        assert second.selected_rows == 1
        assert third.selected_rows == 0
        assert checkpoint.last_rowid == 2
        assert db.query(ArchiveChunk).filter(ArchiveChunk.session_id == session_id).count() == 2


def test_legacy_exporter_pauses_below_disk_floor(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_source_line(db, session_id=session_id, raw_json='{"line":1}', source_offset=0)
        db.commit()

        result = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
            disk_floor_bytes=10,
            free_bytes_getter=lambda _path: 5,
        )
        db.commit()

        checkpoint = db.query(ArchiveExportCheckpoint).filter(ArchiveExportCheckpoint.session_id == session_id).one()
        assert result.paused is True
        assert result.pause_reason == "low disk"
        assert result.selected_rows == 0
        assert checkpoint.status == "paused"
        assert checkpoint.last_rowid == 0
        assert db.query(ArchiveChunk).count() == 0


def test_legacy_exporter_quarantines_corrupt_raw_row_and_advances(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        db.add(
            AgentSourceLine(
                session_id=session_id,
                thread_id=None,
                source_path="/tmp/session.jsonl",
                source_offset=0,
                branch_id=1,
                revision=1,
                is_branch_copy=0,
                raw_json="",
                raw_json_z=b"not zstd",
                raw_json_codec=CODEC_ZSTD,
                line_hash="bad-hash",
            )
        )
        db.commit()

        first = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
        )
        db.commit()
        second = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
        )

        checkpoint = db.query(ArchiveExportCheckpoint).filter(ArchiveExportCheckpoint.session_id == session_id).one()
        quarantine = db.query(ArchiveExportQuarantine).filter(ArchiveExportQuarantine.session_id == session_id).one()
        assert first.selected_rows == 1
        assert first.rows_quarantined == 1
        assert first.rows_exported == 0
        assert second.selected_rows == 0
        assert checkpoint.status == "quarantined"
        assert checkpoint.last_rowid == 1
        assert quarantine.source_table == "source_lines"
        assert quarantine.rowid == 1
        assert db.query(ArchiveChunk).count() == 0


def test_legacy_exporter_exports_raw_events(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="codex")
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="parsed",
                timestamp=_ts(),
                source_path="/tmp/events.jsonl",
                source_offset=25,
                event_hash="event-hash",
                raw_json=None,
                raw_json_z=compress_raw_json('{"event":"raw"}'),
                raw_json_codec=CODEC_ZSTD,
                event_uuid="event-1",
            )
        )
        db.commit()

        result = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="events",
            session_id=session_id,
        )
        db.commit()

        chunk = db.query(ArchiveChunk).filter(ArchiveChunk.session_id == session_id).one()
        records = archive_store.read_chunk(chunk.relative_path)
        assert result.rows_exported == 1
        assert chunk.stream == "events"
        assert records[0].provider == "codex"
        assert records[0].source_path == "/tmp/events.jsonl"
        assert records[0].source_offset == 25
        assert records[0].raw_bytes == b'{"event":"raw"}'
        assert records[0].legacy_ref == {
            "table": "events",
            "rowid": 1,
            "raw_json_codec": CODEC_ZSTD,
            "event_hash": "event-hash",
            "event_uuid": "event-1",
        }


def test_legacy_exporter_dry_run_writes_nothing(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_source_line(db, session_id=session_id, raw_json='{"line":1}', source_offset=0)
        db.commit()

        result = export_legacy_raw_archive_batch(
            db,
            archive_store=archive_store,
            tenant_id="tenant-a",
            source_table="source_lines",
            session_id=session_id,
            dry_run=True,
        )

        assert result.dry_run is True
        assert result.rows_exported == 1
        assert result.chunks_written == 0
        assert result.checkpoints_written == 0
        assert db.query(ArchiveChunk).count() == 0
        assert db.query(ArchiveExportCheckpoint).count() == 0


def _session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy-exporter.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _add_session(db, *, session_id, provider: str) -> AgentSession:
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/tmp/longhouse",
        started_at=_ts(),
        last_activity_at=_ts(),
    )
    db.add(session)
    db.flush()
    return session


def _add_source_line(db, *, session_id, raw_json: str, source_offset: int) -> AgentSourceLine:
    row = AgentSourceLine(
        session_id=session_id,
        thread_id=None,
        source_path="/tmp/session.jsonl",
        source_offset=source_offset,
        branch_id=1,
        revision=1,
        is_branch_copy=0,
        raw_json="",
        raw_json_z=compress_raw_json(raw_json),
        raw_json_codec=CODEC_ZSTD,
        line_hash=f"hash-{source_offset}",
    )
    db.add(row)
    db.flush()
    return row
