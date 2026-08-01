"""Browser timeline session-listing use case."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import ContextManager

from pydantic import Field
from sqlalchemy.orm import Session

from zerg.services.agents import AgentsStore
from zerg.services.session_listing import SessionListingError
from zerg.services.session_listing import SessionListParams
from zerg.services.session_listing import list_agent_sessions
from zerg.services.session_response_projection import build_session_response_map
from zerg.services.session_response_projection import has_real_sessions
from zerg.services.session_views import SessionResponse
from zerg.services.session_views import SessionsListResponse
from zerg.utils.server_timing import ServerTimingRecorder
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)


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

    def to_agent_params(self) -> SessionListParams:
        return SessionListParams(
            project=self.project,
            provider=self.provider,
            environment=self.environment,
            include_test=self.include_test,
            hide_autonomous=self.hide_autonomous,
            include_automation=self.include_automation,
            device_id=self.device_id,
            days_back=self.days_back,
            query=self.query,
            limit=self.limit,
            offset=self.offset,
            sort=self.sort,
            mode=self.mode,
            context_mode=self.context_mode,
        )


@dataclass(frozen=True)
class TimelineSessionListResult:
    response: TimelineSessionsListResponse | SessionsListResponse
    headers: dict[str, str] = field(default_factory=dict)
    compatibility_raw: bool = False


async def list_timeline_sessions_for_browser(
    *,
    db: Session,
    params: TimelineSessionListParams,
    timing: ServerTimingRecorder | None = None,
    owner_id: int | None = None,
) -> TimelineSessionListResult:
    effective_mode = params.mode or "lexical"
    if params.query is not None or effective_mode != "lexical":
        # COMPATIBILITY: Query-driven and hybrid search return raw SessionResponse[]
        # because thread-aware search ranking/paging hasn't been built yet.
        # The frontend reshapes these into TimelineSessionCards client-side via
        # buildCompatibilityTimelineCards(). This is the only remaining non-thread
        # path on the timeline read surface.
        try:
            with _timing_span(timing, "compat_delegate"):
                raw_result = await list_agent_sessions(
                    db=db,
                    auth=None,
                    params=params.to_agent_params(),
                    owner_id=owner_id,
                )
        except SessionListingError:
            raise
        except Exception as exc:
            logger.exception("Failed to list sessions")
            raise SessionListingError(500, "Failed to list sessions") from exc
        return TimelineSessionListResult(
            response=raw_result.response,
            headers=raw_result.headers,
            compatibility_raw=True,
        )

    return await asyncio.to_thread(
        _list_timeline_sessions_for_browser_sync,
        db=db,
        params=params,
        timing=timing,
        owner_id=owner_id,
    )


def _list_timeline_sessions_for_browser_sync(
    *,
    db: Session,
    params: TimelineSessionListParams,
    timing: ServerTimingRecorder | None,
    owner_id: int | None,
) -> TimelineSessionListResult:
    store = AgentsStore(db)
    since = datetime.now(timezone.utc) - timedelta(days=params.days_back)
    with _timing_span(timing, "list_threads"):
        total, thread_rows = store.list_timeline_thread_page(
            project=params.project,
            provider=params.provider,
            environment=params.environment,
            include_test=params.include_test,
            device_id=params.device_id,
            since=since,
            query=params.query,
            limit=params.limit,
            offset=params.offset,
            hide_autonomous=params.hide_autonomous,
            include_automation=params.include_automation,
            context_mode=params.context_mode,
        )
    with _timing_span(timing, "unread_union"):
        # Unread Console results must survive days_back and pagination: they
        # stay listed until read or archived, so union any missing ones into
        # the page (docs/specs/console-unread-acknowledgement.md).
        unread_rows = _unread_thread_rows(db, params=params, exclude=thread_rows)
        if unread_rows:
            thread_rows = unread_rows + thread_rows
            total += len(unread_rows)
    with _timing_span(timing, "build_cards"):
        sessions = build_timeline_cards_from_thread_rows(db=db, thread_rows=thread_rows, owner_id=owner_id)
    with _timing_span(timing, "has_real"):
        has_real_sessions_value = has_real_sessions(db, default_when_empty=total == 0)

    return TimelineSessionListResult(
        response=TimelineSessionsListResponse(
            sessions=sessions,
            total=total,
            has_real_sessions=has_real_sessions_value,
        )
    )


def _unread_thread_rows(
    db: Session,
    *,
    params: TimelineSessionListParams,
    exclude: tuple[tuple[str, str, datetime | None], ...],
) -> tuple[tuple[str, str, datetime | None], ...]:
    from sqlalchemy import or_

    from zerg.models.agents import AgentSession

    query = db.query(AgentSession).filter(
        AgentSession.last_console_result_at.isnot(None),
        or_(
            AgentSession.last_read_at.is_(None),
            AgentSession.last_console_result_at > AgentSession.last_read_at,
        ),
        AgentSession.user_state.notin_(("archived", "snoozed")),
    )
    if params.project is not None:
        query = query.filter(AgentSession.project == params.project)
    if params.provider is not None:
        query = query.filter(AgentSession.provider == params.provider)
    if params.environment is not None:
        query = query.filter(AgentSession.environment == params.environment)
    elif not params.include_test:
        query = query.filter(AgentSession.environment.notin_(("test", "e2e")))
    if params.device_id is not None:
        query = query.filter(AgentSession.device_id == params.device_id)

    seen_sessions = {session_id for _thread_id, session_id, _anchor in exclude}
    seen_threads = {thread_id for thread_id, _session_id, _anchor in exclude}
    rows: list[tuple[str, str, datetime | None]] = []
    for session in query.order_by(AgentSession.last_console_result_at.desc()).limit(100).all():
        session_id = str(session.id)
        thread_id = str(session.primary_thread_id or session.id)
        if session_id in seen_sessions or thread_id in seen_threads:
            continue
        rows.append((thread_id, session_id, session.last_console_result_at))
    return tuple(rows)


def build_timeline_cards_from_thread_rows(
    *,
    db: Session,
    thread_rows: tuple[tuple[str, str, datetime | None], ...],
    owner_id: int | None = None,
) -> list[TimelineSessionCardResponse]:
    if not thread_rows:
        return []

    representative_ids = [session_id for _thread_id, session_id, _thread_anchor in thread_rows]
    response_map = build_session_response_map(
        db=db,
        session_ids=representative_ids,
        owner_id=owner_id,
    )
    representative_rows = []
    for thread_id, session_id, thread_anchor in thread_rows:
        representative_rows.append((thread_id, response_map.get(session_id), thread_anchor))

    root_ids: set[str] = set()
    head_ids: set[str] = set()
    for _thread_id, detail, _thread_anchor in representative_rows:
        if detail is None:
            continue
        root_ids.add(detail.thread_root_session_id)
        head_ids.add(detail.thread_head_session_id)
    supplemental_ids = sorted((root_ids | head_ids) - response_map.keys())
    response_map.update(
        build_session_response_map(
            db=db,
            session_ids=supplemental_ids,
            owner_id=owner_id,
        )
    )

    cards: list[TimelineSessionCardResponse] = []
    for thread_id, representative, thread_anchor in representative_rows:
        if representative is None:
            continue
        head = response_map.get(representative.thread_head_session_id, representative)
        root = response_map.get(representative.thread_root_session_id, representative)
        cards.append(
            TimelineSessionCardResponse(
                thread_id=thread_id,
                timeline_anchor_at=(
                    thread_anchor
                    or representative.timeline_anchor_at
                    or head.timeline_anchor_at
                    or representative.last_activity_at
                    or head.last_activity_at
                    or head.started_at
                ),
                head=head,
                detail=head,
                root=root,
                continuation_count=head.thread_continuation_count or representative.thread_continuation_count or 1,
                started_origin_label=root.origin_label or root.environment,
                head_origin_label=head.origin_label or head.environment,
            )
        )
    return cards


def _timing_span(timing: ServerTimingRecorder | None, name: str) -> ContextManager[None]:
    if timing is None:
        return nullcontext()
    return timing.span(name)
