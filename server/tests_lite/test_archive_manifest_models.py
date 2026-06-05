from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ArchiveExportCheckpoint
from zerg.models.agents import ProjectorCheckpoint


def test_archive_manifest_and_checkpoint_models_create_and_enforce_scope(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/archive_manifest.db")
    Base.metadata.create_all(bind=engine)
    session_factory = make_sessionmaker(engine)
    session_id = uuid4()

    with session_factory() as db:
        chunk = ArchiveChunk(
            tenant_id="tenant-a",
            session_id=session_id,
            stream="source_lines",
            relative_path="sessions/session-a/chunks/source_lines-000000000001-000000000001-abcd.jsonl.zst",
            first_source_seq=1,
            last_source_seq=1,
            record_count=1,
            uncompressed_bytes=128,
            compressed_bytes=64,
            payload_sha256="a" * 64,
            file_sha256="b" * 64,
        )
        db.add(chunk)
        db.flush()
        db.add(
            ArchiveExportCheckpoint(
                exporter_name="legacy-source-lines",
                tenant_id="tenant-a",
                source_table="source_lines",
                session_id=session_id,
                last_rowid=123,
                last_source_seq=1,
                status="running",
            )
        )
        db.add(
            ProjectorCheckpoint(
                projector_name="hot-card",
                parser_revision="parser-v1",
                session_id=session_id,
                chunk_id=chunk.id,
                chunk_payload_sha256=chunk.payload_sha256,
                last_record_ordinal=1,
                status="current",
            )
        )
        db.commit()

    with session_factory() as db:
        assert db.query(ArchiveChunk).count() == 1
        assert db.query(ArchiveExportCheckpoint).count() == 1
        assert db.query(ProjectorCheckpoint).count() == 1
        db.add(
            ArchiveChunk(
                tenant_id="tenant-a",
                session_id=session_id,
                stream="source_lines",
                relative_path="sessions/session-a/chunks/source_lines-000000000001-000000000001-abcd.jsonl.zst",
                first_source_seq=1,
                last_source_seq=1,
                record_count=1,
                uncompressed_bytes=128,
                compressed_bytes=64,
                payload_sha256="a" * 64,
                file_sha256="b" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
