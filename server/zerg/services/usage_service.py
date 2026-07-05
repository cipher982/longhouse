"""LLM usage aggregation service.

The retired automation-run data plane no longer backs usage accounting.
Launch usage endpoints intentionally return empty counters until the session
runtime has a first-class usage ledger.
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Literal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.schemas.usage import TokenBreakdown
from zerg.schemas.usage import UsageLimit
from zerg.schemas.usage import UserUsageResponse
from zerg.utils.time import utc_now_naive


def _today_utc_date() -> date:
    """Return today's date in UTC (as a date)."""
    return utc_now_naive().date()


def _utc_day_start(day: date) -> datetime:
    """Return naive UTC datetime at start of a given day."""
    return datetime(day.year, day.month, day.day)


def _period_start_date(period: Literal["today", "7d", "30d"]) -> datetime.date:
    """Return the start date for the given period."""
    today = _today_utc_date()
    if period == "today":
        return today
    elif period == "7d":
        return today - timedelta(days=6)  # Include today
    elif period == "30d":
        return today - timedelta(days=29)  # Include today
    else:
        raise ValueError(f"Invalid period: {period}")


def _usage_range_for_period(period: Literal["today", "7d", "30d"]) -> tuple[datetime, datetime]:
    """Return (start_dt, end_dt) for a period in naive UTC.

    We use an exclusive end bound at *tomorrow 00:00 UTC* so "today/7d/30d"
    are inclusive of the current day.
    """
    today = _today_utc_date()
    start_date = _period_start_date(period)
    start_dt = _utc_day_start(start_date)
    end_dt = _utc_day_start(today + timedelta(days=1))
    return start_dt, end_dt


def _get_user_usage_range(db: Session, user_id: int, start_dt: datetime, end_dt: datetime) -> dict:
    """Aggregate token/cost/run stats for a user within [start_dt, end_dt)."""
    _ = (db, user_id, start_dt, end_dt)
    return {"tokens": 0, "cost_usd": 0.0, "runs": 0}


def get_user_usage(
    db: Session,
    user_id: int,
    period: Literal["today", "7d", "30d"] = "today",
) -> UserUsageResponse:
    """Get LLM usage stats for a user.

    Args:
        db: Database session
        user_id: ID of the user
        period: Time period - "today", "7d", or "30d"

    Returns:
        UserUsageResponse with token/cost stats and limit info
    """
    start_dt, end_dt = _usage_range_for_period(period)
    period_usage = _get_user_usage_range(db, user_id, start_dt, end_dt)

    # Get today's cost for limit calculation (limits are always daily)
    today_start, tomorrow_start = _usage_range_for_period("today")
    today_cost_usd = _get_user_usage_range(db, user_id, today_start, tomorrow_start)["cost_usd"]

    # Calculate limit status
    settings = get_settings()
    daily_limit_cents = int(getattr(settings, "daily_cost_per_user_cents", 0) or 0)

    if daily_limit_cents <= 0:
        # No limit configured
        limit = UsageLimit(
            daily_cost_cents=0,
            used_percent=0.0,
            remaining_usd=0.0,
            status="unlimited",
        )
    else:
        daily_limit_usd = daily_limit_cents / 100.0
        used_percent = (today_cost_usd / daily_limit_usd) * 100.0 if daily_limit_usd > 0 else 0.0
        remaining_usd = max(0.0, daily_limit_usd - today_cost_usd)

        if used_percent >= 100.0:
            status = "exceeded"
        elif used_percent >= 80.0:
            status = "warning"
        else:
            status = "ok"

        limit = UsageLimit(
            daily_cost_cents=daily_limit_cents,
            used_percent=round(used_percent, 1),
            remaining_usd=round(remaining_usd, 4),
            status=status,
        )

    # Token breakdown (prompt/completion) - we don't currently track these
    # separately on Run, so they'll be None
    tokens = TokenBreakdown(
        prompt=None,  # Not tracked separately yet
        completion=None,  # Not tracked separately yet
        total=period_usage["tokens"],
    )

    return UserUsageResponse(
        period=period,
        tokens=tokens,
        cost_usd=period_usage["cost_usd"],
        runs=period_usage["runs"],
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Admin functions (Phase 2)
# ---------------------------------------------------------------------------


def get_all_users_usage(
    db: Session,
    *,
    sort: Literal["cost_today", "cost_7d", "cost_30d", "email", "created_at"] = "cost_today",
    order: Literal["asc", "desc"] = "desc",
    limit: int = 50,
    offset: int = 0,
    active: Optional[bool] = None,
) -> dict:
    """Get all users with their usage stats (admin-only).

    Args:
        db: Database session
        sort: Sort field - "cost_today", "cost_7d", "cost_30d", "email", "created_at"
        order: Sort order - "asc" or "desc"
        limit: Max results to return
        offset: Pagination offset

    Returns:
        Dict with users list, total count, limit, offset
    """
    from zerg.models.models import User as UserModel

    user_filters = []
    if active is True:
        user_filters.append(UserModel.is_active.is_(True))
    elif active is False:
        user_filters.append(UserModel.is_active.is_(False))

    total = int(db.query(func.count(UserModel.id)).filter(*user_filters).scalar() or 0)

    query = db.query(UserModel).filter(*user_filters)

    sort_expr_map = {
        "cost_today": UserModel.id,
        "cost_7d": UserModel.id,
        "cost_30d": UserModel.id,
        "email": func.lower(UserModel.email),
        "created_at": UserModel.created_at,
    }
    sort_expr = sort_expr_map[sort]
    query = query.order_by(sort_expr.desc() if order == "desc" else sort_expr.asc())

    rows = query.limit(limit).offset(offset).all()

    users = []
    for row in rows:
        users.append(
            {
                "id": row.id,
                "email": row.email,
                "display_name": row.display_name,
                "role": row.role,
                "is_active": row.is_active,
                "created_at": row.created_at,
                "usage": {
                    "today": {"tokens": 0, "cost_usd": 0.0, "runs": 0},
                    "seven_days": {"tokens": 0, "cost_usd": 0.0, "runs": 0},
                    "thirty_days": {"tokens": 0, "cost_usd": 0.0, "runs": 0},
                },
            }
        )

    return {"users": users, "total": total, "limit": limit, "offset": offset}


def get_user_usage_detail(
    db: Session,
    user_id: int,
    period: Literal["today", "7d", "30d"] = "7d",
) -> dict:
    """Get detailed usage for a specific user (admin-only).

    Args:
        db: Database session
        user_id: ID of the user to get details for
        period: Time period for breakdown - "today", "7d", or "30d"

    Returns:
        Dict with user info, period summary, daily breakdown, top automations
    """
    from zerg.models.models import User as UserModel

    # Get user
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        return None

    start_dt, end_dt = _usage_range_for_period(period)

    # Get period summary
    summary = _get_user_usage_range(db, user_id, start_dt, end_dt)

    daily_breakdown = []
    top_automations = []

    # Get usage for all periods for the user row
    usage_today = {"tokens": 0, "cost_usd": 0.0, "runs": 0}
    usage_7d = {"tokens": 0, "cost_usd": 0.0, "runs": 0}
    usage_30d = {"tokens": 0, "cost_usd": 0.0, "runs": 0}

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at,
            "usage": {
                "today": usage_today,
                "seven_days": usage_7d,
                "thirty_days": usage_30d,
            },
        },
        "period": period,
        "summary": {"tokens": summary["tokens"], "cost_usd": summary["cost_usd"], "runs": summary["runs"]},
        "daily_breakdown": daily_breakdown,
        "top_automations": top_automations,
    }
