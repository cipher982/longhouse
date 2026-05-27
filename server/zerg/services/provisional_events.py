from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionObservation
from zerg.services.session_observations import OBS_KIND_BRIDGE_TRANSCRIPT_DELTA
from zerg.utils.time import normalize_utc

EVENT_ORIGIN_DURABLE = "durable"
EVENT_ORIGIN_LIVE_PROVISIONAL = "live_provisional"
MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION = 50
BRIDGE_TRANSCRIPT_OBSERVATION_KEEP_PER_SESSION = 200
BRIDGE_TRANSCRIPT_OBSERVATION_CLEANUP_BATCH_SIZE = 5000
BRIDGE_TRANSCRIPT_OBSERVATION_CLEANUP_MAX_SESSIONS = 25

logger = logging.getLogger(__name__)


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

    rows: list[SessionObservation] = []
    for session_id in session_ids:
        rows.extend(
            db.query(SessionObservation)
            .filter(SessionObservation.session_id == session_id)
            .filter(SessionObservation.source == "codex_bridge_live")
            .filter(SessionObservation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
            .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
            .limit(MAX_ACTIVE_PREVIEW_OBSERVATIONS_PER_SESSION)
            .all()
        )
    candidates_by_turn: dict[tuple[str, str], _PreviewCandidate] = {}
    for row in rows:
        candidate = _preview_candidate_from_bridge_observation(row)
        if candidate is None:
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


def cleanup_bridge_transcript_preview_observations(
    db: Session,
    *,
    session_ids: list[UUID] | None = None,
    keep_per_session: int = BRIDGE_TRANSCRIPT_OBSERVATION_KEEP_PER_SESSION,
    batch_size: int = BRIDGE_TRANSCRIPT_OBSERVATION_CLEANUP_BATCH_SIZE,
    max_sessions: int = BRIDGE_TRANSCRIPT_OBSERVATION_CLEANUP_MAX_SESSIONS,
    commit: bool = True,
    latest_durable_at_by_session: dict[UUID, datetime] | None = None,
) -> int:
    """Prune disposable Codex live-preview observations.

    Bridge transcript deltas are raw live UI evidence, not durable archive.
    Durable transcript rows and the latest bounded preview window are enough
    for timeline rendering, so keep cleanup incremental to avoid long SQLite
    writer stalls on large dogfood databases.
    """
    if keep_per_session < 1 or batch_size < 1 or max_sessions < 1:
        return 0

    candidate_session_ids = session_ids or _bridge_preview_session_ids(db, max_sessions=max_sessions)
    if not candidate_session_ids:
        return 0

    removed = 0
    durable_activity = _latest_durable_activity_map(db, candidate_session_ids)
    if latest_durable_at_by_session:
        for session_id, latest_durable_at in latest_durable_at_by_session.items():
            durable_activity[str(session_id)] = normalize_utc(latest_durable_at)
    for session_id in candidate_session_ids[:max_sessions]:
        remaining = batch_size - removed
        if remaining <= 0:
            break
        latest_durable_at = durable_activity.get(str(session_id))
        if latest_durable_at is not None:
            removed += _delete_bridge_preview_observation_ids(
                db,
                _bridge_preview_ids_before_durable(db, session_id, latest_durable_at, limit=remaining),
            )
        remaining = batch_size - removed
        if remaining <= 0:
            break
        removed += _delete_bridge_preview_observation_ids(
            db,
            _bridge_preview_ids_over_cap(db, session_id, keep_per_session=keep_per_session, limit=remaining),
        )

    if removed and commit:
        db.commit()
    if removed:
        logger.info("live preview cleanup: removed %d bridge transcript observation rows", removed)
    return removed


def _latest_durable_activity_map(db: Session, session_ids: list[UUID]) -> dict[str, datetime | None]:
    return {
        str(row.id): normalize_utc(row.last_activity_at)
        for row in (
            db.query(AgentSession.id, AgentSession.last_activity_at)
            .filter(AgentSession.id.in_(session_ids))
            .filter(AgentSession.last_activity_at.isnot(None))
            .all()
        )
    }


def _bridge_preview_session_ids(db: Session, *, max_sessions: int) -> list[UUID]:
    rows = (
        db.query(AgentSession.id)
        .filter(AgentSession.provider == "codex")
        .order_by(func.coalesce(AgentSession.last_activity_at, AgentSession.started_at).desc(), AgentSession.id.desc())
        .limit(max_sessions)
        .all()
    )
    return [row[0] for row in rows if row[0] is not None]


def _bridge_preview_query(db: Session, session_id: UUID):
    return (
        db.query(SessionObservation.id)
        .filter(SessionObservation.session_id == session_id)
        .filter(SessionObservation.source == "codex_bridge_live")
        .filter(SessionObservation.kind == OBS_KIND_BRIDGE_TRANSCRIPT_DELTA)
    )


def _bridge_preview_ids_before_durable(
    db: Session,
    session_id: UUID,
    latest_durable_at: datetime,
    *,
    limit: int,
) -> list[int]:
    rows = (
        _bridge_preview_query(db, session_id)
        .filter(SessionObservation.observed_at < latest_durable_at)
        .order_by(SessionObservation.observed_at.asc(), SessionObservation.id.asc())
        .limit(limit)
        .all()
    )
    return [int(row[0]) for row in rows]


def _bridge_preview_ids_over_cap(
    db: Session,
    session_id: UUID,
    *,
    keep_per_session: int,
    limit: int,
) -> list[int]:
    rows = (
        _bridge_preview_query(db, session_id)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .offset(keep_per_session)
        .limit(limit)
        .all()
    )
    return [int(row[0]) for row in rows]


def _delete_bridge_preview_observation_ids(db: Session, ids: list[int]) -> int:
    if not ids:
        return 0
    return db.query(SessionObservation).filter(SessionObservation.id.in_(ids)).delete(synchronize_session=False)


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
