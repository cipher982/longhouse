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
# Admin functions (Phase 2 - stubs for now)
# ---------------------------------------------------------------------------


def get_all_users_usage(
    db: Session,
    *,
    sort: str = "cost_today",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Get all users with their usage stats (admin-only).

    TODO: Implement in Phase 2.
    """
    raise NotImplementedError("Admin users list not yet implemented")


def get_user_usage_detail(
    db: Session,
    user_id: int,
    period: Literal["today", "7d", "30d"] = "today",
) -> dict:
    """Get detailed usage for a specific user (admin-only).

    TODO: Implement in Phase 2.
    """
    raise NotImplementedError("Admin user detail not yet implemented")
