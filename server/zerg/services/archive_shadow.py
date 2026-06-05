"""Shadow archive writes for new ingest payloads.

This is Phase 4 plumbing: legacy ingest remains authoritative, while this
service can write the same raw source lines to the archive store behind an
explicit flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.config import Settings
from zerg.config import get_settings
from zerg.data_plane import create_archive_store
from zerg.models.agents import ArchiveChunk
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.archive_store import ArchiveChunkRef
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArchiveShadowResult:
    enabled: bool
    chunks_written: int = 0
    records_written: int = 0
    error: str | None = None


def write_ingest_shadow_archive(
    db: Session,
    *,
    data: SessionIngest,
    result: IngestResult,
    settings: Settings | None = None,
    archive_store: FilesystemArchiveStore | None = None,
) -> ArchiveShadowResult:
    settings = settings or get_settings()
    if not settings.archive_shadow_write_enabled:
        return ArchiveShadowResult(enabled=False)

    source_lines = source_lines_from_ingest(data)
    if not source_lines:
        return ArchiveShadowResult(enabled=True)

    try:
        store = archive_store or create_archive_store(settings)
        records = build_source_line_archive_records(
            data=data,
            result=result,
            source_lines=source_lines,
            tenant_id=settings.archive_shadow_tenant_id,
        )
        chunks = store.write_record_chunks(
            records,
            target_uncompressed_bytes=max(1, int(settings.archive_shadow_chunk_target_bytes)),
        )
    except Exception as exc:
        logger.warning("Shadow archive chunk write failed for session %s: %s", result.session_id, exc, exc_info=True)
        return ArchiveShadowResult(enabled=True, error=type(exc).__name__)

    try:
        insert_archive_chunk_manifests(db, chunks)
        return ArchiveShadowResult(
            enabled=True,
            chunks_written=len(chunks),
            records_written=len(source_lines),
        )
    except Exception as exc:
        db.rollback()
        logger.warning("Shadow archive manifest write failed for session %s: %s", result.session_id, exc, exc_info=True)
        return ArchiveShadowResult(enabled=True, error=type(exc).__name__)


def source_lines_from_ingest(data: SessionIngest) -> list[SourceLineIngest]:
    """Return exact source lines, falling back to event raw_json rows."""
    if data.source_lines:
        return list(data.source_lines)

    seen: set[tuple[str, int, str]] = set()
    lines: list[SourceLineIngest] = []
    for event in data.events:
        if not event.raw_json or not event.source_path or event.source_offset is None:
            continue
        key = (event.source_path, int(event.source_offset), event.raw_json)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            SourceLineIngest(
                source_path=event.source_path,
                source_offset=int(event.source_offset),
                raw_json=event.raw_json,
            )
        )
    return lines


def build_source_line_archive_records(
    *,
    data: SessionIngest,
    result: IngestResult,
    source_lines: Iterable[SourceLineIngest],
    tenant_id: str,
) -> list[ArchiveRecord]:
    sorted_lines = sorted(
        source_lines,
        key=lambda line: (line.source_path, int(line.source_offset), line.raw_json),
    )
    records: list[ArchiveRecord] = []
    for index, line in enumerate(sorted_lines, start=1):
        records.append(
            ArchiveRecord(
                tenant_id=tenant_id,
                session_id=str(result.session_id),
                stream="source_lines",
                source_seq=index,
                raw_bytes=line.raw_json.encode("utf-8"),
                legacy_ref={
                    "source": "agents_ingest",
                    "source_path": line.source_path,
                    "source_offset": int(line.source_offset),
                },
                provider=data.provider,
                source_path=line.source_path,
                source_offset=int(line.source_offset),
            )
        )
    return records


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
