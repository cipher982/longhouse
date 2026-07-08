"""Short-lived retrieval.db recall helper.

The Runtime Host can be under heavy SQLite writer pressure from the archive DB.
Running FTS reads in a fresh helper process keeps recall isolated from that
process-local SQLite state while still using the same retrieval.db index.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from zerg.services.retrieval_index import child_chunk_count
from zerg.services.retrieval_index import connect_retrieval_db_readonly
from zerg.services.retrieval_index import get_chunks_by_ids
from zerg.services.retrieval_index import resolve_retrieval_db_path
from zerg.services.retrieval_index import retrieval_schema_ready
from zerg.services.retrieval_index import search_lexical_chunks


def _bounded_context_text(value: str | None) -> str:
    if not value:
        return ""
    return value[:500] + ("..." if len(value) > 500 else "")


def _parse_context_line(line: str) -> tuple[str, str | None, str] | None:
    label, sep, content = line.partition(": ")
    if not sep:
        return None
    if ":" in label:
        role, tool_name = label.split(":", 1)
    else:
        role, tool_name = label, None
    if role not in {"user", "assistant", "tool", "system"}:
        return None
    return role, tool_name, content.replace("\\n", "\n")


def _role_for_chunk_kind(chunk_kind: str) -> str:
    if chunk_kind == "intent":
        return "user"
    if chunk_kind == "tool_result":
        return "tool"
    return "assistant"


def _context_item_from_hit(hit) -> dict[str, object]:
    return {
        "index": hit.event_index_start,
        "role": _role_for_chunk_kind(hit.chunk_kind),
        "content": _bounded_context_text(hit.content),
        "tool_name": None,
        "is_match": True,
    }


def _indexed_context_items(parent, hit, *, context_turns: int) -> list[dict[str, object]]:
    if parent is None or context_turns <= 0:
        return [_context_item_from_hit(hit)]

    lower = hit.event_index_start - context_turns
    upper = hit.event_index_end + context_turns
    items: list[dict[str, object]] = []
    for offset, line in enumerate(parent.content.splitlines()):
        event_index = parent.event_index_start + offset
        if event_index < lower or event_index > upper:
            continue
        parsed = _parse_context_line(line)
        if parsed is None:
            continue
        role, tool_name, content = parsed
        items.append(
            {
                "index": event_index,
                "role": role,
                "content": _bounded_context_text(content),
                "tool_name": tool_name,
                "is_match": hit.event_index_start <= event_index <= hit.event_index_end,
            }
        )
    return items or [_context_item_from_hit(hit)]


def _structured_hits(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split() if ":" in part][:20]


def retrieval_recall_payload(
    database_url: str,
    *,
    query: str,
    project: str | None,
    provider: str | None,
    since_days: int,
    max_results: int,
    context_turns: int,
    hide_internal_canary: bool,
) -> dict[str, Any] | None:
    retrieval_path = resolve_retrieval_db_path(database_url)
    if retrieval_path is None or not retrieval_path.exists():
        return None

    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    with connect_retrieval_db_readonly(retrieval_path) as retrieval_db:
        if not retrieval_schema_ready(retrieval_db):
            return None
        if child_chunk_count(retrieval_db) <= 0:
            return None
        hits = search_lexical_chunks(
            retrieval_db,
            query,
            project=project,
            provider=provider,
            since=since.isoformat(),
            hide_internal_canary=hide_internal_canary,
            limit=max_results,
        )
        parent_ids = [hit.parent_chunk_id for hit in hits if hit.parent_chunk_id is not None]
        parents = get_chunks_by_ids(retrieval_db, parent_ids)

    matches = []
    for hit in hits:
        parent = parents.get(hit.parent_chunk_id or -1)
        context_text = parent.content if parent is not None else hit.content
        context_start = parent.event_index_start if parent is not None else hit.event_index_start
        context_end = parent.event_index_end if parent is not None else hit.event_index_end
        matches.append(
            {
                "session_id": hit.session_id,
                "chunk_index": hit.chunk_index,
                "score": hit.score,
                "chunk_id": hit.chunk_id,
                "chunk_uid": hit.chunk_uid,
                "parent_chunk_id": hit.parent_chunk_id,
                "context_chunk_id": parent.chunk_id if parent is not None else hit.chunk_id,
                "chunk_kind": hit.chunk_kind,
                "context_text": context_text,
                "intent": hit.intent_text,
                "evidence": hit.evidence_text,
                "structured_hits": _structured_hits(hit.structured_text),
                "diagnostics": {"mode": "lexical", "source": "retrieval_db"},
                "event_index_start": hit.event_index_start,
                "event_index_end": hit.event_index_end,
                "total_events": max(0, context_end - context_start + 1),
                "context": _indexed_context_items(parent, hit, context_turns=context_turns),
                "match_event_id": hit.first_event_id,
            }
        )
    return {"matches": matches, "total": len(matches)}


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    result = retrieval_recall_payload(**payload)
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
