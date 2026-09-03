"""Link Longhouse sends to the durable transcript events they became.

A provider writes injected text into its own transcript with no Longhouse
identity attached, so the honest link is text plus time, made once at ingest
and persisted on the receipt. Clients then reconcile their optimistic rows by
`client_request_id` instead of guessing, whether or not the echo is on the
page they have loaded.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from zerg.catalogd.client import CatalogClient

logger = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")
_LINK_TIMEOUT_SECONDS = 2.0
_LIST_TIMEOUT_SECONDS = 4.25


def normalize_input_text(value: str | None) -> str:
    """Whitespace-insensitive equality: a bridge may trim or re-wrap the text it injects."""
    return _WHITESPACE.sub(" ", (value or "")).strip()


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def user_input_candidates(records: list[Any]) -> list[dict[str, Any]]:
    """Durable user text events from one ingested render batch, as link candidates.

    Accepts render records as parsed objects or as their wire dicts.
    """
    candidates: list[dict[str, Any]] = []
    for record in records:
        if _field(record, "role") != "user" or _field(record, "tool_name"):
            continue
        text = _field(record, "content_text")
        event_id = _field(record, "event_id")
        order_time_us = _field(record, "order_time_us")
        if not isinstance(text, str) or not text.strip() or not isinstance(event_id, str) or type(order_time_us) is not int:
            continue
        candidates.append(
            {
                "event_id": event_id,
                "timestamp": datetime.fromtimestamp(order_time_us / 1_000_000, tz=UTC).isoformat(),
                "text": text,
            }
        )
    return candidates


async def link_ingested_user_inputs(catalogd: CatalogClient, session_id: UUID | str, records: list[Any]) -> int:
    """Best effort after a commit: link delivered sends to the user events in this batch."""
    candidates = user_input_candidates(records)
    if not candidates:
        return 0
    try:
        result = await catalogd.call(
            "session.input.link_events.v2",
            {
                "session_id": str(session_id),
                "candidates": candidates,
                "observed_at": datetime.now(UTC).isoformat(),
            },
            timeout_seconds=_LINK_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - provenance is enrichment; the commit already happened
        logger.warning("Failed to link input receipts for session %s", session_id, exc_info=True)
        return 0
    linked = result.get("linked") if isinstance(result, dict) else None
    return len(linked) if isinstance(linked, list) else 0


async def session_input_receipts(catalogd: CatalogClient, session_id: UUID | str) -> list[dict[str, Any]]:
    """Recent receipts for a session as the API projects them, newest first."""
    try:
        result = await catalogd.call(
            "session.input.receipts.list.v2",
            {"session_id": str(session_id)},
            timeout_seconds=_LIST_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - a page without provenance still renders
        logger.warning("Failed to list input receipts for session %s", session_id, exc_info=True)
        return []
    return input_receipts_from_rows(result.get("receipts") if isinstance(result, dict) else None)


def input_receipts_from_rows(receipts: object) -> list[dict[str, Any]]:
    """Catalog receipt rows (from the list RPC or the session read) as the API projects them."""
    if not isinstance(receipts, list):
        return []
    projected: list[dict[str, Any]] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        projected.append(
            {
                "client_request_id": receipt.get("client_request_id"),
                "intent": str(receipt.get("intent") or "auto"),
                "status": str(receipt.get("status") or "queued"),
                "created_at": receipt.get("created_at"),
                "event_id": receipt.get("durable_event_id"),
            }
        )
    return projected


def input_origins_by_event(receipts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`input_origin` payloads keyed by the durable event each receipt became."""
    origins: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        event_id = receipt.get("event_id")
        if isinstance(event_id, str) and event_id and event_id not in origins:
            origins[event_id] = {
                "authored_via": "longhouse",
                "session_input_id": None,
                "client_request_id": receipt.get("client_request_id"),
            }
    return origins
