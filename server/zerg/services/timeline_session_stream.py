"""Browser timeline session SSE stream use case."""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone
from typing import Protocol

from zerg.services.session_listing import SessionListingError

logger = logging.getLogger(__name__)

# Pubsub wakes active tabs immediately. This timeout is only the fallback poll
# for missed cross-worker wakes, so keep it slow enough that idle tabs do not
# continuously compete with machine ingest for SQLite connections.
TIMELINE_STREAM_CHANGE_WAIT_SECONDS = 5.0
TIMELINE_STREAM_HEARTBEAT_SECONDS = 30.0

TimelineWindowSignature = tuple[
    tuple[str, str, datetime | None, datetime | None, datetime | None, int, datetime | None],
    ...,
]


class TimelineStreamRequest(Protocol):
    async def is_disconnected(self) -> bool: ...


def validate_timeline_stream_contract(*, query: str | None, sort: str | None, mode: str | None) -> None:
    if _stream_supports_preflight(query=query, sort=sort, mode=mode):
        return
    raise SessionListingError(
        400,
        "Timeline session stream only supports the default no-query lexical recency contract.",
    )


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _effective_stream_sort(query: str | None, sort: str | None) -> str:
    return sort or ("relevance" if query else "recency")


def _stream_supports_preflight(*, query: str | None, sort: str | None, mode: str | None) -> bool:
    effective_sort = _effective_stream_sort(query, sort)
    return query is None and mode in (None, "lexical") and effective_sort == "recency"
