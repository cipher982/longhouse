"""Shared types for session-listing use cases."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from zerg.services.session_views import SessionsListResponse


@dataclass(frozen=True)
class SessionListParams:
    project: str | None
    provider: str | None
    environment: str | None
    include_test: bool
    hide_autonomous: bool
    device_id: str | None
    days_back: int
    query: str | None
    limit: int
    offset: int
    sort: str | None
    mode: str | None
    context_mode: str


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
