"""Test helper: register archive chunk manifests the archive readers look up."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import ArchiveChunk
from zerg.services.archive_store import ArchiveChunkRef


def insert_archive_chunk_manifests(db: Session, chunks: Iterable[ArchiveChunkRef]) -> None:
    for chunk in chunks:
        stmt = (
            sqlite_insert(ArchiveChunk)
            .values(
                tenant_id=chunk.tenant_id,
                session_id=chunk.session_id,
                stream=chunk.stream,
                relative_path=chunk.relative_path,
                first_source_seq=chunk.first_source_seq,
                last_source_seq=chunk.last_source_seq,
                record_count=chunk.record_count,
                uncompressed_bytes=chunk.uncompressed_bytes,
                compressed_bytes=chunk.compressed_bytes,
                payload_sha256=chunk.payload_sha256,
                file_sha256=chunk.file_sha256,
                state="sealed",
            )
            .on_conflict_do_nothing(index_elements=["relative_path"])
        )
        db.execute(stmt)
    db.flush()
