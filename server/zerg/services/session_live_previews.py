from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionLivePreview
from zerg.services.provisional_events import EVENT_ORIGIN_LIVE_PROVISIONAL
from zerg.services.provisional_events import TranscriptPreview
from zerg.services.provisional_events import build_provisional_cursor
from zerg.services.provisional_events import build_provisional_key
from zerg.utils.time import normalize_utc

LIVE_PREVIEW_SOURCE = "codex_bridge_live"
LIVE_PREVIEW_PROGRESS_KIND = "bridge_live_transcript_delta"


@dataclass(frozen=True)
class LivePreviewCandidate:
    session_id: UUID
    thread_id: str | None
    turn_key: str
    seq: int | None
    preview_text: str
    provisional_cursor: str
    provisional_complete: bool
    preview_observed_at: datetime
    source: str
    last_observation_id: str


def live_preview_candidate_from_runtime_event(
    event: Any,
    *,
    observation_id: str,
) -> LivePreviewCandidate | None:
    payload = event.payload or {}
    if event.session_id is None:
        return None
    if (event.provider or "").strip().lower() != "codex":
        return None
    source = (event.source or "").strip()
    if source.lower() != LIVE_PREVIEW_SOURCE:
        return None
    if event.kind != "progress_signal":
        return None
    if payload.get("progress_kind") != LIVE_PREVIEW_PROGRESS_KIND:
        return None

    preview_text = str(payload.get("live_text") or "").strip()
    if not preview_text:
        return None

    thread_id = _optional_str(payload.get("thread_id"))
    turn_id = _optional_str(payload.get("turn_id"))
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=turn_id,
    )
    seq = _coerce_seq(payload.get("seq"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=bool(payload.get("turn_completed")),
        preview_observed_at=observed_at,
        source=source,
        last_observation_id=observation_id,
    )


def upsert_session_live_preview(db: Session, candidate: LivePreviewCandidate) -> bool:
    existing = db.get(SessionLivePreview, candidate.session_id)
    now = datetime.now(timezone.utc)
    if existing is not None and existing.last_observation_id == candidate.last_observation_id:
        return False
    if existing is not None and existing.superseded_at is not None and existing.turn_key == candidate.turn_key:
        superseded_at = normalize_utc(existing.superseded_at)
        candidate_at = normalize_utc(candidate.preview_observed_at)
        if superseded_at is not None and candidate_at is not None and candidate_at <= superseded_at:
            return False
    if existing is not None and not _candidate_should_replace(candidate, existing):
        return False

    if existing is None:
        db.add(
            SessionLivePreview(
                session_id=candidate.session_id,
                thread_id=candidate.thread_id,
                turn_key=candidate.turn_key,
                seq=candidate.seq,
                preview_text=candidate.preview_text,
                provisional_cursor=candidate.provisional_cursor,
                provisional_complete=1 if candidate.provisional_complete else 0,
                event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
                preview_observed_at=candidate.preview_observed_at,
                preview_updated_at=now,
                source=candidate.source,
                last_observation_id=candidate.last_observation_id,
            )
        )
        return True

    existing.thread_id = candidate.thread_id
    existing.turn_key = candidate.turn_key
    existing.seq = candidate.seq
    existing.preview_text = candidate.preview_text
    existing.provisional_cursor = candidate.provisional_cursor
    existing.provisional_complete = 1 if candidate.provisional_complete else 0
    existing.event_origin = EVENT_ORIGIN_LIVE_PROVISIONAL
    existing.preview_observed_at = candidate.preview_observed_at
    existing.preview_updated_at = now
    existing.source = candidate.source
    existing.last_observation_id = candidate.last_observation_id
    existing.superseded_at = None
    existing.superseded_by_event_id = None
    existing.superseded_reason = None
    return True


def supersede_session_live_preview(
    db: Session,
    *,
    session_id: UUID,
    durable_at: datetime | None,
    durable_event_id: int | None = None,
    reason: str = "superseded_by_durable",
) -> bool:
    row = db.get(SessionLivePreview, session_id)
    if row is None or row.superseded_at is not None:
        return False
    normalized_durable_at = normalize_utc(durable_at)
    preview_at = normalize_utc(row.preview_observed_at)
    if normalized_durable_at is None or preview_at is None or normalized_durable_at < preview_at:
        return False

    now = datetime.now(timezone.utc)
    row.superseded_at = normalized_durable_at
    row.preview_updated_at = now
    row.superseded_by_event_id = durable_event_id
    row.superseded_reason = reason
    return True


def load_session_live_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionLivePreview)
        .filter(SessionLivePreview.session_id.in_(session_ids))
        .filter(SessionLivePreview.superseded_at.is_(None))
        .all()
    )
    previews: dict[str, TranscriptPreview] = {}
    for row in rows:
        text = str(row.preview_text or "").strip()
        if not text:
            continue
        timestamp = normalize_utc(row.preview_observed_at)
        if timestamp is None:
            continue
        previews[str(row.session_id)] = TranscriptPreview(
            event_id=int(row.seq or 0),
            text=text,
            event_origin=row.event_origin or EVENT_ORIGIN_LIVE_PROVISIONAL,
            timestamp=timestamp,
            provisional_cursor=row.provisional_cursor,
            provisional_complete=bool(row.provisional_complete),
        )
    return previews


def _candidate_should_replace(candidate: LivePreviewCandidate, existing: SessionLivePreview) -> bool:
    candidate_at = normalize_utc(candidate.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    existing_at = normalize_utc(existing.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    if candidate.turn_key != existing.turn_key:
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is not None:
        if candidate.seq != existing.seq:
            return candidate.seq > existing.seq
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is None:
        return True
    if candidate.seq is None and existing.seq is not None:
        return False
    return candidate_at >= existing_at


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
