from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from sqlalchemy import text

from zerg.data_plane import initialize_derived_database
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ProjectorCheckpoint
from zerg.services.archive_derived_projector import DERIVED_EVENTS_PARSER_REVISION
from zerg.services.archive_derived_projector import DERIVED_EVENTS_PROJECTOR_NAME
from zerg.services.archive_derived_projector import project_archive_chunks_to_derived_events
from zerg.services.archive_derived_projector import select_pending_archive_chunks
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore


def test_archive_derived_projector_writes_events_and_fts(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Find archive search"}}',
                ),
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=100,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_use","name":"Read","id":"tool-1","input":{"file":"x.py"}},{"type":"text","text":"Indexed."}]}}',
                ),
                _record(
                    session_id,
                    source_seq=3,
                    source_offset=200,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"tool-1","content":"file output"}]}}',
                ),
            ],
        )
        manifest_db.commit()

        result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        manifest_db.commit()
        derived_db.commit()

        rows = derived_db.execute(text("SELECT role, content_text, tool_name, tool_output_text FROM derived_events ORDER BY id")).fetchall()
        fts_rows = derived_db.execute(
            text("SELECT session_id FROM derived_events_fts WHERE derived_events_fts MATCH 'archive'")
        ).fetchall()
        checkpoint = manifest_db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()

        assert result.selected_chunks == 1
        assert result.chunks_projected == 1
        assert result.events_projected == 4
        assert [row[0] for row in rows] == ["user", "assistant", "assistant", "tool"]
        assert rows[1][2] == "Read"
        assert rows[3][3] == "file output"
        assert fts_rows == [(str(session_id),)]
        assert checkpoint.projector_name == DERIVED_EVENTS_PROJECTOR_NAME
        assert checkpoint.parser_revision == DERIVED_EVENTS_PARSER_REVISION
        assert checkpoint.status == "current"

        rerun = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        assert rerun.selected_chunks == 0
        assert derived_db.execute(text("SELECT COUNT(*) FROM derived_events")).scalar() == 4


def test_archive_derived_projector_supports_generic_events_and_parser_revisions(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=session_id, provider="codex")
        _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    provider="codex",
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"message","role":"user","content":"codex generic","timestamp":"2026-01-01T00:00:01Z"}',
                )
            ],
        )
        manifest_db.commit()

        first = project_archive_chunks_to_derived_events(
            manifest_db,
            derived_db,
            archive_store=archive_store,
            parser_revision="derived-a",
        )
        second = project_archive_chunks_to_derived_events(
            manifest_db,
            derived_db,
            archive_store=archive_store,
            parser_revision="derived-b",
        )
        manifest_db.commit()
        derived_db.commit()

        assert first.selected_chunks == 1
        assert second.selected_chunks == 1
        assert derived_db.execute(text("SELECT COUNT(*) FROM derived_events")).scalar() == 2
        assert manifest_db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).count() == 2


def test_archive_derived_projector_marks_unsupported_terminal(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=session_id, provider="codex")
        _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=session_id,
            records=[_record(session_id, provider="codex", source_seq=1, source_offset=0, raw='{"kind":"unknown"}')],
        )
        manifest_db.commit()

        result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        manifest_db.commit()
        derived_db.commit()

        checkpoint = manifest_db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()
        assert result.unsupported_chunks == 1
        assert result.events_projected == 0
        assert checkpoint.status == "unsupported"
        assert select_pending_archive_chunks(manifest_db) == []


def test_archive_derived_projector_skips_sidechain_records(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"root"}}',
                ),
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=10,
                    raw='{"type":"user","isSidechain":true,"timestamp":"2026-01-01T00:00:01Z","message":{"content":"child"}}',
                ),
            ],
        )
        manifest_db.commit()

        result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        manifest_db.commit()
        derived_db.commit()

        rows = derived_db.execute(text("SELECT content_text FROM derived_events ORDER BY id")).fetchall()
        assert result.events_projected == 1
        assert rows == [("root",)]


def test_archive_derived_projector_records_corruption_error_for_retry(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=session_id, provider="claude")
        chunk = _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}',
                )
            ],
        )
        archive_store.root.joinpath(chunk.relative_path).write_bytes(b"not zstd")
        manifest_db.commit()

        result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        manifest_db.commit()

        checkpoint = manifest_db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()
        assert result.chunks_failed == 1
        assert checkpoint.status == "error"
        assert checkpoint.error == "ArchiveCorruptionError"
        assert [row.id for row in select_pending_archive_chunks(manifest_db)] == [chunk.id]


def test_archive_derived_projector_mixed_batch_keeps_success_and_error_checkpoints(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    good_session_id = uuid4()
    bad_session_id = uuid4()

    with manifest() as manifest_db, derived() as derived_db:
        _add_session(manifest_db, session_id=good_session_id, provider="claude")
        _add_session(manifest_db, session_id=bad_session_id, provider="claude")
        _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=good_session_id,
            records=[
                _record(
                    good_session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"good"}}',
                )
            ],
        )
        bad_chunk = _add_archive_chunk(
            manifest_db,
            archive_store,
            session_id=bad_session_id,
            records=[
                _record(
                    bad_session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"bad"}}',
                )
            ],
        )
        archive_store.root.joinpath(bad_chunk.relative_path).write_bytes(b"not zstd")
        manifest_db.commit()

        result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store, limit=2)
        manifest_db.commit()
        derived_db.commit()

        statuses = {
            str(row.session_id): row.status
            for row in manifest_db.query(ProjectorCheckpoint).order_by(ProjectorCheckpoint.session_id).all()
        }
        assert result.selected_chunks == 2
        assert result.chunks_projected == 1
        assert result.chunks_failed == 1
        assert statuses[str(good_session_id)] == "current"
        assert statuses[str(bad_session_id)] == "error"
        assert derived_db.execute(text("SELECT content_text FROM derived_events")).fetchall() == [("good",)]


def _stores(tmp_path):
    manifest_engine = make_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(bind=manifest_engine)
    derived_engine = make_engine(f"sqlite:///{tmp_path / 'derived.db'}")
    initialize_derived_database(derived_engine)
    return make_sessionmaker(manifest_engine), make_sessionmaker(derived_engine)


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


def _record(
    session_id,
    *,
    source_seq: int,
    source_offset: int,
    raw: str,
    provider: str = "claude",
) -> ArchiveRecord:
    return ArchiveRecord(
        tenant_id="tenant-test",
        session_id=str(session_id),
        stream="source_lines",
        source_seq=source_seq,
        raw_bytes=raw.encode("utf-8"),
        legacy_ref={"source": "test"},
        provider=provider,
        source_path="/tmp/session.jsonl",
        source_offset=source_offset,
    )


def _add_archive_chunk(db, archive_store: FilesystemArchiveStore, *, session_id, records: list[ArchiveRecord]) -> ArchiveChunk:
    ref = archive_store.write_chunk(records)
    chunk = ArchiveChunk(
        tenant_id=ref.tenant_id,
        session_id=session_id,
        stream=ref.stream,
        relative_path=ref.relative_path,
        first_source_seq=ref.first_source_seq,
        last_source_seq=ref.last_source_seq,
        record_count=ref.record_count,
        uncompressed_bytes=ref.uncompressed_bytes,
        compressed_bytes=ref.compressed_bytes,
        payload_sha256=ref.payload_sha256,
        file_sha256=ref.file_sha256,
        state="sealed",
    )
    db.add(chunk)
    db.flush()
    return chunk
