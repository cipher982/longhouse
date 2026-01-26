"""Trace coverage reporting for observability regression checks."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.course_event import CourseEvent
from zerg.models.llm_audit import LLMAuditLog
from zerg.models.models import CommisJob
from zerg.models.models import Course


@dataclass
class CoverageBucket:
    """Coverage bucket for a single category."""

    name: str
    total: int
    with_trace: int
    pct: float
    empty: bool


def _bucket(name: str, total: int, with_trace: int) -> CoverageBucket:
    empty = total == 0
    pct = 100.0 if empty else round((with_trace / total) * 100.0, 2)
    return CoverageBucket(name=name, total=total, with_trace=with_trace, pct=pct, empty=empty)


def _apply_since(query, column, since: datetime | None):
    if since is None:
        return query
    return query.filter(column.isnot(None)).filter(column >= since)


def _event_breakdown(rows: list[tuple[str, dict | None]]) -> tuple[int, int, list[CoverageBucket]]:
    totals: dict[str, dict[str, int]] = {}
    total_events = 0
    with_trace = 0

    for event_type, payload in rows:
        total_events += 1
        if event_type not in totals:
            totals[event_type] = {"total": 0, "with_trace": 0}
        totals[event_type]["total"] += 1

        has_trace = bool(payload and payload.get("trace_id"))
        if has_trace:
            with_trace += 1
            totals[event_type]["with_trace"] += 1

    buckets = [_bucket(name, counts["total"], counts["with_trace"]) for name, counts in totals.items()]
    buckets.sort(key=lambda b: b.name)

    return total_events, with_trace, buckets


def build_trace_coverage_report(db: Session, since: datetime | None = None) -> dict[str, Any]:
    """Build a trace coverage report across core observability tables."""
    now = datetime.now(timezone.utc)

    courses_query = _apply_since(db.query(Course), Course.created_at, since)
    courses_total = courses_query.count()
    courses_with_trace = courses_query.filter(Course.trace_id.isnot(None)).count()

    jobs_query = _apply_since(db.query(CommisJob), CommisJob.created_at, since)
    jobs_total = jobs_query.count()
    jobs_with_trace = jobs_query.filter(CommisJob.trace_id.isnot(None)).count()

    audit_query = _apply_since(db.query(LLMAuditLog), LLMAuditLog.created_at, since)
    audit_total = audit_query.count()
    audit_with_trace = audit_query.filter(LLMAuditLog.trace_id.isnot(None)).count()

    events_query = _apply_since(db.query(CourseEvent), CourseEvent.created_at, since)
    event_rows = events_query.with_entities(CourseEvent.event_type, CourseEvent.payload).all()
    events_total, events_with_trace, event_buckets = _event_breakdown(event_rows)

    buckets = [
        _bucket("courses", courses_total, courses_with_trace),
        _bucket("commis_jobs", jobs_total, jobs_with_trace),
        _bucket("llm_audit_log", audit_total, audit_with_trace),
        _bucket("course_events", events_total, events_with_trace),
    ]

    return {
        "generated_at": now.isoformat(),
        "since": since.isoformat() if since else None,
        "buckets": [asdict(bucket) for bucket in buckets],
        "event_types": [asdict(bucket) for bucket in event_buckets],
    }


__all__ = ["build_trace_coverage_report"]
