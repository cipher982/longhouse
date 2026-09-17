"""Schemas shared by catalog-backed timeline readers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field

from zerg.services.session_views import MachineSearchLaneFailure
from zerg.services.session_views import SessionResponse
from zerg.utils.time import UTCBaseModel


class TimelineSessionCardResponse(UTCBaseModel):
    thread_id: str = Field(..., description="Logical thread/task root UUID")
    timeline_anchor_at: datetime | None = Field(None, description="Anchor used for timeline ordering and grouping")
    head: SessionResponse
    detail: SessionResponse
    root: SessionResponse
    continuation_count: int = Field(..., description="Concrete continuation count in this logical thread")
    started_origin_label: str | None = Field(None, description="Origin label for where the thread started")
    head_origin_label: str | None = Field(None, description="Origin label for the current writable head")


class TimelineSessionsListResponse(UTCBaseModel):
    sessions: list[TimelineSessionCardResponse]
    total: int
    has_real_sessions: bool = True
    # A search answer must say which lanes produced it. Without this, a
    # hybrid request whose dense lane failed is indistinguishable from a
    # corpus that genuinely has no paraphrase match, and the "finds by
    # meaning" label keeps making a promise the response did not keep.
    lanes: list[Literal["lexical", "dense"]] = Field(default_factory=list)
    degraded: list[MachineSearchLaneFailure] = Field(default_factory=list)


@dataclass(frozen=True)
class TimelineSessionListParams:
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
    include_automation: bool = False
    include_hidden: bool = False
