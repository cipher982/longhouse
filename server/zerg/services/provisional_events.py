from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import SessionObservation
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.utils.time import normalize_utc

EVENT_ORIGIN_DURABLE = "durable"
EVENT_ORIGIN_LIVE_PROVISIONAL = "live_provisional"
MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION = 50


@dataclass(frozen=True)
class TranscriptPreview:
    event_id: int
    text: str
    event_origin: str
    timestamp: datetime
    provisional_cursor: str | None
    provisional_complete: bool


@dataclass(frozen=True)
class _PreviewCandidate:
    session_id: str
    turn_key: str
    seq: int | None
    observation_id: str
    row_id: int
    preview: TranscriptPreview


def visible_transcript_event_predicate():
    return durable_transcript_event_predicate()


def durable_transcript_event_predicate():
    return or_(AgentEvent.event_origin.is_(None), AgentEvent.event_origin == EVENT_ORIGIN_DURABLE)


def build_provisional_key(*, source: str, session_id: UUID | str, thread_id: str | None, turn_id: str | None) -> str:
    return ":".join(
        [
            source,
            str(session_id),
            _clean_identity_part(thread_id, fallback="unknown-thread"),
            _clean_identity_part(turn_id, fallback="unknown-turn"),
        ]
    )


def build_provisional_cursor(*, key: str, seq: int | None) -> str:
    return f"{key}:{seq}" if seq is not None else f"{key}:unknown-seq"


def load_active_provisional_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}

    durable_activity = {
        str(row.session_id): normalize_utc(row.latest_activity_at)
        for row in (
            db.query(
                AgentEvent.session_id.label("session_id"),
                func.max(AgentEvent.timestamp).label("latest_activity_at"),
            )
            .filter(AgentEvent.session_id.in_(session_ids))
            .filter(durable_transcript_event_predicate())
            .group_by(AgentEvent.session_id)
            .all()
        )
    }

    rows: list[SessionObservation] = []
    for session_id in session_ids:
        query = (
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session_id)
            .filter(SessionObservation.source == "codex_bridge_live")
            .filter(SessionObservation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
        )
        latest_durable_at = durable_activity.get(str(session_id))
        if latest_durable_at is not None:
            query = query.filter(SessionObservation.observed_at >= latest_durable_at)
        rows.extend(
            query.order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
            .limit(MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION)
            .all()
        )
    candidates_by_turn: dict[tuple[str, str], _PreviewCandidate] = {}
    for row in rows:
        candidate = _preview_candidate_from_bridge_observation(row)
        if candidate is None:
            continue
        preview = candidate.preview
        latest_durable_at = durable_activity.get(str(row.session_id))
        preview_at = normalize_utc(preview.timestamp)
        if latest_durable_at is not None and preview_at is not None and latest_durable_at > preview_at:
            continue
        key = (candidate.session_id, candidate.turn_key)
        existing = candidates_by_turn.get(key)
        if existing is None or _candidate_is_newer(candidate, existing):
            candidates_by_turn[key] = candidate

    previews: dict[str, TranscriptPreview] = {}
    for candidate in candidates_by_turn.values():
        existing = previews.get(candidate.session_id)
        if existing is None or (candidate.preview.timestamp, candidate.row_id) > (
            existing.timestamp,
            existing.event_id,
        ):
            previews[candidate.session_id] = candidate.preview
    return previews


def _preview_candidate_from_bridge_observation(row: SessionObservation) -> _PreviewCandidate | None:
    if row.session_id is None:
        return None
    payload = _observation_payload(row)
    bridge_payload = payload.get("payload")
    if not isinstance(bridge_payload, dict):
        return None
    if bridge_payload.get("progress_kind") != "bridge_live_transcript_delta":
        return None

    text = str(bridge_payload.get("live_text") or "").strip()
    if not text:
        return None

    turn_key = build_provisional_key(
        source=row.source or "codex_bridge_live",
        session_id=row.session_id,
        thread_id=_optional_str(bridge_payload.get("thread_id")),
        turn_id=_optional_str(bridge_payload.get("turn_id")),
    )
    seq = _coerce_seq(bridge_payload.get("seq"))
    cursor = build_provisional_cursor(key=turn_key, seq=seq)
    timestamp = normalize_utc(row.observed_at) or row.received_at
    return _PreviewCandidate(
        session_id=str(row.session_id),
        turn_key=turn_key,
        seq=seq,
        observation_id=row.observation_id,
        row_id=int(row.id),
        preview=TranscriptPreview(
            event_id=int(row.id),
            text=text,
            event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
            timestamp=timestamp,
            provisional_cursor=cursor,
            provisional_complete=bool(bridge_payload.get("turn_completed")),
        ),
    )


def _candidate_is_newer(candidate: _PreviewCandidate, existing: _PreviewCandidate) -> bool:
    if candidate.seq is not None and existing.seq is not None:
        return candidate.seq > existing.seq
    if candidate.seq is not None and existing.seq is None:
        return True
    if candidate.seq is None and existing.seq is not None:
        return False
    return (candidate.preview.timestamp, candidate.row_id) > (existing.preview.timestamp, existing.row_id)


def _clean_identity_part(value: str | None, *, fallback: str) -> str:
    value = (value or "").strip()
    return value or fallback


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _coerce_seq(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _observation_payload(observation: SessionObservation) -> dict[str, Any]:
    raw_json = observation.payload_json
    if not raw_json:
        return {}
    try:
        encoded = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    return encoded if isinstance(encoded, dict) else {}
