"""Compaction-boundary classification.

A small, dependency-free classifier shared by every ingest write path and the
backfill CLI. It turns a raw provider JSONL line into a structured
``compaction_kind`` so active-context projection can find boundaries without
decoding raw payloads at request time (letting raw bytes move to the archive).
"""

from __future__ import annotations

import json

# Structured marker values stored in ``events.compaction_kind``.
COMPACTION_KIND_SUMMARY = "summary"
COMPACTION_KIND_COMPACT_BOUNDARY = "compact_boundary"
COMPACTION_KIND_MICROCOMPACT_BOUNDARY = "microcompact_boundary"

_BOUNDARY_SUBTYPES = {COMPACTION_KIND_COMPACT_BOUNDARY, COMPACTION_KIND_MICROCOMPACT_BOUNDARY}


def classify_compaction_kind(raw_json: str | None) -> str | None:
    """Return the compaction-boundary kind for a raw line, or None.

    Mirrors the legacy ``_is_compaction_boundary_raw_json`` logic exactly: a
    ``type=="summary"`` line is a ``summary`` boundary; a ``type=="system"``
    line with ``subtype`` in {compact_boundary, microcompact_boundary} returns
    that subtype. Anything else returns None.
    """
    if not raw_json:
        return None
    try:
        obj = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    row_type = obj.get("type")
    if row_type == "summary":
        return COMPACTION_KIND_SUMMARY
    if row_type != "system":
        return None
    subtype = obj.get("subtype")
    if subtype in _BOUNDARY_SUBTYPES:
        return subtype
    return None
