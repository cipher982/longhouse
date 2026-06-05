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
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.archive_store import ArchiveChunkRef
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore

logger = logging.getLogger(__name__)
_SOURCE_SEQ_MAX = (1 << 63) - 1


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
    force_enabled: bool = False,
) -> PreparedArchiveShadow:
    settings = settings or get_settings()
    if not force_enabled and not settings.archive_shadow_write_enabled:
        return PreparedArchiveShadow(enabled=False)

    source_lines = source_lines_from_ingest(data)
    raw_events = raw_events_from_ingest(data)
    if not source_lines and not raw_events:
        return PreparedArchiveShadow(enabled=True)

    try:
        store = archive_store or create_archive_store(settings)
        chunks: list[ArchiveChunkRef] = []
        records_written = 0
        source_line_records = build_source_line_archive_records(
            data=data,
            result=result,
            source_lines=source_lines,
            tenant_id=settings.archive_shadow_tenant_id,
        )
        if source_line_records and manifest_db is not None:
            archived_keys = archived_source_line_keys(
                manifest_db,
                archive_store=store,
                session_id=result.session_id,
                stream="source_lines",
                first_source_seq=min(record.source_seq for record in source_line_records),
                last_source_seq=max(record.source_seq for record in source_line_records),
            )
            source_line_records = [record for record in source_line_records if _record_key(record) not in archived_keys]
        if source_line_records:
            chunks.extend(
                store.write_record_chunks(
                    source_line_records,
                    target_uncompressed_bytes=max(1, int(settings.archive_shadow_chunk_target_bytes)),
                )
            )
            records_written += len(source_line_records)

        event_records = build_event_archive_records(
            data=data,
            result=result,
            events=raw_events,
            tenant_id=settings.archive_shadow_tenant_id,
        )
        if event_records and manifest_db is not None:
            archived_keys = archived_event_keys(
                manifest_db,
                archive_store=store,
                session_id=result.session_id,
                first_source_seq=min(record.source_seq for record in event_records),
                last_source_seq=max(record.source_seq for record in event_records),
            )
            event_records = [record for record in event_records if _event_record_key(record) not in archived_keys]
        if event_records:
            chunks.extend(
                store.write_record_chunks(
                    event_records,
                    target_uncompressed_bytes=max(1, int(settings.archive_shadow_chunk_target_bytes)),
                )
            )
            records_written += len(event_records)
    except Exception as exc:
        logger.warning("Shadow archive chunk write failed for session %s: %s", result.session_id, exc, exc_info=True)
        return PreparedArchiveShadow(enabled=True, error=type(exc).__name__)

    if not records_written:
        return PreparedArchiveShadow(enabled=True)
    return PreparedArchiveShadow(
        enabled=True,
        chunks=tuple(chunks),
        records_written=records_written,
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


def raw_events_from_ingest(data: SessionIngest) -> list[EventIngest]:
    """Return provider event rows that carry raw JSON payloads."""

    seen: set[str] = set()
    events: list[EventIngest] = []
    for event in data.events:
        if not event.raw_json:
            continue
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events


def build_source_line_archive_records(
    *,
    data: SessionIngest,
    result: IngestResult,
    source_lines: Iterable[SourceLineIngest],
    tenant_id: str,
) -> list[ArchiveRecord]:
    sequenced_lines = sorted(
        _assign_source_sequences(_unique_source_lines(source_lines)),
        key=lambda item: (item[0], item[1].source_path, int(item[1].source_offset), item[1].raw_json),
    )
    records: list[ArchiveRecord] = []
    for source_seq, line in sequenced_lines:
        records.append(
            ArchiveRecord(
                tenant_id=tenant_id,
                session_id=str(result.session_id),
                stream="source_lines",
                source_seq=source_seq,
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


def build_event_archive_records(
    *,
    data: SessionIngest,
    result: IngestResult,
    events: Iterable[EventIngest],
    tenant_id: str,
) -> list[ArchiveRecord]:
    sequenced_events = sorted(
        _assign_event_sequences(events),
        key=lambda item: item[0],
    )
    records: list[ArchiveRecord] = []
    for source_seq, event in sequenced_events:
        event_key = _event_key(event)
        records.append(
            ArchiveRecord(
                tenant_id=tenant_id,
                session_id=str(result.session_id),
                stream="events",
                source_seq=source_seq,
                raw_bytes=(event.raw_json or "").encode("utf-8"),
                legacy_ref={
                    "source": "agents_ingest",
                    "event_key": event_key,
                    "role": event.role,
                    "timestamp": event.timestamp.isoformat(),
                    "tool_call_id": event.tool_call_id,
                    "source_path": event.source_path,
                    "source_offset": event.source_offset,
                },
                provider=data.provider,
                source_path=event.source_path,
                source_offset=event.source_offset,
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


def archived_event_keys(
    db: Session,
    *,
    archive_store: FilesystemArchiveStore,
    session_id: UUID | str,
    first_source_seq: int | None = None,
    last_source_seq: int | None = None,
) -> set[str]:
    query = (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.session_id == UUID(str(session_id)))
        .filter(ArchiveChunk.stream == "events")
        .filter(ArchiveChunk.state == "sealed")
    )
    if first_source_seq is not None:
        query = query.filter(ArchiveChunk.last_source_seq >= first_source_seq)
    if last_source_seq is not None:
        query = query.filter(ArchiveChunk.first_source_seq <= last_source_seq)

    chunks = query.order_by(ArchiveChunk.first_source_seq).all()
    keys: set[str] = set()
    for chunk in chunks:
        try:
            for record in archive_store.read_chunk(chunk.relative_path):
                key = _event_record_key(record)
                if key is not None:
                    keys.add(key)
        except Exception as exc:
            logger.warning(
                "Skipping unreadable archive chunk while filtering event archive overlap: %s: %s",
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


def _assign_source_sequences(source_lines: Iterable[SourceLineIngest]) -> list[tuple[int, SourceLineIngest]]:
    used: dict[int, tuple[str, int, str]] = {}
    sequenced: list[tuple[int, SourceLineIngest]] = []
    for line in sorted(source_lines, key=lambda item: (item.source_path, int(item.source_offset), item.raw_json)):
        key = (line.source_path, int(line.source_offset), line.raw_json)
        salt = 0
        source_seq = _stable_source_seq(key, salt=salt)
        while source_seq in used and used[source_seq] != key:
            salt += 1
            source_seq = _stable_source_seq(key, salt=salt)
        used[source_seq] = key
        sequenced.append((source_seq, line))
    return sequenced


def _assign_event_sequences(events: Iterable[EventIngest]) -> list[tuple[int, EventIngest]]:
    used: dict[int, str] = {}
    sequenced: list[tuple[int, EventIngest]] = []
    for event in sorted(events, key=_event_key):
        key = _event_key(event)
        salt = 0
        source_seq = _stable_event_seq(key, salt=salt)
        while source_seq in used and used[source_seq] != key:
            salt += 1
            source_seq = _stable_event_seq(key, salt=salt)
        used[source_seq] = key
        sequenced.append((source_seq, event))
    return sequenced


def _stable_source_seq(key: tuple[str, int, str], *, salt: int) -> int:
    source_path, source_offset, raw_json = key
    digest = hashlib.blake2b(
        f"{source_path}\0{source_offset}\0{raw_json}\0{salt}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & _SOURCE_SEQ_MAX


def _stable_event_seq(key: str, *, salt: int) -> int:
    digest = hashlib.blake2b(
        f"{key}\0{salt}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big") & _SOURCE_SEQ_MAX


def _record_key(record: ArchiveRecord) -> tuple[str, int, str] | None:
    if record.source_path is None or record.source_offset is None:
        return None
    return (
        record.source_path,
        int(record.source_offset),
        hashlib.sha256(record.raw_bytes).hexdigest(),
    )


def _event_key(event: EventIngest) -> str:
    return _hash_parts(
        event.role,
        event.timestamp.isoformat(),
        event.source_path or "",
        str(event.source_offset) if event.source_offset is not None else "",
        event.tool_call_id or "",
        event.raw_json or "",
    )


def _event_record_key(record: ArchiveRecord) -> str | None:
    legacy_ref = record.legacy_ref
    if legacy_ref is None:
        return None
    value = legacy_ref.get("event_key")
    if not isinstance(value, str) or not value:
        return None
    return value


def _hash_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()
