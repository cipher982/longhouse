"""LLM usage aggregation service.

Provides user-facing and admin-facing usage statistics based on Course data.
All costs are derived from Course.total_cost_usd (NULL costs are excluded from sums).
"""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Literal
from typing import Optional

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.models import Course as CourseModel
from zerg.models.models import Fiche as FicheModel
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
    """Aggregate token/cost/course stats for a user within [start_dt, end_dt)."""
    result = (
        db.query(
            func.coalesce(func.sum(func.coalesce(CourseModel.total_tokens, 0)), 0).label("total_tokens"),
            func.coalesce(func.sum(func.coalesce(CourseModel.total_cost_usd, 0.0)), 0.0).label("total_cost"),
            func.count(CourseModel.id).label("course_count"),
        )
        .join(FicheModel, FicheModel.id == CourseModel.fiche_id)
        .filter(
            FicheModel.owner_id == user_id,
            CourseModel.started_at.isnot(None),
            CourseModel.started_at >= start_dt,
            CourseModel.started_at < end_dt,
        )
        .first()
    )

    return {
        "tokens": int(result.total_tokens) if result else 0,
        "cost_usd": round(float(result.total_cost), 4) if result else 0.0,
        "courses": int(result.course_count) if result else 0,
    }


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
    # separately on Course, so they'll be None
    tokens = TokenBreakdown(
        prompt=None,  # Not tracked separately yet
        completion=None,  # Not tracked separately yet
        total=period_usage["tokens"],
    )

    return UserUsageResponse(
        period=period,
        tokens=tokens,
        cost_usd=period_usage["cost_usd"],
        courses=period_usage["courses"],
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

    today = _today_utc_date()
    today_start = _utc_day_start(today)
    tomorrow_start = _utc_day_start(today + timedelta(days=1))
    start_7d = _utc_day_start(today - timedelta(days=6))
    start_30d = _utc_day_start(today - timedelta(days=29))

    user_filters = []
    if active is True:
        user_filters.append(UserModel.is_active.is_(True))
    elif active is False:
        user_filters.append(UserModel.is_active.is_(False))

    total = int(db.query(func.count(UserModel.id)).filter(*user_filters).scalar() or 0)

    # Limit the joined course rows to the last 30 days to keep the join small.
    course_join_cond = and_(
        CourseModel.fiche_id == FicheModel.id,
        CourseModel.started_at.isnot(None),
        CourseModel.started_at >= start_30d,
        CourseModel.started_at < tomorrow_start,
    )

    def _sum_when(cond, value, *, else_value):
        return func.coalesce(func.sum(case((cond, value), else_=else_value)), else_value)

    # Today aggregates
    today_cond = and_(CourseModel.started_at >= today_start, CourseModel.started_at < tomorrow_start)
    tokens_today = _sum_when(today_cond, func.coalesce(CourseModel.total_tokens, 0), else_value=0).label("tokens_today")
    cost_today = _sum_when(today_cond, func.coalesce(CourseModel.total_cost_usd, 0.0), else_value=0.0).label("cost_today")
    courses_today = _sum_when(today_cond, 1, else_value=0).label("courses_today")

    # 7d aggregates
    seven_cond = and_(CourseModel.started_at >= start_7d, CourseModel.started_at < tomorrow_start)
    tokens_7d = _sum_when(seven_cond, func.coalesce(CourseModel.total_tokens, 0), else_value=0).label("tokens_7d")
    cost_7d = _sum_when(seven_cond, func.coalesce(CourseModel.total_cost_usd, 0.0), else_value=0.0).label("cost_7d")
    courses_7d = _sum_when(seven_cond, 1, else_value=0).label("courses_7d")

    # 30d aggregates
    thirty_cond = and_(CourseModel.started_at >= start_30d, CourseModel.started_at < tomorrow_start)
    tokens_30d = _sum_when(thirty_cond, func.coalesce(CourseModel.total_tokens, 0), else_value=0).label("tokens_30d")
    cost_30d = _sum_when(thirty_cond, func.coalesce(CourseModel.total_cost_usd, 0.0), else_value=0.0).label("cost_30d")
    courses_30d = _sum_when(thirty_cond, 1, else_value=0).label("courses_30d")

    query = (
        db.query(
            UserModel.id,
            UserModel.email,
            UserModel.display_name,
            UserModel.role,
            UserModel.is_active,
            UserModel.created_at,
            tokens_today,
            cost_today,
            courses_today,
            tokens_7d,
            cost_7d,
            courses_7d,
            tokens_30d,
            cost_30d,
            courses_30d,
        )
        .outerjoin(FicheModel, FicheModel.owner_id == UserModel.id)
        .outerjoin(CourseModel, course_join_cond)
        .filter(*user_filters)
        .group_by(
            UserModel.id,
            UserModel.email,
            UserModel.display_name,
            UserModel.role,
            UserModel.is_active,
            UserModel.created_at,
        )
    )

    sort_expr_map = {
        "cost_today": cost_today,
        "cost_7d": cost_7d,
        "cost_30d": cost_30d,
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
                    "today": {
                        "tokens": int(row.tokens_today or 0),
                        "cost_usd": round(float(row.cost_today or 0.0), 4),
                        "courses": int(row.courses_today or 0),
                    },
                    "seven_days": {
                        "tokens": int(row.tokens_7d or 0),
                        "cost_usd": round(float(row.cost_7d or 0.0), 4),
                        "courses": int(row.courses_7d or 0),
                    },
                    "thirty_days": {
                        "tokens": int(row.tokens_30d or 0),
                        "cost_usd": round(float(row.cost_30d or 0.0), 4),
                        "courses": int(row.courses_30d or 0),
                    },
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
        Dict with user info, period summary, daily breakdown, top fiches
    """
    from zerg.models.models import User as UserModel

    # Get user
    user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if not user:
        return None

    today = _today_utc_date()
    today_start = _utc_day_start(today)
    tomorrow_start = _utc_day_start(today + timedelta(days=1))

    start_dt, end_dt = _usage_range_for_period(period)

    # Get period summary
    summary = _get_user_usage_range(db, user_id, start_dt, end_dt)

    # Get daily breakdown
    daily_query = (
        db.query(
            func.date(CourseModel.started_at).label("course_date"),
            func.coalesce(func.sum(func.coalesce(CourseModel.total_tokens, 0)), 0).label("tokens"),
            func.coalesce(func.sum(func.coalesce(CourseModel.total_cost_usd, 0.0)), 0.0).label("cost_usd"),
            func.count(CourseModel.id).label("courses"),
        )
        .join(FicheModel, FicheModel.id == CourseModel.fiche_id)
        .filter(
            FicheModel.owner_id == user_id,
            CourseModel.started_at.isnot(None),
            CourseModel.started_at >= start_dt,
            CourseModel.started_at < end_dt,
        )
        .group_by(func.date(CourseModel.started_at))
        .order_by(func.date(CourseModel.started_at).desc())
        .all()
    )

    daily_breakdown = [
        {
            "date": row.course_date,
            "tokens": int(row.tokens),
            "cost_usd": round(float(row.cost_usd), 4),
            "courses": int(row.courses),
        }
        for row in daily_query
    ]

    # Get top fiches by cost
    top_fiches_query = (
        db.query(
            FicheModel.id.label("fiche_id"),
            FicheModel.name,
            func.coalesce(func.sum(func.coalesce(CourseModel.total_tokens, 0)), 0).label("tokens"),
            func.coalesce(func.sum(func.coalesce(CourseModel.total_cost_usd, 0.0)), 0.0).label("cost_usd"),
            func.count(CourseModel.id).label("courses"),
        )
        .join(CourseModel, FicheModel.id == CourseModel.fiche_id)
        .filter(
            FicheModel.owner_id == user_id,
            CourseModel.started_at.isnot(None),
            CourseModel.started_at >= start_dt,
            CourseModel.started_at < end_dt,
        )
        .group_by(FicheModel.id, FicheModel.name)
        .order_by(func.sum(func.coalesce(CourseModel.total_cost_usd, 0.0)).desc())
        .limit(10)
        .all()
    )

    top_fiches = [
        {
            "fiche_id": row.fiche_id,
            "name": row.name,
            "tokens": int(row.tokens),
            "cost_usd": round(float(row.cost_usd), 4),
            "courses": int(row.courses),
        }
        for row in top_fiches_query
    ]

    # Get usage for all periods for the user row
    usage_today = _get_user_usage_range(db, user_id, today_start, tomorrow_start)
    usage_7d = _get_user_usage_range(db, user_id, _utc_day_start(today - timedelta(days=6)), tomorrow_start)
    usage_30d = _get_user_usage_range(db, user_id, _utc_day_start(today - timedelta(days=29)), tomorrow_start)

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
        "summary": {"tokens": summary["tokens"], "cost_usd": summary["cost_usd"], "courses": summary["courses"]},
        "daily_breakdown": daily_breakdown,
        "top_fiches": top_fiches,
    }
