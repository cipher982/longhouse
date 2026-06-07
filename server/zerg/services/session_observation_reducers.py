"""Reducers from raw session observations into read models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.services.agents.compaction import classify_compaction_kind
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_PROVIDER_SOURCE_LINE
from zerg.utils.time import normalize_utc


@dataclass(frozen=True)
class ProviderEventReduction:
    event: AgentEvent | None
    inserted: bool


def reduce_bridge_transcript_observation(db: Session, observation: SessionObservation) -> AgentEvent | None:
    _ = (db, observation)
    return None


def reduce_source_line_observation(db: Session, observation: SessionObservation) -> AgentSourceLine | None:
    if observation.kind != OBS_KIND_PROVIDER_SOURCE_LINE:
        return None
    if observation.session_id is None or observation.source_path is None or observation.source_offset is None:
        return None

    payload = _observation_payload(observation)
    raw_json = payload.get("raw_json")
    line_hash = payload.get("line_hash")
    # line_hash is the durable identity; raw_json may be absent when raw bytes
    # live only in the archive. The slim source_lines row (the ordering/branch
    # index) must be written either way — only the raw payload columns depend on
    # raw_json being present.
    if not isinstance(line_hash, str):
        return None

    branch_id = _coerce_int(payload.get("branch_id"))
    revision = _coerce_int(payload.get("revision"))
    if branch_id is None or revision is None:
        return None

    if isinstance(raw_json, str):
        raw_values = {"raw_json": "", "raw_json_z": compress_raw_json(raw_json), "raw_json_codec": CODEC_ZSTD}
    else:
        raw_values = {"raw_json": "", "raw_json_z": None, "raw_json_codec": CODEC_PLAIN}
    stmt = (
        sqlite_insert(AgentSourceLine)
        .values(
            session_id=observation.session_id,
            thread_id=observation.thread_id,
            source_path=observation.source_path,
            source_offset=int(observation.source_offset),
            branch_id=branch_id,
            revision=revision,
            is_branch_copy=0,
            line_hash=line_hash,
            **raw_values,
        )
        .on_conflict_do_nothing(
            index_elements=["session_id", "branch_id", "source_path", "source_offset", "line_hash"],
        )
    )
    db.execute(stmt)
    db.flush()
    return (
        db.query(AgentSourceLine)
        .filter(AgentSourceLine.session_id == observation.session_id)
        .filter(AgentSourceLine.branch_id == branch_id)
        .filter(AgentSourceLine.source_path == observation.source_path)
        .filter(AgentSourceLine.source_offset == int(observation.source_offset))
        .filter(AgentSourceLine.line_hash == line_hash)
        .first()
    )


def reduce_provider_event_observation(db: Session, observation: SessionObservation) -> ProviderEventReduction:
    if observation.kind != OBS_KIND_PROVIDER_EVENT:
        return ProviderEventReduction(event=None, inserted=False)
    if observation.session_id is None:
        return ProviderEventReduction(event=None, inserted=False)

    payload = _observation_payload(observation)
    branch_id = _coerce_int(payload.get("branch_id"))
    role = payload.get("role")
    event_hash = payload.get("event_hash")
    if branch_id is None or not isinstance(role, str) or not isinstance(event_hash, str):
        raise ValueError(f"provider_event observation {observation.observation_id} missing required reducer payload")

    timestamp = _coerce_datetime(payload.get("timestamp")) or normalize_utc(observation.observed_at) or datetime.now(timezone.utc)
    source_offset = int(observation.source_offset) if observation.source_offset is not None else None
    event_uuid = _optional_str(payload.get("event_uuid"))
    existing = _find_existing_provider_event(
        db,
        observation=observation,
        branch_id=branch_id,
        role=role,
        timestamp=timestamp,
        event_hash=event_hash,
        event_uuid=event_uuid,
        content_text=_optional_str(payload.get("content_text")),
        tool_name=_optional_str(payload.get("tool_name")),
        tool_call_id=_optional_str(payload.get("tool_call_id")),
        source_offset=source_offset,
    )
    if existing is not None:
        return ProviderEventReduction(event=existing, inserted=False)

    raw_json = _optional_str(payload.get("raw_json"))
    raw_json_z = compress_raw_json(raw_json) if raw_json is not None else None
    # Prefer the structured compaction_kind carried in the payload (derived from
    # raw at ingest). Fall back to classifying raw for older observations that
    # predate the field. Never depends on stored raw at projection time.
    compaction_kind = payload.get("compaction_kind")
    if compaction_kind is None:
        compaction_kind = classify_compaction_kind(raw_json)
    stmt = (
        sqlite_insert(AgentEvent)
        .values(
            session_id=observation.session_id,
            thread_id=observation.thread_id,
            branch_id=branch_id,
            role=role,
            content_text=_optional_str(payload.get("content_text")),
            tool_name=_optional_str(payload.get("tool_name")),
            tool_input_json=payload.get("tool_input_json"),
            tool_output_text=_optional_str(payload.get("tool_output_text")),
            tool_call_id=_optional_str(payload.get("tool_call_id")),
            timestamp=timestamp,
            source_path=observation.source_path,
            source_offset=source_offset,
            event_hash=event_hash,
            raw_json=None,
            raw_json_z=raw_json_z,
            raw_json_codec=CODEC_ZSTD if raw_json_z else CODEC_PLAIN,
            compaction_kind=compaction_kind,
            schema_version=1,
            event_uuid=event_uuid,
            parent_event_uuid=_optional_str(payload.get("parent_event_uuid")),
            event_origin="durable",
        )
        .on_conflict_do_nothing()
    )
    result = db.execute(stmt)
    db.flush()
    inserted = bool(result.rowcount and result.rowcount > 0)
    event = _find_existing_provider_event(
        db,
        observation=observation,
        branch_id=branch_id,
        role=role,
        timestamp=timestamp,
        event_hash=event_hash,
        event_uuid=event_uuid,
        content_text=_optional_str(payload.get("content_text")),
        tool_name=_optional_str(payload.get("tool_name")),
        tool_call_id=_optional_str(payload.get("tool_call_id")),
        source_offset=source_offset,
    )
    return ProviderEventReduction(event=event, inserted=inserted)


def _observation_payload(observation: SessionObservation) -> dict:
    raw = observation.payload_json
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return normalize_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return normalize_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _optional_str(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value


def _find_existing_provider_event(
    db: Session,
    *,
    observation: SessionObservation,
    branch_id: int,
    role: str,
    timestamp: datetime,
    event_hash: str,
    event_uuid: str | None,
    content_text: str | None,
    tool_name: str | None,
    tool_call_id: str | None,
    source_offset: int | None,
) -> AgentEvent | None:
    base = db.query(AgentEvent).filter(AgentEvent.session_id == observation.session_id).filter(AgentEvent.branch_id == branch_id)
    if event_uuid:
        row = base.filter(AgentEvent.event_uuid == event_uuid).first()
        if row is not None:
            return row
    if observation.source_path is not None and source_offset is not None:
        row = (
            base.filter(AgentEvent.source_path == observation.source_path)
            .filter(AgentEvent.source_offset == source_offset)
            .filter(AgentEvent.event_hash == event_hash)
            .first()
        )
        if row is not None:
            return row
    return (
        base.filter(AgentEvent.source_path.is_(None))
        .filter(AgentEvent.source_offset.is_(None))
        .filter(AgentEvent.event_hash == event_hash)
        .filter(AgentEvent.role == role)
        .filter(AgentEvent.timestamp == timestamp)
        .filter(AgentEvent.content_text == content_text)
        .filter(AgentEvent.tool_name == tool_name)
        .filter(AgentEvent.tool_call_id == tool_call_id)
        .first()
    )
