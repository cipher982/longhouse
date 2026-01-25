"""LLM usage aggregation service.

Provides user-facing and admin-facing usage statistics based on AgentRun data.
All costs are derived from AgentRun.total_cost_usd (NULL costs are excluded from sums).
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
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun as AgentRunModel
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
    result = (
        db.query(
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_tokens, 0)), 0).label("total_tokens"),
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_cost_usd, 0.0)), 0.0).label("total_cost"),
            func.count(AgentRunModel.id).label("run_count"),
        )
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            AgentRunModel.started_at >= start_dt,
            AgentRunModel.started_at < end_dt,
        )
        .first()
    )

    return {
        "tokens": int(result.total_tokens) if result else 0,
        "cost_usd": round(float(result.total_cost), 4) if result else 0.0,
        "runs": int(result.run_count) if result else 0,
    }


def _is_demo_prefs(prefs: Optional[dict]) -> bool:
    if not prefs:
        return False
    return bool(prefs.get("demo") or prefs.get("is_demo"))


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
    # separately on AgentRun, so they'll be None
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

    # Limit the joined run rows to the last 30 days to keep the join small.
    run_join_cond = and_(
        AgentRunModel.agent_id == AgentModel.id,
        AgentRunModel.started_at.isnot(None),
        AgentRunModel.started_at >= start_30d,
        AgentRunModel.started_at < tomorrow_start,
    )

    def _sum_when(cond, value, *, else_value):
        return func.coalesce(func.sum(case((cond, value), else_=else_value)), else_value)

    # Today aggregates
    today_cond = and_(AgentRunModel.started_at >= today_start, AgentRunModel.started_at < tomorrow_start)
    tokens_today = _sum_when(today_cond, func.coalesce(AgentRunModel.total_tokens, 0), else_value=0).label("tokens_today")
    cost_today = _sum_when(today_cond, func.coalesce(AgentRunModel.total_cost_usd, 0.0), else_value=0.0).label("cost_today")
    runs_today = _sum_when(today_cond, 1, else_value=0).label("runs_today")

    # 7d aggregates
    seven_cond = and_(AgentRunModel.started_at >= start_7d, AgentRunModel.started_at < tomorrow_start)
    tokens_7d = _sum_when(seven_cond, func.coalesce(AgentRunModel.total_tokens, 0), else_value=0).label("tokens_7d")
    cost_7d = _sum_when(seven_cond, func.coalesce(AgentRunModel.total_cost_usd, 0.0), else_value=0.0).label("cost_7d")
    runs_7d = _sum_when(seven_cond, 1, else_value=0).label("runs_7d")

    # 30d aggregates
    thirty_cond = and_(AgentRunModel.started_at >= start_30d, AgentRunModel.started_at < tomorrow_start)
    tokens_30d = _sum_when(thirty_cond, func.coalesce(AgentRunModel.total_tokens, 0), else_value=0).label("tokens_30d")
    cost_30d = _sum_when(thirty_cond, func.coalesce(AgentRunModel.total_cost_usd, 0.0), else_value=0.0).label("cost_30d")
    runs_30d = _sum_when(thirty_cond, 1, else_value=0).label("runs_30d")

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
            runs_today,
            tokens_7d,
            cost_7d,
            runs_7d,
            tokens_30d,
            cost_30d,
            runs_30d,
        )
        .outerjoin(AgentModel, AgentModel.owner_id == UserModel.id)
        .outerjoin(AgentRunModel, run_join_cond)
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

    user_ids = [row.id for row in rows]
    prefs_by_id: dict[int, Optional[dict]] = {}
    if user_ids:
        prefs_rows = db.query(UserModel.id, UserModel.prefs).filter(UserModel.id.in_(user_ids)).all()
        prefs_by_id = {row.id: row.prefs for row in prefs_rows}

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
                "is_demo": _is_demo_prefs(prefs_by_id.get(row.id)),
                "usage": {
                    "today": {
                        "tokens": int(row.tokens_today or 0),
                        "cost_usd": round(float(row.cost_today or 0.0), 4),
                        "runs": int(row.runs_today or 0),
                    },
                    "seven_days": {
                        "tokens": int(row.tokens_7d or 0),
                        "cost_usd": round(float(row.cost_7d or 0.0), 4),
                        "runs": int(row.runs_7d or 0),
                    },
                    "thirty_days": {
                        "tokens": int(row.tokens_30d or 0),
                        "cost_usd": round(float(row.cost_30d or 0.0), 4),
                        "runs": int(row.runs_30d or 0),
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
        Dict with user info, period summary, daily breakdown, top agents
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
            func.date(AgentRunModel.started_at).label("run_date"),
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_tokens, 0)), 0).label("tokens"),
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_cost_usd, 0.0)), 0.0).label("cost_usd"),
            func.count(AgentRunModel.id).label("runs"),
        )
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            AgentRunModel.started_at >= start_dt,
            AgentRunModel.started_at < end_dt,
        )
        .group_by(func.date(AgentRunModel.started_at))
        .order_by(func.date(AgentRunModel.started_at).desc())
        .all()
    )

    daily_breakdown = [
        {
            "date": row.run_date,
            "tokens": int(row.tokens),
            "cost_usd": round(float(row.cost_usd), 4),
            "runs": int(row.runs),
        }
        for row in daily_query
    ]

    # Get top agents by cost
    top_agents_query = (
        db.query(
            AgentModel.id.label("agent_id"),
            AgentModel.name,
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_tokens, 0)), 0).label("tokens"),
            func.coalesce(func.sum(func.coalesce(AgentRunModel.total_cost_usd, 0.0)), 0.0).label("cost_usd"),
            func.count(AgentRunModel.id).label("runs"),
        )
        .join(AgentRunModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            AgentRunModel.started_at >= start_dt,
            AgentRunModel.started_at < end_dt,
        )
        .group_by(AgentModel.id, AgentModel.name)
        .order_by(func.sum(func.coalesce(AgentRunModel.total_cost_usd, 0.0)).desc())
        .limit(10)
        .all()
    )

    top_agents = [
        {
            "agent_id": row.agent_id,
            "name": row.name,
            "tokens": int(row.tokens),
            "cost_usd": round(float(row.cost_usd), 4),
            "runs": int(row.runs),
        }
        for row in top_agents_query
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
            "is_demo": _is_demo_prefs(user.prefs),
            "usage": {
                "today": usage_today,
                "seven_days": usage_7d,
                "thirty_days": usage_30d,
            },
        },
        "period": period,
        "summary": {"tokens": summary["tokens"], "cost_usd": summary["cost_usd"], "runs": summary["runs"]},
        "daily_breakdown": daily_breakdown,
        "top_agents": top_agents,
    }
