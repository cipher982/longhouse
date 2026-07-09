"""Machine-facing day export for Sauron worklog."""

from __future__ import annotations

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

from zerg.models.agents import AgentSession
from zerg.services.session_kernel_projection import project_session_lineage_fields
from zerg.utils.time import UTCBaseModel

WORKLOG_DAY_SOURCE = "longhouse-worklog-api-v1"

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


__all__ = [
    "WORKLOG_DAY_MESSAGE_SQL",
    "WORKLOG_DAY_SESSIONS_SQL",
    "WorklogDayExportResponse",
    "build_worklog_day_export",
    "resolve_worklog_day_window",
]
