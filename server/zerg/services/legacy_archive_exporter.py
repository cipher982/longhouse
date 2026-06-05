"""Read-only legacy raw table exporter into the archive store."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveExportCheckpoint
from zerg.models.agents import ArchiveExportQuarantine
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.raw_json_compression import decode_raw_json

LegacyRawTable = Literal["source_lines", "events"]

LEGACY_RAW_EXPORTER_NAME = "legacy-raw-v1"
LEGACY_RAW_TABLES: dict[LegacyRawTable, type[AgentSourceLine] | type[AgentEvent]] = {
    "source_lines": AgentSourceLine,
    "events": AgentEvent,
}


@dataclass(frozen=True)
class LegacyArchiveExportResult:
    source_table: LegacyRawTable
    session_id: UUID
    selected_rows: int
    rows_exported: int
    rows_quarantined: int
    chunks_written: int
    checkpoints_written: int
    paused: bool = False
    pause_reason: str | None = None
    dry_run: bool = False
    last_rowid: int = 0


def export_legacy_raw_archive_batch(
    db: Session,
    *,
    archive_store: FilesystemArchiveStore,
    tenant_id: str,
    source_table: LegacyRawTable,
    session_id: UUID | str,
    exporter_name: str = LEGACY_RAW_EXPORTER_NAME,
    batch_size: int = 500,
    chunk_target_uncompressed_bytes: int = 8 * 1024 * 1024,
    disk_floor_bytes: int = 0,
    dry_run: bool = False,
    free_bytes_getter: Callable[[Path], int] | None = None,
) -> LegacyArchiveExportResult:
    """Export one bounded per-session batch from a legacy raw table.

    The function only reads legacy raw rows. It writes archive files, archive
    manifests, export checkpoints, and quarantine rows in the new archive
    control tables.
    """
    normalized_session_id = UUID(str(session_id))
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if chunk_target_uncompressed_bytes <= 0:
        raise ValueError("chunk_target_uncompressed_bytes must be positive")
    if source_table not in LEGACY_RAW_TABLES:
        raise ValueError(f"unsupported legacy raw table: {source_table}")

    if disk_floor_bytes > 0:
        free_bytes = (free_bytes_getter or _filesystem_free_bytes)(archive_store.root)
        if free_bytes < disk_floor_bytes:
            if not dry_run:
                _upsert_export_checkpoint(
                    db,
                    exporter_name=exporter_name,
                    tenant_id=tenant_id,
                    source_table=source_table,
                    session_id=normalized_session_id,
                    last_rowid=_checkpoint_last_rowid(
                        db,
                        exporter_name=exporter_name,
                        tenant_id=tenant_id,
                        source_table=source_table,
                        session_id=normalized_session_id,
                    ),
                    last_source_seq=0,
                    status="paused",
                    error="low disk",
                )
                db.flush()
            return LegacyArchiveExportResult(
                source_table=source_table,
                session_id=normalized_session_id,
                selected_rows=0,
                rows_exported=0,
                rows_quarantined=0,
                chunks_written=0,
                checkpoints_written=0 if dry_run else 1,
                paused=True,
                pause_reason="low disk",
                dry_run=dry_run,
            )

    checkpoint = _get_export_checkpoint(
        db,
        exporter_name=exporter_name,
        tenant_id=tenant_id,
        source_table=source_table,
        session_id=normalized_session_id,
    )
    last_rowid = int(checkpoint.last_rowid) if checkpoint is not None else 0
    model = LEGACY_RAW_TABLES[source_table]
    rows = (
        db.query(model)
        .filter(model.session_id == normalized_session_id)
        .filter(model.id > last_rowid)
        .order_by(model.id.asc())
        .limit(batch_size)
        .all()
    )
    if not rows:
        return LegacyArchiveExportResult(
            source_table=source_table,
            session_id=normalized_session_id,
            selected_rows=0,
            rows_exported=0,
            rows_quarantined=0,
            chunks_written=0,
            checkpoints_written=0,
            dry_run=dry_run,
            last_rowid=last_rowid,
        )

    provider = _session_provider(db, normalized_session_id)
    records: list[ArchiveRecord] = []
    quarantined = 0
    max_processed_rowid = last_rowid
    for row in rows:
        rowid = int(row.id)
        max_processed_rowid = max(max_processed_rowid, rowid)
        try:
            raw_json = decode_raw_json(row)
            if raw_json is None:
                raise ValueError("raw_json is missing")
        except Exception as exc:
            quarantined += 1
            if not dry_run:
                _upsert_quarantine(
                    db,
                    exporter_name=exporter_name,
                    tenant_id=tenant_id,
                    source_table=source_table,
                    rowid=rowid,
                    session_id=normalized_session_id,
                    error=type(exc).__name__,
                )
            continue
        records.append(
            ArchiveRecord(
                tenant_id=tenant_id,
                session_id=str(normalized_session_id),
                stream=source_table,
                source_seq=rowid,
                raw_bytes=raw_json.encode("utf-8"),
                legacy_ref=_legacy_ref(source_table, row),
                provider=provider,
                source_path=getattr(row, "source_path", None),
                source_offset=getattr(row, "source_offset", None),
            )
        )

    chunks_written = 0
    if records and not dry_run:
        chunks = archive_store.write_record_chunks(
            records,
            target_uncompressed_bytes=chunk_target_uncompressed_bytes,
        )
        insert_archive_chunk_manifests(db, chunks)
        chunks_written = len(chunks)

    checkpoints_written = 0
    if not dry_run:
        status = "quarantined" if quarantined else "current"
        _upsert_export_checkpoint(
            db,
            exporter_name=exporter_name,
            tenant_id=tenant_id,
            source_table=source_table,
            session_id=normalized_session_id,
            last_rowid=max_processed_rowid,
            last_source_seq=max((record.source_seq for record in records), default=max_processed_rowid),
            status=status,
            error=None,
        )
        db.flush()
        checkpoints_written = 1

    return LegacyArchiveExportResult(
        source_table=source_table,
        session_id=normalized_session_id,
        selected_rows=len(rows),
        rows_exported=len(records),
        rows_quarantined=quarantined,
        chunks_written=chunks_written,
        checkpoints_written=checkpoints_written,
        dry_run=dry_run,
        last_rowid=max_processed_rowid,
    )


def _get_export_checkpoint(
    db: Session,
    *,
    exporter_name: str,
    tenant_id: str,
    source_table: str,
    session_id: UUID,
) -> ArchiveExportCheckpoint | None:
    return (
        db.query(ArchiveExportCheckpoint)
        .filter(ArchiveExportCheckpoint.exporter_name == exporter_name)
        .filter(ArchiveExportCheckpoint.tenant_id == tenant_id)
        .filter(ArchiveExportCheckpoint.source_table == source_table)
        .filter(ArchiveExportCheckpoint.session_id == session_id)
        .first()
    )


def _checkpoint_last_rowid(
    db: Session,
    *,
    exporter_name: str,
    tenant_id: str,
    source_table: str,
    session_id: UUID,
) -> int:
    checkpoint = _get_export_checkpoint(
        db,
        exporter_name=exporter_name,
        tenant_id=tenant_id,
        source_table=source_table,
        session_id=session_id,
    )
    return int(checkpoint.last_rowid) if checkpoint is not None else 0


def _upsert_export_checkpoint(
    db: Session,
    *,
    exporter_name: str,
    tenant_id: str,
    source_table: str,
    session_id: UUID,
    last_rowid: int,
    last_source_seq: int,
    status: str,
    error: str | None,
) -> None:
    stmt = sqlite_insert(ArchiveExportCheckpoint).values(
        exporter_name=exporter_name,
        tenant_id=tenant_id,
        source_table=source_table,
        session_id=session_id,
        last_rowid=last_rowid,
        last_source_seq=last_source_seq,
        status=status,
        error=error,
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["exporter_name", "tenant_id", "source_table", "session_id"],
            set_={
                "last_rowid": last_rowid,
                "last_source_seq": last_source_seq,
                "status": status,
                "error": error,
            },
        )
    )


def _upsert_quarantine(
    db: Session,
    *,
    exporter_name: str,
    tenant_id: str,
    source_table: str,
    rowid: int,
    session_id: UUID,
    error: str,
) -> None:
    stmt = sqlite_insert(ArchiveExportQuarantine).values(
        exporter_name=exporter_name,
        tenant_id=tenant_id,
        source_table=source_table,
        rowid=rowid,
        session_id=session_id,
        error=error,
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["exporter_name", "tenant_id", "source_table", "rowid"],
            set_={"session_id": session_id, "error": error},
        )
    )


def _legacy_ref(source_table: LegacyRawTable, row: AgentSourceLine | AgentEvent) -> dict[str, object]:
    ref: dict[str, object] = {
        "table": source_table,
        "rowid": int(row.id),
        "raw_json_codec": int(getattr(row, "raw_json_codec", 0) or 0),
    }
    branch_id = getattr(row, "branch_id", None)
    if branch_id is not None:
        ref["branch_id"] = int(branch_id)
    revision = getattr(row, "revision", None)
    if revision is not None:
        ref["revision"] = int(revision)
    line_hash = getattr(row, "line_hash", None)
    if line_hash:
        ref["line_hash"] = str(line_hash)
    event_hash = getattr(row, "event_hash", None)
    if event_hash:
        ref["event_hash"] = str(event_hash)
    event_uuid = getattr(row, "event_uuid", None)
    if event_uuid:
        ref["event_uuid"] = str(event_uuid)
    return ref


def _session_provider(db: Session, session_id: UUID) -> str | None:
    return db.query(AgentSession.provider).filter(AgentSession.id == session_id).scalar()


def _filesystem_free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return int(shutil.disk_usage(probe).free)
