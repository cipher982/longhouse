"""Project sealed archive chunks into rebuildable derived event/search tables."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Iterable

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ProjectorCheckpoint
from zerg.services.archive_store import ArchiveCorruptionError
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import ArchiveStore
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

DERIVED_EVENTS_PROJECTOR_NAME = "derived-events"
DERIVED_EVENTS_PARSER_REVISION = "archive-derived-events-v1"
_TERMINAL_CHECKPOINT_STATUSES = {"current", "unsupported"}


@dataclass(frozen=True)
class ArchiveDerivedProjectorResult:
    selected_chunks: int
    chunks_projected: int
    chunks_failed: int
    unsupported_chunks: int
    checkpoints_written: int
    records_read: int
    events_projected: int


@dataclass(frozen=True)
class DerivedArchiveEvent:
    event_key: str
    session_id: str
    parser_revision: str
    archive_chunk_id: int
    archive_record_ordinal: int
    role: str
    content_text: str | None
    tool_name: str | None
    tool_input_json: dict[str, Any] | None
    tool_output_text: str | None
    tool_call_id: str | None
    timestamp: datetime | None
    source_path: str | None
    source_offset: int | None
    event_hash: str
    raw_json: str


def project_archive_chunks_to_derived_events(
    manifest_db: Session,
    derived_db: Session,
    *,
    archive_store: ArchiveStore,
    parser_revision: str = DERIVED_EVENTS_PARSER_REVISION,
    limit: int = 100,
) -> ArchiveDerivedProjectorResult:
    """Project sealed chunks into derived.db, then checkpoint manifest progress.

    This function commits successful per-chunk derived writes before it flushes
    the manifest checkpoint. The caller must still commit ``manifest_db``; if
    that commit is lost, a rerun replaces the already-committed derived rows.
    """
    pending_chunks = select_pending_archive_chunks(manifest_db, parser_revision=parser_revision, limit=limit)
    if not pending_chunks:
        return ArchiveDerivedProjectorResult(
            selected_chunks=0,
            chunks_projected=0,
            chunks_failed=0,
            unsupported_chunks=0,
            checkpoints_written=0,
            records_read=0,
            events_projected=0,
        )

    chunks_projected = 0
    chunks_failed = 0
    unsupported_chunks = 0
    checkpoints_written = 0
    records_read = 0
    events_projected = 0
    for chunk in pending_chunks:
        try:
            verification = archive_store.verify_chunk(
                chunk.relative_path,
                expected_file_sha256=chunk.file_sha256,
                expected_payload_sha256=chunk.payload_sha256,
            )
            if not verification.valid:
                raise ArchiveCorruptionError("; ".join(verification.errors))
            events, unsupported_records = parse_archive_records_to_derived_events(
                verification.records,
                chunk=chunk,
                parser_revision=parser_revision,
            )
            replace_derived_events_for_chunk(
                derived_db,
                chunk_id=int(chunk.id),
                parser_revision=parser_revision,
                events=events,
            )
            derived_db.commit()
        except Exception as exc:
            derived_db.rollback()
            logger.warning("Derived archive projection failed for chunk %s: %s", chunk.id, exc, exc_info=True)
            _upsert_projector_checkpoint(
                manifest_db,
                chunk=chunk,
                parser_revision=parser_revision,
                status="error",
                error=type(exc).__name__,
            )
            checkpoints_written += 1
            chunks_failed += 1
            continue

        records_read += len(verification.records)
        events_projected += len(events)
        status = "current"
        if not events and unsupported_records:
            status = "unsupported"
            unsupported_chunks += 1
        else:
            chunks_projected += 1
        _upsert_projector_checkpoint(
            manifest_db,
            chunk=chunk,
            parser_revision=parser_revision,
            status=status,
            error=None,
        )
        checkpoints_written += 1

    manifest_db.flush()
    return ArchiveDerivedProjectorResult(
        selected_chunks=len(pending_chunks),
        chunks_projected=chunks_projected,
        chunks_failed=chunks_failed,
        unsupported_chunks=unsupported_chunks,
        checkpoints_written=checkpoints_written,
        records_read=records_read,
        events_projected=events_projected,
    )


def select_pending_archive_chunks(
    db: Session,
    *,
    parser_revision: str = DERIVED_EVENTS_PARSER_REVISION,
    limit: int = 100,
) -> list[ArchiveChunk]:
    if limit <= 0:
        return []
    terminal_chunk_ids = (
        select(ProjectorCheckpoint.chunk_id)
        .where(ProjectorCheckpoint.projector_name == DERIVED_EVENTS_PROJECTOR_NAME)
        .where(ProjectorCheckpoint.parser_revision == parser_revision)
        .where(ProjectorCheckpoint.status.in_(_TERMINAL_CHECKPOINT_STATUSES))
        .where(ProjectorCheckpoint.chunk_id == ArchiveChunk.id)
        .where(ProjectorCheckpoint.chunk_payload_sha256 == ArchiveChunk.payload_sha256)
    )
    return (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.stream == "source_lines")
        .filter(ArchiveChunk.state == "sealed")
        .filter(~ArchiveChunk.id.in_(terminal_chunk_ids))
        .order_by(ArchiveChunk.id.asc())
        .limit(limit)
        .all()
    )


def parse_archive_records_to_derived_events(
    records: Iterable[ArchiveRecord],
    *,
    chunk: ArchiveChunk,
    parser_revision: str,
) -> tuple[list[DerivedArchiveEvent], int]:
    events: list[DerivedArchiveEvent] = []
    unsupported_records = 0
    for ordinal, record in enumerate(records, start=1):
        parsed, unsupported = _parse_record_events(record, chunk=chunk, parser_revision=parser_revision, ordinal=ordinal)
        if unsupported:
            unsupported_records += 1
        events.extend(parsed)
    return events, unsupported_records


def replace_derived_events_for_chunk(
    db: Session,
    *,
    chunk_id: int,
    parser_revision: str,
    events: Iterable[DerivedArchiveEvent],
) -> int:
    """Idempotently replace one chunk/parser projection in derived.db.

    The manifest checkpoint lives in a separate database, so reruns must repair
    a crash after derived rows commit but before the manifest checkpoint does.
    """
    existing_row_ids = db.execute(
        text(
            """
            SELECT id FROM derived_events
            WHERE archive_chunk_id = :chunk_id
              AND parser_revision = :parser_revision
            """
        ),
        {"chunk_id": chunk_id, "parser_revision": parser_revision},
    ).fetchall()
    if existing_row_ids:
        db.execute(
            text(
                """
                DELETE FROM derived_events_fts
                WHERE rowid IN (
                    SELECT id FROM derived_events
                    WHERE archive_chunk_id = :chunk_id
                      AND parser_revision = :parser_revision
                )
                """
            ),
            {"chunk_id": chunk_id, "parser_revision": parser_revision},
        )
        db.execute(
            text(
                """
                DELETE FROM derived_events
                WHERE archive_chunk_id = :chunk_id
                  AND parser_revision = :parser_revision
                """
            ),
            {"chunk_id": chunk_id, "parser_revision": parser_revision},
        )
    return insert_derived_events(db, events)


def insert_derived_events(db: Session, events: Iterable[DerivedArchiveEvent]) -> int:
    inserted = 0
    for event in events:
        values = {
            "event_key": event.event_key,
            "session_id": event.session_id,
            "parser_revision": event.parser_revision,
            "archive_chunk_id": event.archive_chunk_id,
            "archive_record_ordinal": event.archive_record_ordinal,
            "role": event.role,
            "content_text": event.content_text,
            "tool_name": event.tool_name,
            "tool_input_json": json.dumps(event.tool_input_json, sort_keys=True) if event.tool_input_json is not None else None,
            "tool_output_text": event.tool_output_text,
            "tool_call_id": event.tool_call_id,
            "timestamp": event.timestamp,
            "source_path": event.source_path,
            "source_offset": event.source_offset,
            "event_hash": event.event_hash,
            "raw_json": event.raw_json,
        }
        result = db.execute(
            text(
                """
                INSERT OR IGNORE INTO derived_events (
                    event_key,
                    session_id,
                    parser_revision,
                    archive_chunk_id,
                    archive_record_ordinal,
                    role,
                    content_text,
                    tool_name,
                    tool_input_json,
                    tool_output_text,
                    tool_call_id,
                    timestamp,
                    source_path,
                    source_offset,
                    event_hash,
                    raw_json
                ) VALUES (
                    :event_key,
                    :session_id,
                    :parser_revision,
                    :archive_chunk_id,
                    :archive_record_ordinal,
                    :role,
                    :content_text,
                    :tool_name,
                    :tool_input_json,
                    :tool_output_text,
                    :tool_call_id,
                    :timestamp,
                    :source_path,
                    :source_offset,
                    :event_hash,
                    :raw_json
                )
                """
            ),
            values,
        )
        inserted += int(result.rowcount or 0)
        row_id = db.execute(text("SELECT id FROM derived_events WHERE event_key = :event_key"), {"event_key": event.event_key}).scalar()
        if row_id is None:
            continue
        db.execute(
            text(
                """
                INSERT OR REPLACE INTO derived_events_fts (
                    rowid,
                    content_text,
                    tool_output_text,
                    tool_name,
                    role,
                    session_id,
                    parser_revision
                ) VALUES (
                    :rowid,
                    :content_text,
                    :tool_output_text,
                    :tool_name,
                    :role,
                    :session_id,
                    :parser_revision
                )
                """
            ),
            {
                "rowid": int(row_id),
                "content_text": event.content_text,
                "tool_output_text": event.tool_output_text,
                "tool_name": event.tool_name,
                "role": event.role,
                "session_id": event.session_id,
                "parser_revision": event.parser_revision,
            },
        )
    db.flush()
    return inserted


def _upsert_projector_checkpoint(
    db: Session,
    *,
    chunk: ArchiveChunk,
    parser_revision: str,
    status: str,
    error: str | None,
) -> None:
    values = {
        "projector_name": DERIVED_EVENTS_PROJECTOR_NAME,
        "parser_revision": parser_revision,
        "session_id": chunk.session_id,
        "chunk_id": chunk.id,
        "chunk_payload_sha256": chunk.payload_sha256,
        "last_record_ordinal": int(chunk.record_count or 0),
        "status": status,
        "error": error,
    }
    stmt = sqlite_insert(ProjectorCheckpoint).values(**values)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["projector_name", "parser_revision", "session_id", "chunk_id"],
            set_={
                "chunk_payload_sha256": chunk.payload_sha256,
                "last_record_ordinal": int(chunk.record_count or 0),
                "status": status,
                "error": error,
                "updated_at": datetime.now(timezone.utc),
            },
        )
    )


def _parse_record_events(
    record: ArchiveRecord,
    *,
    chunk: ArchiveChunk,
    parser_revision: str,
    ordinal: int,
) -> tuple[list[DerivedArchiveEvent], bool]:
    try:
        raw_text = record.raw_bytes.decode("utf-8")
        obj = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], True
    if not isinstance(obj, dict):
        return [], True
    if _is_sidechain_or_meta_record(obj):
        return [], False

    generic = _parse_generic_event_object(obj, record=record)
    if generic is not None:
        return _build_events(
            generic, record=record, chunk=chunk, parser_revision=parser_revision, ordinal=ordinal, raw_text=raw_text
        ), False

    event_type = str(obj.get("type") or "")
    if event_type in {"user", "assistant", "summary", "file-history-snapshot", "system", "progress"}:
        parsed = _parse_claude_event_object(obj, record=record)
        return _build_events(parsed, record=record, chunk=chunk, parser_revision=parser_revision, ordinal=ordinal, raw_text=raw_text), False

    return [], True


def _build_events(
    parsed_events: list[dict[str, Any]],
    *,
    record: ArchiveRecord,
    chunk: ArchiveChunk,
    parser_revision: str,
    ordinal: int,
    raw_text: str,
) -> list[DerivedArchiveEvent]:
    events: list[DerivedArchiveEvent] = []
    for event_index, parsed in enumerate(parsed_events, start=1):
        role = str(parsed["role"])
        event_hash = _event_hash(parsed)
        event_key = _event_key(
            parser_revision=parser_revision,
            chunk_id=int(chunk.id),
            ordinal=ordinal,
            event_index=event_index,
            event_hash=event_hash,
        )
        events.append(
            DerivedArchiveEvent(
                event_key=event_key,
                session_id=str(record.session_id),
                parser_revision=parser_revision,
                archive_chunk_id=int(chunk.id),
                archive_record_ordinal=ordinal,
                role=role,
                content_text=_optional_str(parsed.get("content_text")),
                tool_name=_optional_str(parsed.get("tool_name")),
                tool_input_json=parsed.get("tool_input_json") if isinstance(parsed.get("tool_input_json"), dict) else None,
                tool_output_text=_optional_str(parsed.get("tool_output_text")),
                tool_call_id=_optional_str(parsed.get("tool_call_id")),
                timestamp=_parse_timestamp(_optional_str(parsed.get("timestamp"))),
                source_path=record.source_path,
                source_offset=record.source_offset,
                event_hash=event_hash,
                raw_json=raw_text,
            )
        )
    return events


def _parse_generic_event_object(obj: dict, *, record: ArchiveRecord) -> list[dict[str, Any]] | None:
    _ = record
    payload = obj.get("payload")
    payload_obj = payload if isinstance(payload, dict) else None
    role = _optional_str(obj.get("role")) or (None if payload_obj is None else _optional_str(payload_obj.get("role")))
    event_type = _optional_str(obj.get("type"))
    payload_type = None if payload_obj is None else _optional_str(payload_obj.get("type"))
    if role is None and event_type != "message" and payload_type != "message":
        return None
    if role not in {"user", "assistant", "tool", "system"}:
        return None
    content_text = (
        _optional_str(obj.get("content_text"))
        or _text_content(obj.get("content"))
        or (None if payload_obj is None else _optional_str(payload_obj.get("content_text")))
        or (None if payload_obj is None else _text_content(payload_obj.get("content")))
    )
    return [
        {
            "role": role,
            "content_text": content_text,
            "tool_name": _optional_str(obj.get("tool_name"))
            or (None if payload_obj is None else _optional_str(payload_obj.get("tool_name"))),
            "tool_input_json": obj.get("tool_input_json")
            if isinstance(obj.get("tool_input_json"), dict)
            else (None if payload_obj is None else payload_obj.get("tool_input_json")),
            "tool_output_text": _optional_str(obj.get("tool_output_text"))
            or (None if payload_obj is None else _optional_str(payload_obj.get("tool_output_text"))),
            "tool_call_id": _optional_str(obj.get("tool_call_id"))
            or (None if payload_obj is None else _optional_str(payload_obj.get("tool_call_id"))),
            "timestamp": _optional_str(obj.get("timestamp"))
            or (None if payload_obj is None else _optional_str(payload_obj.get("timestamp"))),
        }
    ]


def _parse_claude_event_object(obj: dict, *, record: ArchiveRecord) -> list[dict[str, Any]]:
    _ = record
    event_type = str(obj.get("type") or "")
    timestamp = _optional_str(obj.get("timestamp"))
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if event_type == "progress":
        return []
    if event_type == "user":
        if _contains_tool_result(content):
            return _claude_tool_results(content, timestamp=timestamp)
        text_value = _text_content(content)
        return [] if not _normalized_text(text_value) else [{"role": "user", "content_text": text_value, "timestamp": timestamp}]
    if event_type == "assistant":
        if not isinstance(content, list):
            return []
        events: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text_value = _optional_str(item.get("text"))
                if _normalized_text(text_value):
                    events.append({"role": "assistant", "content_text": text_value, "timestamp": timestamp})
            elif item_type == "tool_use":
                events.append(
                    {
                        "role": "assistant",
                        "tool_name": _optional_str(item.get("name")) or "tool",
                        "tool_input_json": item.get("input") if isinstance(item.get("input"), dict) else None,
                        "tool_call_id": _optional_str(item.get("id")),
                        "timestamp": timestamp,
                    }
                )
        return events
    return []


def _claude_tool_results(content: object, *, timestamp: str | None) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        output = _text_content(item.get("content"))
        if not output and item.get("is_error") is True:
            output = "[tool error]"
        if output:
            events.append(
                {
                    "role": "tool",
                    "tool_output_text": output,
                    "tool_call_id": _optional_str(item.get("tool_use_id")),
                    "timestamp": timestamp,
                }
            )
    return events


def _text_content(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text":
                    text_value = item.get("text")
                elif item_type == "tool_result":
                    text_value = item.get("content")
                else:
                    text_value = None
                if isinstance(text_value, str):
                    parts.append(text_value)
        return "\n".join(part for part in parts if part)
    return None


def _contains_tool_result(value: object) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) and item.get("type") == "tool_result" for item in value)


def _is_sidechain_or_meta_record(obj: dict) -> bool:
    return _truthy(obj.get("isSidechain")) or _truthy(obj.get("isMeta"))


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return normalize_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _event_hash(parsed: dict[str, Any]) -> str:
    payload = json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _event_key(
    *,
    parser_revision: str,
    chunk_id: int,
    ordinal: int,
    event_index: int,
    event_hash: str,
) -> str:
    raw = f"{parser_revision}:{chunk_id}:{ordinal}:{event_index}:{event_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
