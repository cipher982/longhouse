"""Shadow archive writes for new ingest payloads.

This is Phase 4 plumbing: legacy ingest remains authoritative, while this
service can write the same raw source lines to the archive store behind an
explicit flag.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

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
_SOURCE_SEQ_HASH_BITS = 20
_SOURCE_SEQ_HASH_MASK = (1 << _SOURCE_SEQ_HASH_BITS) - 1
_SOURCE_SEQ_MAX_OFFSET = ((1 << 63) - 1) >> _SOURCE_SEQ_HASH_BITS


@dataclass(frozen=True)
class ArchiveShadowResult:
    enabled: bool
    chunks_written: int = 0
    records_written: int = 0
    error: str | None = None


@dataclass(frozen=True)
class PreparedArchiveShadow:
    enabled: bool
    chunks: tuple[ArchiveChunkRef, ...] = ()
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
    prepared = prepare_ingest_shadow_archive(
        data=data,
        result=result,
        settings=settings,
        archive_store=archive_store,
        manifest_db=db,
    )
    if not prepared.enabled:
        return ArchiveShadowResult(enabled=False)
    if prepared.error:
        return ArchiveShadowResult(enabled=True, error=prepared.error)
    if not prepared.chunks:
        return ArchiveShadowResult(enabled=True, records_written=prepared.records_written)

    try:
        insert_archive_chunk_manifests(db, prepared.chunks)
        return ArchiveShadowResult(
            enabled=True,
            chunks_written=len(prepared.chunks),
            records_written=prepared.records_written,
        )
    except Exception as exc:
        db.rollback()
        logger.warning("Shadow archive manifest write failed for session %s: %s", result.session_id, exc, exc_info=True)
        return ArchiveShadowResult(enabled=True, error=type(exc).__name__)


def prepare_ingest_shadow_archive(
    *,
    data: SessionIngest,
    result: IngestResult,
    settings: Settings | None = None,
    archive_store: FilesystemArchiveStore | None = None,
    manifest_db: Session | None = None,
) -> PreparedArchiveShadow:
    settings = settings or get_settings()
    if not settings.archive_shadow_write_enabled:
        return PreparedArchiveShadow(enabled=False)

    source_lines = source_lines_from_ingest(data)
    if not source_lines:
        return PreparedArchiveShadow(enabled=True)

    try:
        store = archive_store or create_archive_store(settings)
        records = build_source_line_archive_records(
            data=data,
            result=result,
            source_lines=source_lines,
            tenant_id=settings.archive_shadow_tenant_id,
        )
        if manifest_db is not None:
            archived_keys = archived_source_line_keys(
                manifest_db,
                archive_store=store,
                session_id=result.session_id,
                stream="source_lines",
                first_source_seq=min(record.source_seq for record in records),
                last_source_seq=max(record.source_seq for record in records),
            )
            records = [record for record in records if _record_key(record) not in archived_keys]
        if not records:
            return PreparedArchiveShadow(enabled=True)
        chunks = store.write_record_chunks(
            records,
            target_uncompressed_bytes=max(1, int(settings.archive_shadow_chunk_target_bytes)),
        )
    except Exception as exc:
        logger.warning("Shadow archive chunk write failed for session %s: %s", result.session_id, exc, exc_info=True)
        return PreparedArchiveShadow(enabled=True, error=type(exc).__name__)

    return PreparedArchiveShadow(
        enabled=True,
        chunks=tuple(chunks),
        records_written=len(records),
    )


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
        _unique_source_lines(source_lines),
        key=lambda line: (_stable_source_seq(line), line.source_path, int(line.source_offset), line.raw_json),
    )
    records: list[ArchiveRecord] = []
    for line in sorted_lines:
        records.append(
            ArchiveRecord(
                tenant_id=tenant_id,
                session_id=str(result.session_id),
                stream="source_lines",
                source_seq=_stable_source_seq(line),
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


def archived_source_line_keys(
    db: Session,
    *,
    archive_store: FilesystemArchiveStore,
    session_id: UUID | str,
    stream: str,
    first_source_seq: int | None = None,
    last_source_seq: int | None = None,
) -> set[tuple[str, int, str]]:
    query = (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.session_id == UUID(str(session_id)))
        .filter(ArchiveChunk.stream == stream)
        .filter(ArchiveChunk.state == "sealed")
    )
    if first_source_seq is not None:
        query = query.filter(ArchiveChunk.last_source_seq >= first_source_seq)
    if last_source_seq is not None:
        query = query.filter(ArchiveChunk.first_source_seq <= last_source_seq)

    chunks = query.order_by(ArchiveChunk.first_source_seq).all()
    keys: set[tuple[str, int, str]] = set()
    for chunk in chunks:
        try:
            for record in archive_store.read_chunk(chunk.relative_path):
                key = _record_key(record)
                if key is not None:
                    keys.add(key)
        except Exception as exc:
            logger.warning(
                "Skipping unreadable archive chunk while filtering shadow ingest overlap: %s: %s",
                chunk.relative_path,
                exc,
                exc_info=True,
            )
    return keys


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


def _unique_source_lines(source_lines: Iterable[SourceLineIngest]) -> list[SourceLineIngest]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[SourceLineIngest] = []
    for line in source_lines:
        key = (line.source_path, int(line.source_offset), line.raw_json)
        if key in seen:
            continue
        seen.add(key)
        unique.append(line)
    return unique


def _stable_source_seq(line: SourceLineIngest) -> int:
    tie_hash = int.from_bytes(
        hashlib.blake2b(f"{line.source_path}\0{line.raw_json}".encode("utf-8"), digest_size=8).digest(),
        "big",
    )
    offset = min(max(0, int(line.source_offset)), _SOURCE_SEQ_MAX_OFFSET)
    return (offset << _SOURCE_SEQ_HASH_BITS) | (tie_hash & _SOURCE_SEQ_HASH_MASK)


def _record_key(record: ArchiveRecord) -> tuple[str, int, str] | None:
    if record.source_path is None or record.source_offset is None:
        return None
    return (
        record.source_path,
        int(record.source_offset),
        hashlib.sha256(record.raw_bytes).hexdigest(),
    )
