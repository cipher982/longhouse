"""Product health checks derived from persisted session observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from zerg.schemas.observability import ProductHealthCheckSummaryResponse
from zerg.utils.time import utc_now

SESSION_TITLES_CHECK_ID = "session_titles"

_WINDOW_RE = re.compile(r"^\s*(?P<count>\d+)\s*(?P<unit>[mhd])\s*$", re.IGNORECASE)
_MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class _Window:
    label: str
    delta: timedelta


def _build_session_titles_summary(*, window: _Window, generated_at: datetime) -> ProductHealthCheckSummaryResponse:
    from zerg.services.catalog_read_gateway import CatalogReadError
    from zerg.services.catalog_read_gateway import title_dependency_health
    from zerg.services.storage_session_titles import storage_title_scheduler_snapshot
    from zerg.services.storage_session_titles import title_generation_off_reason

    titles_off = title_generation_off_reason()
    if titles_off is not None:
        return ProductHealthCheckSummaryResponse(
            check=SESSION_TITLES_CHECK_ID,
            verdict="ok",
            coverage="full",
            window=window.label,
            generated_at=generated_at,
            headline="Session titles are off here; titles fall back to the first prompt.",
            signals={"titles_off_reason": titles_off},
        )
    try:
        health = title_dependency_health()
    except CatalogReadError:
        return ProductHealthCheckSummaryResponse(
            check=SESSION_TITLES_CHECK_ID,
            verdict="unknown",
            coverage="none",
            window=window.label,
            generated_at=generated_at,
            headline="Session title dependency health is unavailable.",
        )
    dependencies = health.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        return ProductHealthCheckSummaryResponse(
            check=SESSION_TITLES_CHECK_ID,
            verdict="unknown",
            coverage="none",
            window=window.label,
            generated_at=generated_at,
            headline="Session title dependency has not reported yet.",
        )
    blocked = int(health.get("blocked_sessions") or 0)
    opened = int(health.get("open_dependencies") or 0)
    open_dependencies = [item for item in dependencies if isinstance(item, dict) and str(item.get("state") or "") != "healthy"]
    open_availability = sum(item.get("failure_class") == "availability" for item in open_dependencies)
    open_authentication = sum(item.get("failure_class") == "authentication" for item in open_dependencies)
    open_unclassified = len(open_dependencies) - open_availability - open_authentication
    terminal = int(health.get("terminal_sessions") or 0)
    pending = int(health.get("pending_sessions") or 0)
    overdue = int(health.get("overdue_sessions") or 0)
    oldest_pending_age = health.get("oldest_pending_age_seconds")
    oldest_overdue_age = health.get("oldest_overdue_age_seconds")
    scheduler = storage_title_scheduler_snapshot()
    degraded = health.get("status") == "degraded"
    if opened:
        verdict = "degraded"
        headline = f"Session title generation is degraded; {blocked} session{'' if blocked == 1 else 's'} blocked."
    elif terminal:
        verdict = "degraded"
        headline = f"Session title generation has {terminal} terminal obligation{'' if terminal == 1 else 's'}."
    elif degraded:
        verdict = "degraded"
        if overdue:
            headline = f"Session title generation is behind; {overdue} obligation{'' if overdue == 1 else 's'} overdue."
        else:
            headline = f"Session title generation is behind; {pending} obligation{'' if pending == 1 else 's'} pending."
    else:
        verdict = "ok"
        headline = "Session title generation dependency is healthy."
    return ProductHealthCheckSummaryResponse(
        check=SESSION_TITLES_CHECK_ID,
        verdict=verdict,
        coverage="full",
        window=window.label,
        generated_at=generated_at,
        headline=headline,
        signals={
            "open_dependencies": opened,
            "open_availability_dependencies": open_availability,
            "open_authentication_dependencies": open_authentication,
            "open_unclassified_dependencies": open_unclassified,
            "blocked_sessions": blocked,
            "pending_sessions": pending,
            "overdue_sessions": overdue,
            "terminal_sessions": terminal,
            "terminal_shared_failure_sessions": int(health.get("terminal_shared_failure_sessions") or 0),
            "oldest_pending_age_seconds": int(oldest_pending_age) if oldest_pending_age is not None else None,
            "oldest_overdue_age_seconds": int(oldest_overdue_age) if oldest_overdue_age is not None else None,
            "backlog_degraded_after_seconds": int(health.get("backlog_degraded_after_seconds") or 0),
            **scheduler,
        },
    )


def build_session_title_health_check(*, window: str = "15m") -> ProductHealthCheckSummaryResponse:
    """Build the title dependency check without touching retired archive rows."""

    resolved_window = _parse_window(window)
    generated_at = utc_now()
    return _build_session_titles_summary(window=resolved_window, generated_at=generated_at)


def _parse_window(value: str) -> _Window:
    match = _WINDOW_RE.match(value or "")
    if not match:
        raise ValueError("Window must look like 15m, 1h, or 7d.")
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    seconds_by_unit = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
    seconds = count * seconds_by_unit[unit]
    if seconds <= 0 or seconds > _MAX_WINDOW_SECONDS:
        raise ValueError("Window must be between 1 minute and 7 days.")
    return _Window(label=f"{count}{unit}", delta=timedelta(seconds=seconds))
