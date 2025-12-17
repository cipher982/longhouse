"""LLM usage aggregation service.

Provides user-facing and admin-facing usage statistics based on AgentRun data.
All costs are derived from AgentRun.total_cost_usd (NULL costs are excluded from sums).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun as AgentRunModel
from zerg.schemas.usage import (
    TokenBreakdown,
    UsageLimit,
    UserUsageResponse,
)


def _today_utc() -> datetime.date:
    """Return today's date in UTC."""
    return datetime.now(timezone.utc).date()


def _period_start_date(period: Literal["today", "7d", "30d"]) -> datetime.date:
    """Return the start date for the given period."""
    today = _today_utc()
    if period == "today":
        return today
    elif period == "7d":
        return today - timedelta(days=6)  # Include today
    elif period == "30d":
        return today - timedelta(days=29)  # Include today
    else:
        raise ValueError(f"Invalid period: {period}")


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
    start_date = _period_start_date(period)

    # Query aggregates for the period
    # We join AgentRun -> Agent to filter by owner_id
    result = (
        db.query(
            func.coalesce(func.sum(AgentRunModel.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0).label("total_cost"),
            func.count(AgentRunModel.id).label("run_count"),
        )
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            func.date(AgentRunModel.started_at) >= start_date,
        )
        .first()
    )

    total_tokens = int(result.total_tokens) if result else 0
    total_cost = float(result.total_cost) if result else 0.0
    run_count = int(result.run_count) if result else 0

    # Get today's cost for limit calculation (limits are always daily)
    today = _today_utc()
    today_cost_result = (
        db.query(func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0))
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            func.date(AgentRunModel.started_at) == today,
        )
        .scalar()
    )
    today_cost_usd = float(today_cost_result) if today_cost_result else 0.0

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
        total=total_tokens,
    )

    return UserUsageResponse(
        period=period,
        tokens=tokens,
        cost_usd=round(total_cost, 4),
        runs=run_count,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Admin functions (Phase 2)
# ---------------------------------------------------------------------------


def _get_user_usage_for_period(db: Session, user_id: int, start_date) -> dict:
    """Get usage stats for a user in a given period (helper)."""
    result = (
        db.query(
            func.coalesce(func.sum(AgentRunModel.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0).label("total_cost"),
            func.count(AgentRunModel.id).label("run_count"),
        )
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            func.date(AgentRunModel.started_at) >= start_date,
        )
        .first()
    )

    return {
        "tokens": int(result.total_tokens) if result else 0,
        "cost_usd": round(float(result.total_cost), 4) if result else 0.0,
        "runs": int(result.run_count) if result else 0,
    }


def get_all_users_usage(
    db: Session,
    *,
    sort: str = "cost_today",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
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

    today = _today_utc()
    start_7d = today - timedelta(days=6)
    start_30d = today - timedelta(days=29)

    # Get all users first
    users_query = db.query(UserModel).filter(UserModel.is_active == True)  # noqa: E712
    total = users_query.count()

    # Build user list with usage stats
    users_list = []
    for user in users_query.all():
        usage_today = _get_user_usage_for_period(db, user.id, today)
        usage_7d = _get_user_usage_for_period(db, user.id, start_7d)
        usage_30d = _get_user_usage_for_period(db, user.id, start_30d)

        users_list.append({
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "usage": {
                "today": usage_today,
                "seven_days": usage_7d,
                "thirty_days": usage_30d,
            },
        })

    # Sort the list
    sort_key_map = {
        "cost_today": lambda x: x["usage"]["today"]["cost_usd"],
        "cost_7d": lambda x: x["usage"]["seven_days"]["cost_usd"],
        "cost_30d": lambda x: x["usage"]["thirty_days"]["cost_usd"],
        "email": lambda x: x["email"].lower(),
        "created_at": lambda x: x["created_at"] or "",
    }

    sort_fn = sort_key_map.get(sort, sort_key_map["cost_today"])
    reverse = order.lower() == "desc"
    users_list.sort(key=sort_fn, reverse=reverse)

    # Apply pagination
    paginated = users_list[offset : offset + limit]

    return {
        "users": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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

    # Calculate period dates
    today = _today_utc()
    start_date = _period_start_date(period)

    # Get period summary
    summary = _get_user_usage_for_period(db, user_id, start_date)

    # Get daily breakdown
    daily_query = (
        db.query(
            func.date(AgentRunModel.started_at).label("run_date"),
            func.coalesce(func.sum(AgentRunModel.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0).label("cost_usd"),
            func.count(AgentRunModel.id).label("runs"),
        )
        .join(AgentModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            func.date(AgentRunModel.started_at) >= start_date,
        )
        .group_by(func.date(AgentRunModel.started_at))
        .order_by(func.date(AgentRunModel.started_at).desc())
        .all()
    )

    daily_breakdown = [
        {
            "date": str(row.run_date),
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
            func.coalesce(func.sum(AgentRunModel.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(AgentRunModel.total_cost_usd), 0.0).label("cost_usd"),
            func.count(AgentRunModel.id).label("runs"),
        )
        .join(AgentRunModel, AgentModel.id == AgentRunModel.agent_id)
        .filter(
            AgentModel.owner_id == user_id,
            AgentRunModel.started_at.isnot(None),
            func.date(AgentRunModel.started_at) >= start_date,
        )
        .group_by(AgentModel.id, AgentModel.name)
        .order_by(func.sum(AgentRunModel.total_cost_usd).desc())
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
    usage_today = _get_user_usage_for_period(db, user_id, today)
    usage_7d = _get_user_usage_for_period(db, user_id, today - timedelta(days=6))
    usage_30d = _get_user_usage_for_period(db, user_id, today - timedelta(days=29))

    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "usage": {
                "today": usage_today,
                "seven_days": usage_7d,
                "thirty_days": usage_30d,
            },
        },
        "period": period,
        "summary": summary,
        "daily_breakdown": daily_breakdown,
        "top_agents": top_agents,
    }
