"""Ops metrics aggregation service."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.models import User as UserModel


def _window_start_date(today: date, window: str) -> date:
    if window == "today":
        return today
    if window == "7d":
        return today - timedelta(days=6)
    if window == "30d":
        return today - timedelta(days=29)
    raise ValueError("Unsupported window")


def _window_label(window: str) -> str:
    if window == "today":
        return "Today"
    if window == "7d":
        return "Last 7 Days"
    if window == "30d":
        return "Last 30 Days"
    raise ValueError("Unsupported window")


def _today_date_utc() -> date:
    return datetime.now(timezone.utc).date()


def get_summary(db: Session, current_user: UserModel, window: str = "today") -> dict[str, Any]:
    """Compute Ops summary for launch-era data.

    Automation/run KPIs were backed by the retired pre-launch tables and now
    intentionally read as empty until session-native usage accounting exists.
    """
    _ = (db, current_user)
    today = _today_date_utc()
    _window_start_date(today, window)
    settings = get_settings()
    user_budget_cents = int(getattr(settings, "daily_cost_per_user_cents", 0) or 0)
    global_budget_cents = int(getattr(settings, "daily_cost_global_cents", 0) or 0)

    return {
        "window": window,
        "window_label": _window_label(window),
        "runs": 0,
        "cost_usd": None,
        "budget_user": {"limit_cents": user_budget_cents, "used_usd": 0.0, "percent": 0.0 if user_budget_cents > 0 else None},
        "budget_global": {"limit_cents": global_budget_cents, "used_usd": 0.0, "percent": 0.0 if global_budget_cents > 0 else None},
        "active_users_24h": 0,
        "automations_total": 0,
        "automations_scheduled": 0,
        "latency_ms": {"p50": 0, "p95": 0},
        "errors_last_hour": 0,
        "top_automations": [],
    }


def get_timeseries(db: Session, metric: str, window: str = "today") -> list[dict[str, Any]]:
    """Return zero-filled time-series for retired automation metrics."""
    _ = db
    today = _today_date_utc()

    if window == "today":
        if metric not in {"runs_by_hour", "errors_by_hour", "cost_by_hour"}:
            raise ValueError("Unsupported metric for window=today")
        return [{"hour_iso": f"{h:02d}:00Z", "value": 0.0} for h in range(24)]

    if window not in {"7d", "30d"}:
        raise ValueError("Unsupported window")
    if metric not in {"runs_by_day", "errors_by_day", "cost_by_day"}:
        raise ValueError("Unsupported metric for daily window")

    days = 7 if window == "7d" else 30
    start_date = today - timedelta(days=days - 1)
    return [{"hour_iso": (start_date + timedelta(days=i)).isoformat(), "value": 0.0} for i in range(days)]


def get_top_automations(db: Session, window: str = "today", limit: int = 5) -> list[dict[str, Any]]:
    """Return no automation rows because the automation data plane is retired."""
    _ = (db, limit)
    _window_start_date(_today_date_utc(), window)
    return []
