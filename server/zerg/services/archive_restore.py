"""Restore drills for rebuilding clean stores from sealed archive chunks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import ArchiveChunk
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import ArchiveStore


@dataclass(frozen=True)
class ArchiveRestoreResult:
    chunks_seen: int
    chunks_inserted: int
    sessions_created: int
    records_read: int


@dataclass(frozen=True)
class EventStreamReplayResult:
    chunks_seen: int
    records_read: int
    live_archive_primary_records: int
    legacy_export_records: int
    unknown_ref_records: int
    raw_bytes: tuple[bytes, ...]


def restore_archive_manifests_and_sessions(
    db: Session,
    *,
    archive_store: ArchiveStore,
    tenant_id: str | None = None,
    session_id: UUID | str | None = None,
) -> ArchiveRestoreResult:
    """Restore manifest rows and minimal session rows from sealed archive files."""

    chunk_refs = archive_store.list_chunks(
        tenant_id=tenant_id,
        session_id=str(session_id) if session_id is not None else None,
    )
    chunk_paths = [chunk.relative_path for chunk in chunk_refs]
    existing_paths = set()
    if chunk_paths:
        existing_paths = {
            path for (path,) in db.query(ArchiveChunk.relative_path).filter(ArchiveChunk.relative_path.in_(chunk_paths)).all()
        }
    new_chunks = [chunk for chunk in chunk_refs if chunk.relative_path not in existing_paths]
    if new_chunks:
        insert_archive_chunk_manifests(db, new_chunks)

    records_by_session: dict[UUID, list[ArchiveRecord]] = {}
    records_read = 0
    for chunk in chunk_refs:
        records = list(archive_store.read_chunk(chunk.relative_path))
        records_read += len(records)
        if not records:
            continue
        records_by_session.setdefault(UUID(str(chunk.session_id)), []).extend(records)

    existing_session_ids = {
        session_id for (session_id,) in db.query(AgentSession.id).filter(AgentSession.id.in_(records_by_session.keys())).all()
    }
    sessions_created = 0
    for restored_session_id, records in records_by_session.items():
        if restored_session_id in existing_session_ids:
            continue
        db.add(_session_from_archive_records(restored_session_id, records))
        sessions_created += 1

    db.flush()
    return ArchiveRestoreResult(
        chunks_seen=len(chunk_refs),
        chunks_inserted=len(new_chunks),
        sessions_created=sessions_created,
        records_read=records_read,
    )


def replay_event_stream_records(
    db: Session,
    *,
    archive_store: ArchiveStore,
    session_id: UUID | str | None = None,
) -> EventStreamReplayResult:
    """Read raw event archive records and classify live vs legacy ref shapes."""

    query = db.query(ArchiveChunk).filter(ArchiveChunk.stream == "events").filter(ArchiveChunk.state == "sealed")
    if session_id is not None:
        query = query.filter(ArchiveChunk.session_id == UUID(str(session_id)))
    chunks = query.order_by(ArchiveChunk.id.asc()).all()

    records_read = 0
    live_records = 0
    legacy_records = 0
    unknown_records = 0
    raw_bytes: list[bytes] = []
    for chunk in chunks:
        for record in archive_store.read_chunk(chunk.relative_path):
            records_read += 1
            raw_bytes.append(record.raw_bytes)
            ref_kind = _event_ref_kind(record.legacy_ref)
            if ref_kind == "live_archive_primary":
                live_records += 1
            elif ref_kind == "legacy_export":
                legacy_records += 1
            else:
                unknown_records += 1

    return EventStreamReplayResult(
        chunks_seen=len(chunks),
        records_read=records_read,
        live_archive_primary_records=live_records,
        legacy_export_records=legacy_records,
        unknown_ref_records=unknown_records,
        raw_bytes=tuple(raw_bytes),
    )


def _session_from_archive_records(session_id: UUID, records: Iterable[ArchiveRecord]) -> AgentSession:
    record_list = list(records)
    timestamps = [timestamp for record in record_list if (timestamp := _timestamp_from_record(record)) is not None]
    started_at = min(timestamps, default=None)
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    provider = next((record.provider for record in record_list if record.provider), "unknown")
    return AgentSession(
        id=session_id,
        provider=provider or "unknown",
        environment="restored",
        project=None,
        device_id=None,
        cwd=None,
        started_at=started_at,
        last_activity_at=started_at,
    )


def _timestamp_from_record(record: ArchiveRecord) -> datetime | None:
    if record.received_at:
        parsed = _parse_datetime(record.received_at)
        if parsed is not None:
            return parsed
    try:
        payload = json.loads(record.raw_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("timestamp", "created_at", "started_at"):
        parsed = _parse_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_ref_kind(ref: object) -> str:
    if not isinstance(ref, dict):
        return "unknown"
    if isinstance(ref.get("event_key"), str):
        return "live_archive_primary"
    if ref.get("table") == "events" or "rowid" in ref or "event_hash" in ref or "event_uuid" in ref:
        return "legacy_export"
    return "unknown"
