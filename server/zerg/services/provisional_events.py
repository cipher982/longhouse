from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.models.agents import AgentEvent

EVENT_ORIGIN_DURABLE = "durable"
EVENT_ORIGIN_LIVE_PROVISIONAL = "live_provisional"
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
    role: str = "assistant"
    tool_name: str | None = None
    tool_input_json: dict | None = None
    tool_output_text: str | None = None
    tool_call_id: str | None = None
    tool_call_state: str | None = None


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
    from zerg import database as database_module
    from zerg.services.session_live_previews import load_session_live_preview_map

    if not database_module.live_store_configured():
        # Standalone unit-test databases have no split-store topology.
        return load_session_live_preview_map(db, session_ids)
    from zerg.models.live_store import LiveSessionLivePreview
    from zerg.services.catalog_facts import hydrate_catalog_row
    from zerg.services.catalog_facts import session_facts_map
    from zerg.services.session_live_previews import preview_map_from_rows

    facts_by_session = session_facts_map([str(session_id) for session_id in session_ids])
    rows = [
        row
        for facts in facts_by_session.values()
        if (row := hydrate_catalog_row(LiveSessionLivePreview, facts.get("live_preview"))) is not None
    ]
    return preview_map_from_rows(rows)


def _clean_identity_part(value: str | None, *, fallback: str) -> str:
    value = (value or "").strip()
    return value or fallback
