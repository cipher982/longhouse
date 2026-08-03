"""The clean transcript projection: which events carry content, and in what order.

Split out of ``embeddings.py`` so searchd can import it. Episode embeddings are
addressed by clean-message index, and anything that wants to resolve one back to
a transcript position has to reproduce this projection exactly — reimplementing
it would drift the moment sanitization changes. searchd is a deliberately lean
process, and ``embeddings.py`` pulls numpy and tiktoken, so the shared half
lives here with nothing heavier than ``content``.
"""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

from zerg.services.provider_interaction_semantics import seed_persisted_provider_interaction_context
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_event_included
from zerg.services.provider_interaction_semantics import semantic_projection_facts
from zerg.services.transcript_content import is_tool_result
from zerg.services.transcript_content import redact_secrets
from zerg.services.transcript_content import strip_noise


@dataclass(frozen=True)
class CleanTranscriptEvent:
    """A content-bearing transcript event in the same projection used for turn embeddings."""

    index: int
    event_id: int | None
    role: str
    content: str
    tool_name: str | None = None


def extract_content(
    event: Mapping[str, object],
    include_tool_calls: bool,
    tool_output_max_chars: int,
) -> str | None:
    """Extract displayable text from an event dict.

    Returns ``None`` if the event should be skipped.
    """
    # Tool-result events: skip unless caller wants them
    if is_tool_result(event) and not include_tool_calls:
        return None

    parts: list[str] = []

    content_text = event.get("content_text") or ""
    if content_text.strip():
        parts.append(content_text)

    # Append truncated tool output when present
    tool_output = event.get("tool_output_text") or ""
    if tool_output.strip() and include_tool_calls:
        truncated = tool_output[:tool_output_max_chars]
        if len(tool_output) > tool_output_max_chars:
            truncated += "..."
        parts.append(f"Tool output: {truncated}")

    combined = "\n".join(parts).strip()
    if not combined:
        return None

    return combined


def event_sort_key(event: Mapping[str, object]) -> tuple[datetime, int]:
    """Order events for the clean projection.

    Callers that already hold records in catalog order pass an integer
    ``timestamp`` and a sequential ``id``; that lands in the final branch below,
    so every event compares equal on time and the sort reduces to ``id`` —
    preserving the caller's order rather than reordering it. The embeddings
    projector relies on that, and so must anything reproducing its indices.
    """

    timestamp = event.get("timestamp")
    if isinstance(timestamp, datetime):
        ts = timestamp
    elif isinstance(timestamp, str):
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.min.replace(tzinfo=timezone.utc)
    else:
        ts = datetime.min.replace(tzinfo=timezone.utc)
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts, int(event.get("id") or 0)


def iter_clean_transcript_events(
    events: list[Mapping[str, object]],
    *,
    include_tool_calls: bool = False,
    provider: str | None = None,
) -> Iterator[CleanTranscriptEvent]:
    """Yield semantic content events in the clean index space.

    The raw event list is intentionally untouched. This is the projection used
    by embedding and search projections plus turn boundaries, so provider-local
    control rows must be removed before assigning clean indices.
    """
    ordered = sorted(events, key=event_sort_key)
    message_index = 0
    sequence_context: dict[str, object] = {}
    raw_events_by_provider: dict[str, list[object]] = {}
    for event in ordered:
        raw_json = event.get("raw_json")
        if raw_json is not None:
            event_provider = event.get("provider") or provider
            raw_events_by_provider.setdefault(str(event_provider or "unknown"), []).append(raw_json)
    event_providers = {str(event.get("provider") or provider or "unknown") for event in ordered}
    for event_provider in event_providers:
        seed_provider_interaction_sequence_context(
            event_provider,
            raw_events_by_provider.get(event_provider, []),
            sequence_context,
        )
        seed_persisted_provider_interaction_context(event_provider, ordered, sequence_context)

    for event in ordered:
        event_provider = event.get("provider") or provider
        facts = semantic_projection_facts(
            str(event_provider) if event_provider is not None else None,
            role=str(event.get("role") or ""),
            content_text=str(event.get("content_text") or ""),
            raw_json=event.get("raw_json"),
            interaction_kind=(str(event.get("interaction_kind")) if event.get("interaction_kind") is not None else None),
            title_eligible=event.get("title_eligible"),
            sequence_context=sequence_context,
        )
        if not semantic_event_included(
            str(event_provider) if event_provider is not None else None,
            role=str(event.get("role") or ""),
            content_text=str(event.get("content_text") or ""),
            raw_json=event.get("raw_json"),
            interaction_kind=facts["interaction_kind"],
            title_eligible=facts["title_eligible"],
            sequence_context=sequence_context,
        ):
            continue
        content = extract_content(event, include_tool_calls=include_tool_calls, tool_output_max_chars=500)
        if content is None:
            continue
        content = redact_secrets(strip_noise(content))
        if not content.strip():
            continue

        event_id_value = event.get("id")
        event_id = int(event_id_value) if event_id_value is not None else None
        tool_name_value = event.get("tool_name")
        yield CleanTranscriptEvent(
            index=message_index,
            event_id=event_id,
            role=str(event.get("role") or "unknown"),
            content=content,
            tool_name=str(tool_name_value) if tool_name_value else None,
        )
        message_index += 1
