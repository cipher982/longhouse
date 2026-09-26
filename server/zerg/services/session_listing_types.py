"""Shared types for session-listing use cases."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from zerg.services.session_views import SessionsListResponse

# The window a query-less listing falls back to when the caller names no
# explicit range. Search (a query is present) has no such fallback: an
# unnamed range means "all indexed history", not this window. See
# `resolve_search_days_back`.
DEFAULT_LIST_DAYS_BACK = 14


def resolve_search_days_back(explicit: int | None, *, has_query: bool) -> int | None:
    """Turn a possibly-absent `days_back` into the window a read should use.

    An explicit value always wins and narrows the read, search or listing
    alike. Absent a value: a listing (no query) keeps the product's existing
    recent-activity default, unchanged by this function's addition; a search
    (a query is present) returns ``None``, which every read path down to
    searchd's archive FTS and the resident dense index already treats as
    "no lower bound" rather than something that must be widened by hand.
    """
    if explicit is not None:
        return explicit
    return None if has_query else DEFAULT_LIST_DAYS_BACK


@dataclass(frozen=True)
class SessionListParams:
    project: str | None
    provider: str | None
    environment: str | None
    include_test: bool
    hide_autonomous: bool
    device_id: str | None
    days_back: int | None
    query: str | None
    limit: int
    offset: int
    sort: str | None
    mode: str | None
    context_mode: str
    include_automation: bool = False
    include_hidden: bool = False


@dataclass(frozen=True)
class SessionListResult:
    response: SessionsListResponse
    headers: dict[str, str] = field(default_factory=dict)


class SessionListingError(Exception):
    """Expected session-listing failure that maps cleanly to an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
