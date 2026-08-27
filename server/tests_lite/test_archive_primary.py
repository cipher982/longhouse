"""Archive-primary ingest and chunk coverage tests."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import ArchiveChunk
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.archive_primary import build_source_line_archive_records
from zerg.services.archive_primary import source_lines_from_ingest
from zerg.services.archive_primary import write_ingest_archive
from zerg.services.archive_store import FilesystemArchiveStore


def test_archive_writes_source_lines_and_manifest(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, tenant_id="tenant-a", target_bytes=128)
    data = _session_ingest(
        source_lines=[
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=15, raw_json='{"type":"assistant"}'),
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=0, raw_json='{"type":"user"}'),
        ]
    )
    result = _ingest_result()

    with SessionLocal() as db:
        archive_result = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).order_by(ArchiveChunk.id).all()

    assert archive_result.records_written == 2
    assert archive_result.chunks_written >= 1
    assert len(rows) == archive_result.chunks_written
    assert all(row.tenant_id == "tenant-a" for row in rows)
    assert all(str(row.session_id) == str(result.session_id) for row in rows)

    records = []
    for row in rows:
        records.extend(archive_store.read_chunk(row.relative_path))
    assert [record.raw_bytes for record in records] == [b'{"type":"user"}', b'{"type":"assistant"}']
    assert all(record.tenant_id == "tenant-a" for record in records)
    assert all(record.session_id == str(result.session_id) for record in records)


def test_archive_manifest_insert_is_idempotent(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path)
    data = _session_ingest()
    result = _ingest_result()

    with SessionLocal() as db:
        first = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        second = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        assert first.error is None
        assert second.error is None
        assert second.records_written == 0
        assert second.chunks_written == 0
        assert db.query(ArchiveChunk).count() == first.chunks_written


def test_archive_skips_source_lines_already_present_in_sealed_chunks(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, target_bytes=4096)
    result = _ingest_result()

    with SessionLocal() as db:
        first = write_ingest_archive(
            db,
            data=_session_ingest(
                source_lines=[
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"type":"message","role":"user"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=15,
                        raw_json='{"type":"message","role":"assistant"}',
                    ),
                ]
            ),
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        second = write_ingest_archive(
            db,
            data=_session_ingest(
                source_lines=[
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"type":"message","role":"user"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=15,
                        raw_json='{"type":"message","role":"assistant"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=39,
                        raw_json='{"type":"message","role":"tool"}',
                    ),
                ]
            ),
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).order_by(ArchiveChunk.first_source_seq).all()

    assert first.error is None
    assert first.records_written == 2
    assert second.error is None
    assert second.records_written == 1
    assert len(rows) == first.chunks_written + second.chunks_written

    records = []
    for row in rows:
        records.extend(archive_store.read_chunk(row.relative_path))

    assert [(record.source_offset, record.raw_bytes) for record in records] == [
        (0, b'{"type":"message","role":"user"}'),
        (15, b'{"type":"message","role":"assistant"}'),
        (39, b'{"type":"message","role":"tool"}'),
    ]


def test_archive_source_sequences_do_not_collide_for_many_same_offset_records(tmp_path):
    result = _ingest_result()
    source_lines = [
        SourceLineIngest(
            source_path=f"/tmp/session-{index}.jsonl",
            source_offset=0,
            raw_json=f'{{"type":"message","index":{index}}}',
        )
        for index in range(2048)
    ]

    records = build_source_line_archive_records(
        data=_session_ingest(source_lines=source_lines),
        result=result,
        source_lines=source_lines,
        tenant_id="tenant-test",
    )
    source_seqs = [record.source_seq for record in records]

    assert len(source_seqs) == len(set(source_seqs))
    assert all(0 <= source_seq < (1 << 63) for source_seq in source_seqs)

    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    chunk = archive_store.write_chunk(records)

    assert chunk.record_count == len(source_lines)


def test_archive_falls_back_to_event_raw_json(tmp_path):
    data = _session_ingest(
        source_lines=[],
        events=[
            EventIngest(
                role="user",
                content_text="hello",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_path="/tmp/session.jsonl",
                source_offset=42,
                raw_json='{"type":"message","role":"user"}',
            )
        ],
    )

    lines = source_lines_from_ingest(data)

    assert len(lines) == 1
    assert lines[0].source_path == "/tmp/session.jsonl"
    assert lines[0].source_offset == 42
    assert lines[0].raw_json == '{"type":"message","role":"user"}'


def test_archive_writes_event_stream_for_raw_events_without_source_path(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, target_bytes=4096)
    data = _session_ingest(
        source_lines=[],
        events=[
            EventIngest(
                role="system",
                content_text="server synthetic",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_path=None,
                source_offset=None,
                raw_json='{"type":"server","role":"system"}',
            )
        ],
    )
    result = _ingest_result()

    with SessionLocal() as db:
        archive_result = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).all()

    assert archive_result.error is None
    assert archive_result.records_written == 1
    assert len(rows) == 1
    assert rows[0].stream == "events"

    records = archive_store.read_chunk(rows[0].relative_path)
    assert [record.raw_bytes for record in records] == [b'{"type":"server","role":"system"}']
    assert records[0].source_path is None
    assert records[0].source_offset is None


def _session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-primary.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _archive_settings(tmp_path, *, tenant_id: str = "tenant-test", target_bytes: int = 4096):
    return SimpleNamespace(
        archive_primary_tenant_id=tenant_id,
        archive_primary_chunk_target_bytes=target_bytes,
        archive_root=str(tmp_path / "archive"),
    )


def _session_ingest(*, source_lines=None, events=None) -> SessionIngest:
    return SessionIngest(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/tmp",
        git_repo=None,
        git_branch=None,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_lines=source_lines
        if source_lines is not None
        else [
            SourceLineIngest(
                source_path="/tmp/session.jsonl",
                source_offset=0,
                raw_json='{"type":"message","role":"user"}',
            )
        ],
        events=events or [],
    )


def _ingest_result() -> IngestResult:
    return IngestResult(
        session_id=uuid4(),
        events_inserted=0,
        events_skipped=0,
        session_created=True,
        source_lines_inserted=1,
    )
