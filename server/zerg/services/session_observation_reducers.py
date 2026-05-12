"""Reducers from raw session observations into read models."""

from __future__ import annotations

import json

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionObservation
from zerg.services.provisional_events import materialize_bridge_transcript_event
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.services.session_observations import OBS_KIND_PROVIDER_SOURCE_LINE


def reduce_bridge_transcript_observation(db: Session, observation: SessionObservation) -> AgentEvent | None:
    if observation.kind != OBS_KIND_BRIDGE_TRANSCRIPT_DELTA:
        return None
    if observation.session_id is None:
        return None

    payload = _observation_payload(observation)
    bridge_payload = payload.get("payload")
    if not isinstance(bridge_payload, dict):
        return None
    bridge_payload = dict(bridge_payload)
    bridge_payload["_session_observation_id"] = observation.observation_id

    return materialize_bridge_transcript_event(
        db,
        session_id=observation.session_id,
        provider=observation.provider,
        source=observation.source,
        occurred_at=observation.observed_at,
        received_at=observation.received_at,
        payload=bridge_payload,
    )


def reduce_source_line_observation(db: Session, observation: SessionObservation) -> AgentSourceLine | None:
    if observation.kind != OBS_KIND_PROVIDER_SOURCE_LINE:
        return None
    if observation.session_id is None or observation.source_path is None or observation.source_offset is None:
        return None

    payload = _observation_payload(observation)
    raw_json = payload.get("raw_json")
    line_hash = payload.get("line_hash")
    if not isinstance(raw_json, str) or not isinstance(line_hash, str):
        return None

    branch_id = _coerce_int(payload.get("branch_id"))
    revision = _coerce_int(payload.get("revision"))
    if branch_id is None or revision is None:
        return None

    raw_json_z = compress_raw_json(raw_json)
    stmt = (
        sqlite_insert(AgentSourceLine)
        .values(
            session_id=observation.session_id,
            source_path=observation.source_path,
            source_offset=int(observation.source_offset),
            branch_id=branch_id,
            revision=revision,
            is_branch_copy=0,
            raw_json="",
            raw_json_z=raw_json_z,
            raw_json_codec=CODEC_ZSTD,
            line_hash=line_hash,
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
