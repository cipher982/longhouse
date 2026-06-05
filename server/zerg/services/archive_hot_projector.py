"""Project sealed raw archive chunks into hot session-card state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ProjectorCheckpoint
from zerg.models.agents import TimelineCard
from zerg.services.archive_store import ArchiveCorruptionError
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import ArchiveStore
from zerg.services.claude_channel_text import strip_claude_channel_wrapper
from zerg.utils.time import normalize_utc

logger = logging.getLogger(__name__)

HOT_CARD_PROJECTOR_NAME = "hot-card"
HOT_CARD_PARSER_REVISION = "archive-hot-card-v1"
_FIRST_USER_PREVIEW_CHARS = 300
_LAST_VISIBLE_PREVIEW_CHARS = 500
_TERMINAL_CHECKPOINT_STATUSES = {"current", "unsupported"}


@dataclass(frozen=True)
class ArchiveHotProjectorResult:
    selected_chunks: int
    sessions_projected: int
    sessions_partial: int
    checkpoints_written: int
    chunks_failed: int
    unsupported_chunks: int
    records_read: int
    events_projected: int


@dataclass(frozen=True)
class HotArchiveEvent:
    role: str
    timestamp: datetime | None
    content_text: str | None
    tool_name: str | None
    source_path: str | None
    source_offset: int | None
    ordinal: int


@dataclass(frozen=True)
class HotCardProjection:
    user_messages: int
    assistant_messages: int
    tool_calls: int
    first_user_message_preview: str | None
    last_visible_text_preview: str | None
    last_activity_at: datetime | None
    events_projected: int
    records_read: int
    unsupported_records: int
    parse_errors: int
    has_full_coverage: bool


def project_archive_chunks_to_hot_cards(
    db: Session,
    *,
    archive_store: ArchiveStore,
    parser_revision: str = HOT_CARD_PARSER_REVISION,
    limit: int = 100,
) -> ArchiveHotProjectorResult:
    """Project a bounded batch of sealed archive chunks into hot card rows."""
    pending_chunks = select_pending_archive_chunks(db, parser_revision=parser_revision, limit=limit)
    if not pending_chunks:
        return ArchiveHotProjectorResult(
            selected_chunks=0,
            sessions_projected=0,
            sessions_partial=0,
            checkpoints_written=0,
            chunks_failed=0,
            unsupported_chunks=0,
            records_read=0,
            events_projected=0,
        )

    grouped: dict[UUID, list[ArchiveChunk]] = {}
    for chunk in pending_chunks:
        grouped.setdefault(chunk.session_id, []).append(chunk)

    sessions_projected = 0
    sessions_partial = 0
    checkpoints_written = 0
    chunks_failed = 0
    unsupported_chunks = 0
    records_read = 0
    events_projected = 0

    for session_id, selected_for_session in grouped.items():
        session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if session is None:
            for chunk in selected_for_session:
                _upsert_projector_checkpoint(
                    db,
                    chunk=chunk,
                    parser_revision=parser_revision,
                    status="error",
                    error="missing session row",
                )
            checkpoints_written += len(selected_for_session)
            chunks_failed += len(selected_for_session)
            continue

        chunks_to_checkpoint = selected_for_session
        apply_full_projection = False
        apply_incremental_projection = False
        existing_card: TimelineCard | None = None
        try:
            selected_records = _read_records_for_chunks(archive_store, selected_for_session)
            projection = build_hot_card_projection(selected_records)
            if projection.has_full_coverage and projection.events_projected > 0:
                all_session_chunks = _load_session_archive_chunks(db, session_id=session_id)
                selected_chunk_ids = {int(chunk.id) for chunk in selected_for_session}
                all_chunk_ids = {int(chunk.id) for chunk in all_session_chunks}
                chunks_to_checkpoint = all_session_chunks
                if selected_chunk_ids != all_chunk_ids:
                    records = _read_records_for_chunks(archive_store, all_session_chunks)
                    projection = build_hot_card_projection(records)
                apply_full_projection = True
            elif projection.events_projected > 0:
                existing_card = _incremental_card_target(
                    db,
                    session_id=session_id,
                    parser_revision=parser_revision,
                    selected_chunks=selected_for_session,
                )
                if existing_card is not None:
                    apply_incremental_projection = True
        except Exception as exc:
            logger.warning("Hot-card archive projection failed for session %s: %s", session_id, exc, exc_info=True)
            for chunk in selected_for_session:
                _upsert_projector_checkpoint(
                    db,
                    chunk=chunk,
                    parser_revision=parser_revision,
                    status="error",
                    error=type(exc).__name__,
                )
            checkpoints_written += len(selected_for_session)
            chunks_failed += len(selected_for_session)
            continue

        records_read += projection.records_read
        events_projected += projection.events_projected
        unsupported_status = projection.events_projected == 0 and projection.unsupported_records > 0
        if unsupported_status:
            status = "unsupported"
            unsupported_chunks += len(selected_for_session)
        else:
            status = "current"

        if apply_full_projection and projection.events_projected > 0 and not unsupported_status:
            _apply_hot_projection(
                db,
                session=session,
                projection=projection,
                parser_revision=parser_revision,
            )
            sessions_projected += 1
        elif apply_incremental_projection and existing_card is not None and not unsupported_status:
            _apply_incremental_hot_projection(
                session=session,
                card=existing_card,
                projection=projection,
                parser_revision=parser_revision,
            )
            sessions_projected += 1
        else:
            sessions_partial += 1

        for chunk in chunks_to_checkpoint:
            _upsert_projector_checkpoint(
                db,
                chunk=chunk,
                parser_revision=parser_revision,
                status=status,
                error=None,
            )
        checkpoints_written += len(chunks_to_checkpoint)

    db.flush()
    return ArchiveHotProjectorResult(
        selected_chunks=len(pending_chunks),
        sessions_projected=sessions_projected,
        sessions_partial=sessions_partial,
        checkpoints_written=checkpoints_written,
        chunks_failed=chunks_failed,
        unsupported_chunks=unsupported_chunks,
        records_read=records_read,
        events_projected=events_projected,
    )


def select_pending_archive_chunks(
    db: Session,
    *,
    parser_revision: str = HOT_CARD_PARSER_REVISION,
    limit: int = 100,
) -> list[ArchiveChunk]:
    if limit <= 0:
        return []
    terminal_chunk_ids = (
        select(ProjectorCheckpoint.chunk_id)
        .where(ProjectorCheckpoint.projector_name == HOT_CARD_PROJECTOR_NAME)
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


def build_hot_card_projection(records: Iterable[ArchiveRecord]) -> HotCardProjection:
    record_list = list(records)
    events: list[HotArchiveEvent] = []
    unsupported_records = 0
    parse_errors = 0
    for ordinal, record in enumerate(record_list, start=1):
        try:
            parsed, unsupported = _parse_record_events(record, ordinal=ordinal)
        except Exception:
            parse_errors += 1
            logger.debug("Failed to parse archive record for hot projection", exc_info=True)
            continue
        if unsupported:
            unsupported_records += 1
        events.extend(parsed)

    user_events = [event for event in events if event.role == "user" and _normalized_text(event.content_text) != "warmup"]
    assistant_text_events = [
        event for event in events if event.role == "assistant" and not event.tool_name and _normalized_text(event.content_text)
    ]
    assistant_tool_events = [event for event in events if event.role == "assistant" and event.tool_name]
    first_user_event = _first_event([event for event in user_events if _normalized_text(event.content_text)])
    last_visible_event = _last_event(
        [
            event
            for event in events
            if event.role in {"user", "assistant"}
            and not (event.role == "assistant" and event.tool_name)
            and _normalized_text(event.content_text)
        ]
    )
    last_activity_event = _last_event(events)

    return HotCardProjection(
        user_messages=len(user_events),
        assistant_messages=len(assistant_text_events),
        tool_calls=len(assistant_tool_events),
        first_user_message_preview=_bounded_preview(
            strip_claude_channel_wrapper(first_user_event.content_text) if first_user_event else None,
            max_len=_FIRST_USER_PREVIEW_CHARS,
        ),
        last_visible_text_preview=_bounded_preview(
            strip_claude_channel_wrapper(last_visible_event.content_text) if last_visible_event else None,
            max_len=_LAST_VISIBLE_PREVIEW_CHARS,
        ),
        last_activity_at=last_activity_event.timestamp if last_activity_event else None,
        events_projected=len(events),
        records_read=len(record_list),
        unsupported_records=unsupported_records,
        parse_errors=parse_errors,
        has_full_coverage=_has_full_coverage(record_list),
    )


def _load_session_archive_chunks(db: Session, *, session_id: UUID) -> list[ArchiveChunk]:
    return (
        db.query(ArchiveChunk)
        .filter(ArchiveChunk.session_id == session_id)
        .filter(ArchiveChunk.stream == "source_lines")
        .filter(ArchiveChunk.state == "sealed")
        .order_by(
            ArchiveChunk.first_source_seq.asc(),
            ArchiveChunk.id.asc(),
        )
        .all()
    )


def _incremental_card_target(
    db: Session,
    *,
    session_id: UUID,
    parser_revision: str,
    selected_chunks: list[ArchiveChunk],
) -> TimelineCard | None:
    card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).first()
    if card is None or card.parser_revision != parser_revision or card.archive_state != "current":
        return None
    max_terminal_seq = _max_terminal_source_seq(db, session_id=session_id, parser_revision=parser_revision)
    if max_terminal_seq is None:
        return None
    if any(int(chunk.first_source_seq) <= max_terminal_seq for chunk in selected_chunks):
        return None
    return card


def _max_terminal_source_seq(db: Session, *, session_id: UUID, parser_revision: str) -> int | None:
    rows = (
        db.query(ArchiveChunk.last_source_seq)
        .join(ProjectorCheckpoint, ProjectorCheckpoint.chunk_id == ArchiveChunk.id)
        .filter(ProjectorCheckpoint.projector_name == HOT_CARD_PROJECTOR_NAME)
        .filter(ProjectorCheckpoint.parser_revision == parser_revision)
        .filter(ProjectorCheckpoint.session_id == session_id)
        .filter(ProjectorCheckpoint.status.in_(_TERMINAL_CHECKPOINT_STATUSES))
        .filter(ProjectorCheckpoint.chunk_payload_sha256 == ArchiveChunk.payload_sha256)
        .filter(ArchiveChunk.session_id == session_id)
        .filter(ArchiveChunk.stream == "source_lines")
        .filter(ArchiveChunk.state == "sealed")
        .all()
    )
    if not rows:
        return None
    return max(int(row[0]) for row in rows)


def _read_records_for_chunks(archive_store: ArchiveStore, chunks: Iterable[ArchiveChunk]) -> list[ArchiveRecord]:
    records: list[ArchiveRecord] = []
    for chunk in chunks:
        verification = archive_store.verify_chunk(
            chunk.relative_path,
            expected_file_sha256=chunk.file_sha256,
            expected_payload_sha256=chunk.payload_sha256,
        )
        if not verification.valid:
            raise ArchiveCorruptionError("; ".join(verification.errors))
        records.extend(verification.records)
    return records


def _apply_hot_projection(
    db: Session,
    *,
    session: AgentSession,
    projection: HotCardProjection,
    parser_revision: str,
) -> None:
    session.user_messages = projection.user_messages
    session.assistant_messages = projection.assistant_messages
    session.tool_calls = projection.tool_calls
    session.first_user_message_preview = projection.first_user_message_preview
    session.last_visible_text_preview = projection.last_visible_text_preview
    projected_activity = _naive_utc(projection.last_activity_at) if projection.last_activity_at is not None else None
    current_activity = _naive_utc(session.last_activity_at) if session.last_activity_at is not None else None
    if projected_activity is not None and (current_activity is None or projected_activity > current_activity):
        session.last_activity_at = projected_activity

    values = {
        "session_id": session.id,
        "provider": session.provider,
        "environment": session.environment,
        "project": session.project,
        "device_id": session.device_id,
        "cwd": session.cwd,
        "started_at": session.started_at,
        "last_activity_at": session.last_activity_at,
        "summary_title": session.summary_title,
        "first_user_message_preview": projection.first_user_message_preview,
        "last_visible_text_preview": projection.last_visible_text_preview,
        "user_messages": projection.user_messages,
        "assistant_messages": projection.assistant_messages,
        "tool_calls": projection.tool_calls,
        "transcript_revision": int(getattr(session, "transcript_revision", 0) or 0),
        "archive_state": "current",
        "archive_lag_records": 0,
        "derived_state": "pending" if int(getattr(session, "needs_projection", 0) or 0) else "current",
        "derived_revision": str(getattr(session, "summary_revision", 0) or 0),
        "parser_revision": parser_revision,
    }
    stmt = sqlite_insert(TimelineCard).values(**values)
    update_values = {key: value for key, value in values.items() if key != "session_id"}
    update_values["updated_at"] = datetime.now(timezone.utc)
    db.execute(stmt.on_conflict_do_update(index_elements=["session_id"], set_=update_values))


def _apply_incremental_hot_projection(
    *,
    session: AgentSession,
    card: TimelineCard,
    projection: HotCardProjection,
    parser_revision: str,
) -> None:
    session.user_messages = int(session.user_messages or 0) + projection.user_messages
    session.assistant_messages = int(session.assistant_messages or 0) + projection.assistant_messages
    session.tool_calls = int(session.tool_calls or 0) + projection.tool_calls
    if not session.first_user_message_preview and projection.first_user_message_preview:
        session.first_user_message_preview = projection.first_user_message_preview
    if projection.last_visible_text_preview:
        session.last_visible_text_preview = projection.last_visible_text_preview
    projected_activity = _naive_utc(projection.last_activity_at) if projection.last_activity_at is not None else None
    current_activity = _naive_utc(session.last_activity_at) if session.last_activity_at is not None else None
    if projected_activity is not None and (current_activity is None or projected_activity > current_activity):
        session.last_activity_at = projected_activity

    card.user_messages = int(card.user_messages or 0) + projection.user_messages
    card.assistant_messages = int(card.assistant_messages or 0) + projection.assistant_messages
    card.tool_calls = int(card.tool_calls or 0) + projection.tool_calls
    if not card.first_user_message_preview and projection.first_user_message_preview:
        card.first_user_message_preview = projection.first_user_message_preview
    if projection.last_visible_text_preview:
        card.last_visible_text_preview = projection.last_visible_text_preview
    card.last_activity_at = session.last_activity_at
    card.archive_state = "current"
    card.archive_lag_records = 0
    card.parser_revision = parser_revision
    card.updated_at = datetime.now(timezone.utc)


def _upsert_projector_checkpoint(
    db: Session,
    *,
    chunk: ArchiveChunk,
    parser_revision: str,
    status: str,
    error: str | None,
) -> None:
    values = {
        "projector_name": HOT_CARD_PROJECTOR_NAME,
        "parser_revision": parser_revision,
        "session_id": chunk.session_id,
        "chunk_id": chunk.id,
        "chunk_payload_sha256": chunk.payload_sha256,
        "last_record_ordinal": int(chunk.record_count or 0),
        "status": status,
        "error": error,
    }
    stmt = sqlite_insert(ProjectorCheckpoint).values(**values)
    update_values = {
        "chunk_payload_sha256": chunk.payload_sha256,
        "last_record_ordinal": int(chunk.record_count or 0),
        "status": status,
        "error": error,
        "updated_at": datetime.now(timezone.utc),
    }
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["projector_name", "parser_revision", "session_id", "chunk_id"],
            set_=update_values,
        )
    )


def _parse_record_events(record: ArchiveRecord, *, ordinal: int) -> tuple[list[HotArchiveEvent], bool]:
    try:
        raw_text = record.raw_bytes.decode("utf-8")
        obj = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], True
    if not isinstance(obj, dict):
        return [], True
    if _is_sidechain_or_meta_record(obj):
        return [], False

    generic = _parse_generic_event_object(obj, record=record, ordinal=ordinal)
    if generic is not None:
        return generic, False

    event_type = str(obj.get("type") or "")
    if event_type in {"user", "assistant", "summary", "file-history-snapshot", "system", "progress"}:
        return _parse_claude_event_object(obj, record=record, ordinal=ordinal), False

    return [], True


def _parse_generic_event_object(obj: dict, *, record: ArchiveRecord, ordinal: int) -> list[HotArchiveEvent] | None:
    payload = obj.get("payload")
    payload_obj = payload if isinstance(payload, dict) else None
    role = _optional_str(obj.get("role")) or (None if payload_obj is None else _optional_str(payload_obj.get("role")))
    event_type = _optional_str(obj.get("type"))
    payload_type = None if payload_obj is None else _optional_str(payload_obj.get("type"))
    if role is None and event_type != "message" and payload_type != "message":
        return None
    if role not in {"user", "assistant", "tool", "system"}:
        return None

    content = (
        _optional_str(obj.get("content_text"))
        or _text_content(obj.get("content"))
        or (None if payload_obj is None else _optional_str(payload_obj.get("content_text")))
        or (None if payload_obj is None else _text_content(payload_obj.get("content")))
    )
    tool_name = _optional_str(obj.get("tool_name")) or (None if payload_obj is None else _optional_str(payload_obj.get("tool_name")))
    timestamp = _parse_timestamp(
        _optional_str(obj.get("timestamp")) or (None if payload_obj is None else _optional_str(payload_obj.get("timestamp")))
    )
    return [_event(record, ordinal=ordinal, role=role, timestamp=timestamp, content_text=content, tool_name=tool_name)]


def _is_sidechain_or_meta_record(obj: dict) -> bool:
    return _truthy(obj.get("isSidechain")) or _truthy(obj.get("isMeta"))


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _parse_claude_event_object(obj: dict, *, record: ArchiveRecord, ordinal: int) -> list[HotArchiveEvent]:
    event_type = str(obj.get("type") or "")
    timestamp = _parse_timestamp(_optional_str(obj.get("timestamp")))
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    if event_type == "progress":
        return []
    if event_type == "user":
        if _contains_tool_result(content):
            return [
                _event(
                    record,
                    ordinal=ordinal,
                    role="tool",
                    timestamp=timestamp,
                    content_text=None,
                    tool_name=None,
                )
            ]
        text = _text_content(content)
        if not _normalized_text(text):
            return []
        return [_event(record, ordinal=ordinal, role="user", timestamp=timestamp, content_text=text, tool_name=None)]
    if event_type == "assistant":
        if not isinstance(content, list):
            return []
        events: list[HotArchiveEvent] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = _optional_str(item.get("text"))
                if _normalized_text(text):
                    events.append(
                        _event(
                            record,
                            ordinal=ordinal,
                            role="assistant",
                            timestamp=timestamp,
                            content_text=text,
                            tool_name=None,
                        )
                    )
            elif item_type == "tool_use":
                events.append(
                    _event(
                        record,
                        ordinal=ordinal,
                        role="assistant",
                        timestamp=timestamp,
                        content_text=None,
                        tool_name=_optional_str(item.get("name")) or "tool",
                    )
                )
        return events
    return []


def _event(
    record: ArchiveRecord,
    *,
    ordinal: int,
    role: str,
    timestamp: datetime | None,
    content_text: str | None,
    tool_name: str | None,
) -> HotArchiveEvent:
    return HotArchiveEvent(
        role=role,
        timestamp=timestamp,
        content_text=content_text,
        tool_name=tool_name,
        source_path=record.source_path,
        source_offset=record.source_offset,
        ordinal=ordinal,
    )


def _has_full_coverage(records: list[ArchiveRecord]) -> bool:
    offsets = [record.source_offset for record in records if record.source_offset is not None]
    return bool(offsets) and min(offsets) == 0


def _first_event(events: list[HotArchiveEvent]) -> HotArchiveEvent | None:
    return min(events, key=_event_order) if events else None


def _last_event(events: list[HotArchiveEvent]) -> HotArchiveEvent | None:
    return max(events, key=_event_order) if events else None


def _event_order(event: HotArchiveEvent) -> tuple[int, datetime, str, int, int]:
    timestamp = event.timestamp or datetime.min.replace(tzinfo=timezone.utc)
    timestamp = normalize_utc(timestamp) or datetime.min.replace(tzinfo=timezone.utc)
    return (
        0 if event.timestamp is not None else 1,
        timestamp,
        event.source_path or "",
        int(event.source_offset or 0),
        event.ordinal,
    )


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
                    text = item.get("text")
                elif item_type == "tool_result":
                    text = item.get("content")
                else:
                    text = None
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return None


def _contains_tool_result(value: object) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) and item.get("type") == "tool_result" for item in value)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return normalize_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _bounded_preview(value: str | None, *, max_len: int) -> str | None:
    normalized = _normalized_text(value)
    if not normalized:
        return None
    return normalized[:max_len]


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


def _naive_utc(value: datetime) -> datetime:
    normalized = normalize_utc(value) or value
    if normalized.tzinfo is None:
        return normalized
    return normalized.astimezone(timezone.utc).replace(tzinfo=None)
