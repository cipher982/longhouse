"""Machine-facing day export for Sauron worklog."""

from __future__ import annotations

from contextlib import suppress
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.models.agents import AgentSession
from zerg.services.session_kernel_projection import project_session_lineage_fields
from zerg.utils.time import UTCBaseModel

WORKLOG_DAY_SOURCE = "longhouse-worklog-api-v1"
WORKLOG_DAY_V2_SOURCE = "longhouse-worklog-search-v2"
_WORKLOG_RPC_PAGE_SIZE = 500
_WORKLOG_MAX_PAGES = 200
_WORKLOG_RPC_TIMEOUT_SECONDS = 5.0

WORKLOG_DAY_SESSIONS_SQL = """
WITH active_sessions AS (
    SELECT
        e.session_id,
        MIN(e.timestamp) AS first_event_at,
        MAX(e.timestamp) AS last_event_at,
        MIN(CASE
            WHEN e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
            THEN e.timestamp
        END) AS first_message_at,
        SUM(CASE
            WHEN e.role IN ('user', 'assistant') AND e.content_text IS NOT NULL
            THEN 1 ELSE 0
        END) AS message_count,
        COUNT(*) AS event_count
    FROM events AS e INDEXED BY ix_events_timestamp
    WHERE e.timestamp >= :window_start_utc AND e.timestamp < :window_end_utc
    GROUP BY e.session_id
)
SELECT
    s.id,
    s.project,
    s.provider,
    s.git_repo,
    s.cwd,
    s.started_at,
    s.user_messages,
    s.assistant_messages,
    s.tool_calls,
    active.first_event_at,
    active.last_event_at,
    active.first_message_at,
    active.message_count,
    active.event_count
FROM active_sessions active
JOIN sessions s ON s.id = active.session_id
WHERE (:include_test = 1 OR s.environment NOT IN ('test', 'e2e'))
ORDER BY COALESCE(active.first_message_at, active.first_event_at), s.started_at, s.id
"""

WORKLOG_DAY_MESSAGE_SQL = """
SELECT
    e.session_id,
    e.role,
    e.content_text,
    e.timestamp
FROM events AS e INDEXED BY ix_events_timestamp
JOIN sessions s ON s.id = e.session_id
WHERE e.timestamp >= :window_start_utc AND e.timestamp < :window_end_utc
  AND e.role IN ('user', 'assistant')
  AND e.content_text IS NOT NULL
  AND (:include_test = 1 OR s.environment NOT IN ('test', 'e2e'))
ORDER BY e.session_id, e.timestamp, e.id
"""


class WorklogDaySession(UTCBaseModel):
    id: str
    project: str | None = None
    provider: str
    git_repo: str | None = None
    cwd: str | None = None
    started_at: datetime
    user_messages: int
    assistant_messages: int
    tool_calls: int
    is_sidechain: bool = False
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    first_message_at: datetime | None = None
    message_count: int = 0
    event_count: int = 0


class WorklogDayEvent(UTCBaseModel):
    session_id: str
    role: str
    content_text: str
    timestamp: datetime


class WorklogDayStats(BaseModel):
    session_count: int = Field(..., ge=0)
    message_count: int = Field(..., ge=0)
    event_count: int = Field(..., ge=0)


class WorklogDayExportResponse(UTCBaseModel):
    date: str
    timezone: str
    window_start: str
    window_end: str
    source: str
    sessions: list[WorklogDaySession]
    events: list[WorklogDayEvent]
    stats: WorklogDayStats
    projection_lag: bool = False
    indexed_through: str | None = None
    desired_through: str | None = None


class WorklogV2Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_worklog_day_window(day: date, timezone_name: str) -> tuple[datetime, datetime, ZoneInfo]:
    """Return the half-open local day window for an IANA timezone."""
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    window_start = datetime.combine(day, time.min, tzinfo=tz)
    window_end = window_start + timedelta(days=1)
    return window_start, window_end, tz


