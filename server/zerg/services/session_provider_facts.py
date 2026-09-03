"""Project provider-authored facts onto the session surfaces clients render.

A provider writes turn durations, recaps and titles into its own transcript on
lines the render surface never shows. The engine parses them into typed facts
and ships them beside the raw bytes; catalogd keeps one row per source line.
This module turns those rows into the decorations the API serves: the
`turn_end` footer on the event a turn finished on, and the session's
`last_turn`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from zerg.catalogd.client import CatalogClient

logger = logging.getLogger(__name__)

_LIST_TIMEOUT_SECONDS = 1.0
TURN_DURATION = "turn.duration"


def _parse_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def session_provider_facts(catalogd: CatalogClient, session_id: UUID | str) -> list[dict[str, Any]]:
    """Recent provider facts for a session, newest first, payload decoded."""
    try:
        result = await catalogd.call(
            "session.provider_facts.list.v2",
            {"session_id": str(session_id)},
            timeout_seconds=_LIST_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a page without provider facts still renders
        logger.warning("Failed to list provider facts for session %s", session_id, exc_info=True)
        return []
    facts = result.get("facts") if isinstance(result, dict) else None
    if not isinstance(facts, list):
        return []
    projected: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        payload = fact.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        at = _parse_at(fact.get("at"))
        if at is None or not isinstance(payload, dict):
            continue
        projected.append(
            {
                "kind": str(fact.get("kind") or ""),
                "at": at,
                "source_position": fact.get("source_position"),
                "payload": payload,
            }
        )
    return projected


def _event_time(event: dict[str, object]) -> datetime | None:
    return _parse_at(event.get("timestamp"))


def turn_ends_by_event(facts: list[dict[str, Any]], events: list[dict[str, object]]) -> dict[str, dict[str, Any]]:
    """Anchor each turn-duration fact to the event its turn ended on.

    The anchor is the last non-user event at or before the fact's time. Claude
    writes `turn_duration` after the turn's final assistant message; Codex's
    `task_complete` behaves the same, so the time rule is provider-neutral.
    An anchor off the loaded page simply yields no decoration on that page.
    """
    candidates = [
        (time, str(event["event_id"]))
        for event in events
        if event.get("role") not in {"user", "system"} and (time := _event_time(event)) is not None and event.get("event_id")
    ]
    candidates.sort(key=lambda item: item[0])
    decorations: dict[str, dict[str, Any]] = {}
    for fact in sorted((f for f in facts if f.get("kind") == TURN_DURATION), key=lambda f: f["at"]):
        anchor = None
        for time, event_id in candidates:
            if time <= fact["at"]:
                anchor = event_id
            else:
                break
        if anchor is None:
            continue
        payload = fact["payload"]
        duration_ms = payload.get("duration_ms")
        if not isinstance(duration_ms, int):
            continue
        message_count = payload.get("message_count")
        decorations[anchor] = {
            "duration_ms": duration_ms,
            "ended_at": fact["at"].isoformat(),
            "message_count": message_count if isinstance(message_count, int) else None,
        }
    return decorations


def last_turn(facts: list[dict[str, Any]], decorations: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The newest finished turn, with its anchor event when it is on the page."""
    latest = None
    for fact in facts:
        if fact.get("kind") != TURN_DURATION or not isinstance(fact["payload"].get("duration_ms"), int):
            continue
        if latest is None or fact["at"] > latest["at"]:
            latest = fact
    if latest is None:
        return None
    ended_at = latest["at"].isoformat()
    event_id = next((event_id for event_id, turn in decorations.items() if turn["ended_at"] == ended_at), None)
    return {"duration_ms": latest["payload"]["duration_ms"], "ended_at": ended_at, "event_id": event_id}


__all__ = ["TURN_DURATION", "last_turn", "session_provider_facts", "turn_ends_by_event"]
