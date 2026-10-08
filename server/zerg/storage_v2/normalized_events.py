"""Normalized-event raw records and their render projection.

Storage-v2 keeps sessions that have no provider-native bytes as
``legacy_normalized_event`` raw objects: one JSON record per parsed event,
rendered with the ``legacy-normalized-v1`` parser revision. The 2026-07 legacy
conversion wrote them for history it could not cover byte for byte, and the
demo corpus is built from them directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import MutableMapping
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.raw_json_compression import decode_raw_json
from zerg.storage_v2.render_objects import RenderRecord

PARSER_REVISION = "legacy-normalized-v1"
ORDERING_REVISION = "semantic-order-v2"
RENDER_VALUE_BYTES = 768 * 1024


def stable_uuid(*parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, "longhouse-storage-v2:" + "\x1f".join(parts))


def opaque_source_id(session_id: UUID, source_path: str, provenance: str) -> str:
    digest = hashlib.sha256(source_path.encode()).hexdigest()
    return f"legacy:{provenance}:{session_id}:{digest}"


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def optional_text(value: object) -> str | None:
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def bounded_render_text(value: object, maximum_bytes: int) -> str | None:
    if value is None:
        return None
    text_value = str(value)
    encoded = text_value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text_value
    digest = hashlib.sha256(encoded).hexdigest()
    suffix = f"\n[Longhouse legacy render truncated; bytes={len(encoded)}; sha256={digest}]".encode()
    prefix = encoded[: max(0, maximum_bytes - len(suffix))].decode("utf-8", errors="ignore").encode("utf-8")
    return (prefix + suffix).decode("utf-8")


def bounded_render_json(value: object, maximum_bytes: int) -> object | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return {"_longhouse_unrenderable_type": f"{type(value).__module__}.{type(value).__qualname__}"}
    if len(encoded) <= maximum_bytes:
        return value
    return {
        "_longhouse_truncated": True,
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def normalized_event_source(events) -> str | None:
    """One raw record for a group of parsed events (``legacy_normalized_event.v1``)."""

    if not events:
        return None
    payload = {
        "schema": "legacy_normalized_event.v1",
        "events": [
            {
                "branch_id": event.branch_id,
                "content_text": event.content_text,
                "event_id": event.id,
                "role": event.role,
                "source_offset": event.source_offset,
                "source_path": event.source_path,
                "thread_id": str(event.thread_id) if event.thread_id is not None else None,
                "timestamp": aware(event.timestamp).isoformat(),
                "tool_call_id": event.tool_call_id,
                "tool_input_json": event.tool_input_json,
                "tool_name": event.tool_name,
                "tool_output_text": event.tool_output_text,
            }
            for event in events
        ],
    }
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return None


def render_record(
    event,
    source_position: int,
    raw_record_ordinal: int,
    session_id: UUID,
    *,
    head_branch_id: int | None,
    provider: str | None = None,
    sequence_context: MutableMapping[str, Any] | None = None,
) -> RenderRecord:
    """Project one parsed event (an ``AgentEvent``-shaped object) into a render record."""

    timestamp = aware(event.timestamp)
    thread_id = str(event.thread_id or stable_uuid("thread", str(session_id), str(event.branch_id or 0)))
    interaction_kind = getattr(event, "interaction_kind", None)
    raw_json = decode_raw_json(event)
    if str(provider or "").strip().lower() == "claude" and raw_json is not None:
        # A parser-owned legacy fact may predate complete raw replay. For
        # Claude, the raw envelope is authoritative and the caller supplies a
        # session-scoped sequence context.
        interaction_kind = None
    if interaction_kind is None and provider is not None:
        interaction_kind = classify_provider_interaction(
            provider,
            role=event.role,
            content_text=event.content_text,
            raw_json=raw_json,
            sequence_context=sequence_context,
        )["interaction_kind"]
    return RenderRecord(
        event_id=f"legacy:{event.id or 0}",
        order_time_us=int(timestamp.timestamp() * 1_000_000),
        source_position=source_position,
        event_subordinal=int(event.id or 0) % (1 << 32),
        role=event.role if event.role in {"user", "assistant", "tool", "system"} else "system",
        content_text=bounded_render_text(event.content_text, RENDER_VALUE_BYTES),
        tool_name=bounded_render_text(event.tool_name, 255),
        tool_input_json=bounded_render_json(event.tool_input_json, RENDER_VALUE_BYTES),
        tool_output_text=bounded_render_text(event.tool_output_text, RENDER_VALUE_BYTES),
        tool_call_id=bounded_render_text(event.tool_call_id, 255),
        thread_id=bounded_render_text(thread_id, 255),
        branch_kind="head" if head_branch_id is None or event.branch_id in {None, head_branch_id} else "abandoned",
        raw_record_ordinal=raw_record_ordinal,
        interaction_kind=interaction_kind,
    )


def render_order_key(row: RenderRecord) -> tuple[int, int, int, str]:
    return (row.order_time_us, row.source_position, row.event_subordinal, row.event_id)


__all__ = [
    "ORDERING_REVISION",
    "PARSER_REVISION",
    "RENDER_VALUE_BYTES",
    "aware",
    "bounded_render_json",
    "bounded_render_text",
    "normalized_event_source",
    "opaque_source_id",
    "optional_text",
    "render_order_key",
    "render_record",
    "stable_uuid",
]