def _utc_sql_param(value: datetime) -> str:
    """Format a timestamp to match SQLite's UTC-naive storage convention."""
    return value.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text_value = str(value)
        dt = datetime.fromisoformat(text_value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _clean_text(value: object) -> str:
    text_value = "" if value is None else str(value)
    return text_value.encode("utf-8", "replace").decode("utf-8")


def _sidechain_map(db: Session, session_ids: list[str]) -> dict[str, bool]:
    if not session_ids:
        return {}
    rows = db.query(AgentSession).filter(AgentSession.id.in_(session_ids)).all()
    out: dict[str, bool] = {}
    for session in rows:
        out[str(session.id)] = bool(project_session_lineage_fields(db, session).is_sidechain)
    return out


def build_worklog_day_export(
    db: Session,
    *,
    day: date,
    timezone_name: str = "America/New_York",
    include_test: bool = False,
) -> WorklogDayExportResponse:
    """Build one worklog day payload from canonical Longhouse session rows."""
    window_start, window_end, _tz = resolve_worklog_day_window(day, timezone_name)
    params = {
        "window_start_utc": _utc_sql_param(window_start),
        "window_end_utc": _utc_sql_param(window_end),
        "include_test": 1 if include_test else 0,
    }
    session_rows = list(db.execute(text(WORKLOG_DAY_SESSIONS_SQL), params).mappings().all())
    event_rows = list(db.execute(text(WORKLOG_DAY_MESSAGE_SQL), params).mappings().all())
    sidechains = _sidechain_map(db, [str(row["id"]) for row in session_rows])

    sessions = [
        WorklogDaySession(
            id=str(row["id"]),
            project=row["project"],
            provider=str(row["provider"] or "unknown"),
            git_repo=row["git_repo"],
            cwd=row["cwd"],
            started_at=_coerce_datetime(row["started_at"]) or window_start.astimezone(timezone.utc),
            user_messages=int(row["user_messages"] or 0),
            assistant_messages=int(row["assistant_messages"] or 0),
            tool_calls=int(row["tool_calls"] or 0),
            is_sidechain=bool(sidechains.get(str(row["id"]), False)),
            first_event_at=_coerce_datetime(row["first_event_at"]),
            last_event_at=_coerce_datetime(row["last_event_at"]),
            first_message_at=_coerce_datetime(row["first_message_at"]),
            message_count=int(row["message_count"] or 0),
            event_count=int(row["event_count"] or 0),
        )
        for row in session_rows
    ]
    events = [
        WorklogDayEvent(
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content_text=_clean_text(row["content_text"]),
            timestamp=_coerce_datetime(row["timestamp"]) or window_start.astimezone(timezone.utc),
        )
        for row in event_rows
    ]

    return WorklogDayExportResponse(
        date=day.isoformat(),
        timezone=timezone_name,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        source=WORKLOG_DAY_SOURCE,
        sessions=sessions,
        events=events,
        stats=WorklogDayStats(
            session_count=len(sessions),
            message_count=len(events),
            event_count=sum(session.event_count for session in sessions),
        ),
    )


async def build_worklog_day_export_v2(
    *,
    catalog: CatalogClient,
    search: CatalogClient,
    owner_id: str,
    day: date,
    timezone_name: str,
    include_test: bool,
) -> WorklogDayExportResponse:
    """Build a day export only from the derived v2 message projection."""

    window_start, window_end, _tz = resolve_worklog_day_window(day, timezone_name)
    window_start_us = _datetime_to_us(window_start)
    window_end_us = _datetime_to_us(window_end)
    lag = await catalog.call(
        "projector.state.list_lag.v2",
        {"projector": "search-v2", "after_session_id": None, "limit": 1},
    )
    desired_through = _revision_text(lag.get("commit_seq"), "commit_seq")
    indexed_through = _revision_text(lag.get("indexed_through"), "indexed_through")
    lag_count = lag.get("lag_count")
    if type(lag_count) is not int or lag_count < 0:
        raise WorklogV2Error("invalid_projection_status", "catalog returned an invalid search projection status")

    snapshot_id: str | None = None
    try:
        session_rows, snapshot_id = await _collect_worklog_pages(
            search,
            owner_id=owner_id,
            window_start_us=window_start_us,
            window_end_us=window_end_us,
            include_test=include_test,
            section="sessions",
            snapshot_id=None,
        )
        event_rows, _snapshot_id = await _collect_worklog_pages(
            search,
            owner_id=owner_id,
            window_start_us=window_start_us,
            window_end_us=window_end_us,
            include_test=include_test,
            section="events",
            snapshot_id=snapshot_id,
        )
    finally:
        if snapshot_id is not None:
            with suppress(Exception):
                await search.call(
                    "worklog.snapshot.release.v2",
                    {"snapshot_id": snapshot_id, "owner_id": owner_id},
                )
    sessions = [
        WorklogDaySession(
            id=str(row["session_id"]),
            project=row.get("project"),
            provider=str(row.get("provider") or "unknown"),
            git_repo=row.get("git_repo"),
            cwd=row.get("cwd"),
            started_at=_coerce_datetime(row.get("started_at")) or window_start.astimezone(timezone.utc),
            user_messages=int(row.get("user_messages") or 0),
            assistant_messages=int(row.get("assistant_messages") or 0),
            tool_calls=int(row.get("tool_calls") or 0),
            is_sidechain=bool(row.get("is_sidechain")),
            first_event_at=_datetime_from_us(row.get("first_event_us")),
            last_event_at=_datetime_from_us(row.get("last_event_us")),
            first_message_at=_datetime_from_us(row.get("first_message_us")),
            message_count=int(row.get("message_count") or 0),
            event_count=int(row.get("day_event_count") or 0),
        )
        for row in session_rows
    ]
    events = [
        WorklogDayEvent(
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content_text=_clean_text(row.get("content_text")),
            timestamp=_datetime_from_us(row.get("order_time_us")) or window_start.astimezone(timezone.utc),
        )
        for row in event_rows
    ]
    return WorklogDayExportResponse(
        date=day.isoformat(),
        timezone=timezone_name,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        source=WORKLOG_DAY_V2_SOURCE,
        sessions=sessions,
        events=events,
        stats=WorklogDayStats(
            session_count=len(sessions),
            message_count=len(events),
            event_count=sum(session.event_count for session in sessions),
        ),
        projection_lag=lag_count > 0,
        indexed_through=indexed_through,
        desired_through=desired_through,
    )


async def _collect_worklog_pages(
    search: CatalogClient,
    *,
    owner_id: str,
    window_start_us: int,
    window_end_us: int,
    include_test: bool,
    section: str,
    snapshot_id: str | None,
) -> tuple[list[dict], str]:
    offset = 0
    rows: list[dict] = []
    for _page in range(_WORKLOG_MAX_PAGES):
        result = await search.call(
            "worklog.day.v2",
            {
                "owner_id": owner_id,
                "window_start_us": window_start_us,
                "window_end_us": window_end_us,
                "include_test": include_test,
                "section": section,
                "snapshot_id": snapshot_id,
                "offset": offset,
                "limit": _WORKLOG_RPC_PAGE_SIZE,
            },
            timeout_seconds=_WORKLOG_RPC_TIMEOUT_SECONDS,
        )
        result_snapshot_id = result.get("snapshot_id")
        if not isinstance(result_snapshot_id, str) or (snapshot_id is not None and result_snapshot_id != snapshot_id):
            raise WorklogV2Error("invalid_search_response", "searchd changed the worklog snapshot identity")
        snapshot_id = result_snapshot_id
        items = result.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise WorklogV2Error("invalid_search_response", "searchd returned an invalid worklog page")
        rows.extend(items)
        if result.get("has_more") is not True:
            return rows, snapshot_id
        next_offset = result.get("next_offset")
        if type(next_offset) is not int or next_offset <= offset or not items:
            raise WorklogV2Error("invalid_search_response", "searchd returned a non-advancing worklog offset")
        offset = next_offset
    raise WorklogV2Error("export_too_large", "worklog export exceeds the bounded compatibility response")


def _revision_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.isdecimal():
        raise WorklogV2Error("invalid_projection_status", f"catalog returned invalid {field}")
    return value


def _datetime_to_us(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _datetime_from_us(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not int:
        raise WorklogV2Error("invalid_search_response", "searchd returned an invalid worklog timestamp")
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)


__all__ = [
    "WORKLOG_DAY_MESSAGE_SQL",
    "WORKLOG_DAY_SESSIONS_SQL",
    "WorklogDayExportResponse",
    "WorklogV2Error",
    "build_worklog_day_export",
    "build_worklog_day_export_v2",
    "resolve_worklog_day_window",
]
