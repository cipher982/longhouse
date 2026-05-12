from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.utils.time import normalize_utc

EVENT_ORIGIN_DURABLE = "durable"
EVENT_ORIGIN_LIVE_PROVISIONAL = "live_provisional"

PROVISIONAL_ACTIVE = "active"
PROVISIONAL_RECONCILED = "reconciled"
PROVISIONAL_SUPERSEDED = "superseded"

PROVISIONAL_MATCH_WINDOW = timedelta(minutes=30)
MIN_PREFIX_MATCH_CHARS = 12


@dataclass(frozen=True)
class TranscriptPreview:
    event_id: int
    text: str
    event_origin: str
    timestamp: datetime
    provisional_cursor: str | None
    provisional_complete: bool


def visible_transcript_event_predicate():
    return or_(
        AgentEvent.event_origin.is_(None),
        AgentEvent.event_origin == EVENT_ORIGIN_DURABLE,
        and_(
            AgentEvent.event_origin == EVENT_ORIGIN_LIVE_PROVISIONAL,
            AgentEvent.provisional_state == PROVISIONAL_ACTIVE,
        ),
    )


def durable_transcript_event_predicate():
    return or_(AgentEvent.event_origin.is_(None), AgentEvent.event_origin == EVENT_ORIGIN_DURABLE)


def active_provisional_event_predicate():
    return and_(
        AgentEvent.event_origin == EVENT_ORIGIN_LIVE_PROVISIONAL,
        AgentEvent.provisional_state == PROVISIONAL_ACTIVE,
    )


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


def materialize_bridge_transcript_event(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    source: str,
    occurred_at: datetime | None,
    received_at: datetime | None,
    payload: dict[str, Any],
) -> AgentEvent | None:
    if provider.strip().lower() != "codex":
        return None
    if source.strip().lower() != "codex_bridge_live":
        return None
    if payload.get("progress_kind") != "bridge_live_transcript_delta":
        return None

    text = str(payload.get("live_text") or "").strip()
    if not text:
        return None

    session = db.query(AgentSession).filter(AgentSession.id == session_id).first()
    if session is None:
        return None

    seq = _coerce_seq(payload.get("seq"))
    key = build_provisional_key(
        source=source,
        session_id=session_id,
        thread_id=_optional_str(payload.get("thread_id")),
        turn_id=_optional_str(payload.get("turn_id")),
    )
    cursor = build_provisional_cursor(key=key, seq=seq)
    timestamp = normalize_utc(occurred_at) or normalize_utc(received_at) or datetime.now(timezone.utc)
    complete = 1 if bool(payload.get("turn_completed")) else 0

    session_events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id)
    existing = session_events.filter(AgentEvent.provisional_key == key).first()
    if existing is not None:
        existing_seq = existing.provisional_seq
        if seq is not None and existing_seq is not None and seq < existing_seq:
            return existing
        if seq is not None and existing_seq is not None and seq == existing_seq:
            incoming_observation_id = _payload_observation_id(payload)
            existing_observation_id = _existing_observation_id(existing)
            if existing_observation_id is None:
                # Rows created before the observation reducer did not persist a
                # source observation id. Keep those rows stable instead of
                # letting same-seq replays overwrite them.
                return existing
            if incoming_observation_id is None or incoming_observation_id >= existing_observation_id:
                return existing
        if seq is None and existing_seq is not None:
            return existing
        if existing.provisional_state in {PROVISIONAL_RECONCILED, PROVISIONAL_SUPERSEDED}:
            return existing
        # Keep a still-active provisional row attached to the current head if
        # the durable archive forked after the bridge first observed the turn.
        existing.branch_id = _get_head_branch_id(db, session_id)
        existing.role = "assistant"
        existing.content_text = text
        existing.timestamp = timestamp
        existing.event_origin = EVENT_ORIGIN_LIVE_PROVISIONAL
        existing.provisional_state = PROVISIONAL_ACTIVE
        existing.provisional_cursor = cursor
        existing.provisional_seq = seq
        existing.provisional_complete = complete
        existing.raw_json = _encode_provisional_raw_json(payload)
        existing.raw_json_z = None
        existing.raw_json_codec = CODEC_PLAIN
        existing.event_hash = _provisional_event_hash(key=key, text=text)
        return existing

    event = AgentEvent(
        session_id=session_id,
        branch_id=_get_head_branch_id(db, session_id),
        role="assistant",
        content_text=text,
        timestamp=timestamp,
        source_path=None,
        source_offset=None,
        event_hash=_provisional_event_hash(key=key, text=text),
        raw_json=_encode_provisional_raw_json(payload),
        raw_json_z=None,
        raw_json_codec=CODEC_PLAIN,
        schema_version=1,
        event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
        provisional_state=PROVISIONAL_ACTIVE,
        provisional_key=key,
        provisional_cursor=cursor,
        provisional_seq=seq,
        provisional_complete=complete,
        reconciled_event_id=None,
    )
    db.add(event)
    db.flush()
    return event


def reconcile_provisional_transcript_events(db: Session, *, session_id: UUID) -> int:
    provisional_rows = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(active_provisional_event_predicate())
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .all()
    )
    if not provisional_rows:
        return 0

    durable_rows = (
        db.query(AgentEvent)
        .filter(AgentEvent.session_id == session_id)
        .filter(durable_transcript_event_predicate())
        .filter(AgentEvent.role == "assistant")
        .filter(AgentEvent.tool_name.is_(None))
        .filter(AgentEvent.content_text.isnot(None))
        .order_by(AgentEvent.timestamp.asc(), AgentEvent.id.asc())
        .all()
    )
    if not durable_rows:
        return 0

    durable_timestamps = [normalize_utc(row.timestamp) for row in durable_rows]
    latest_durable_at = max((timestamp for timestamp in durable_timestamps if timestamp is not None), default=None)
    changed = 0
    matched_durable_ids: set[int] = set()

    for provisional in provisional_rows:
        match = _match_durable_event(provisional, durable_rows, matched_durable_ids)
        if match is not None:
            provisional.provisional_state = PROVISIONAL_RECONCILED
            provisional.reconciled_event_id = match.id
            matched_durable_ids.add(match.id)
            changed += 1
            continue

        provisional_at = normalize_utc(provisional.timestamp)
        if latest_durable_at is not None and provisional_at is not None and latest_durable_at > provisional_at:
            provisional.provisional_state = PROVISIONAL_SUPERSEDED
            changed += 1

    if changed:
        db.flush()
    return changed


def supersede_active_provisional_transcript_events(db: Session, *, session_id: UUID) -> int:
    session_events = db.query(AgentEvent).filter(AgentEvent.session_id == session_id)
    rows = session_events.filter(active_provisional_event_predicate()).all()
    for row in rows:
        row.provisional_state = PROVISIONAL_SUPERSEDED
    if rows:
        db.flush()
    return len(rows)


def load_active_provisional_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}

    ranked = (
        db.query(
            AgentEvent.id.label("event_id"),
            AgentEvent.session_id.label("session_id"),
            AgentEvent.content_text.label("text"),
            AgentEvent.event_origin.label("event_origin"),
            AgentEvent.timestamp.label("timestamp"),
            AgentEvent.provisional_cursor.label("provisional_cursor"),
            AgentEvent.provisional_complete.label("provisional_complete"),
            func.row_number()
            .over(
                partition_by=AgentEvent.session_id,
                order_by=(AgentEvent.timestamp.desc(), AgentEvent.id.desc()),
            )
            .label("rn"),
        )
        .filter(AgentEvent.session_id.in_(session_ids))
        .filter(active_provisional_event_predicate())
        .filter(AgentEvent.content_text.isnot(None))
        .subquery()
    )
    rows = db.query(ranked).filter(ranked.c.rn == 1).all()

    previews: dict[str, TranscriptPreview] = {}
    for row in rows:
        text = str(row.text or "").strip()
        if not text:
            continue
        previews[str(row.session_id)] = TranscriptPreview(
            event_id=int(row.event_id),
            text=text,
            event_origin=str(row.event_origin or EVENT_ORIGIN_LIVE_PROVISIONAL),
            timestamp=row.timestamp,
            provisional_cursor=row.provisional_cursor,
            provisional_complete=bool(row.provisional_complete),
        )
    return previews


def _match_durable_event(
    provisional: AgentEvent,
    durable_rows: list[AgentEvent],
    matched_durable_ids: set[int],
) -> AgentEvent | None:
    provisional_text = _normalize_text(provisional.content_text)
    if not provisional_text:
        return None
    provisional_at = normalize_utc(provisional.timestamp)
    candidates: list[AgentEvent] = []
    for durable in durable_rows:
        if durable.id in matched_durable_ids:
            continue
        durable_text = _normalize_text(durable.content_text)
        if not durable_text:
            continue
        if provisional_at is not None:
            durable_at = normalize_utc(durable.timestamp)
            if durable_at is not None and abs(durable_at - provisional_at) > PROVISIONAL_MATCH_WINDOW:
                continue
        if durable_text == provisional_text:
            candidates.append(durable)
            continue
        if len(provisional_text) >= MIN_PREFIX_MATCH_CHARS and durable_text.startswith(provisional_text):
            candidates.append(durable)
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row.timestamp, row.id))


def _get_head_branch_id(db: Session, session_id: UUID) -> int | None:
    row = (
        db.query(AgentSessionBranch.id)
        .filter(AgentSessionBranch.session_id == session_id)
        .filter(AgentSessionBranch.is_head == 1)
        .order_by(AgentSessionBranch.id.desc())
        .first()
    )
    return int(row[0]) if row else None


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


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _provisional_event_hash(*, key: str, text: str) -> str:
    return hashlib.sha256(f"{key}\n{text}".encode("utf-8")).hexdigest()


def _encode_provisional_raw_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": "longhouse_provisional_transcript",
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_observation_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("_session_observation_id")
    text = str(value or "").strip()
    return text or None


def _existing_observation_id(event: AgentEvent) -> str | None:
    raw_json = event.raw_json
    if not raw_json:
        return None
    try:
        encoded = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(encoded, dict):
        return None
    payload = encoded.get("payload")
    if not isinstance(payload, dict):
        return None
    return _payload_observation_id(payload)
