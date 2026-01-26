"""Pydantic models for LLM usage visibility endpoints."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from typing import Literal
from typing import Optional

from pydantic import BaseModel
from pydantic import Field


class TokenBreakdown(BaseModel):
    """Breakdown of token usage by type."""

    prompt: Optional[int] = Field(None, description="Prompt/input tokens (if tracked)")
    completion: Optional[int] = Field(None, description="Completion/output tokens (if tracked)")
    total: int = Field(..., description="Total tokens used")


class UsageLimit(BaseModel):
    """User's daily budget limit status."""

    daily_cost_cents: int = Field(..., description="Configured daily limit in cents (0=unlimited)")
    used_percent: float = Field(..., description="Percentage of daily limit used")
    remaining_usd: float = Field(..., description="Remaining budget in USD")
    status: Literal["ok", "warning", "exceeded", "unlimited"] = Field(
        ...,
        description="ok=<80%, warning=80-99%, exceeded=>=100%, unlimited=no limit configured",
    )


class UserUsageResponse(BaseModel):
    """Response for GET /api/users/me/usage."""

    period: Literal["today", "7d", "30d"] = Field(..., description="Time period for usage stats")
    tokens: TokenBreakdown = Field(..., description="Token usage breakdown")
    cost_usd: float = Field(..., description="Total cost in USD")
    courses: int = Field(..., description="Number of courses in period")
    limit: UsageLimit = Field(..., description="Daily budget limit info (always today's limit)")


# ---------------------------------------------------------------------------
# Admin models (for Phase 2)
# ---------------------------------------------------------------------------


class PeriodUsage(BaseModel):
    """Usage stats for a single period."""

    tokens: int = Field(..., description="Total tokens")
    cost_usd: float = Field(..., description="Total cost in USD")
    courses: int = Field(..., description="Number of courses")


class UserUsageSummary(BaseModel):
    """Multi-period usage summary for a user."""

    today: PeriodUsage
    seven_days: PeriodUsage  # Named for Python compat (not "7d")
    thirty_days: PeriodUsage


class AdminUserRow(BaseModel):
    """User row with usage stats for admin list view."""

    id: int
    email: str
    display_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: Optional[datetime] = None
    usage: UserUsageSummary


class AdminUsersResponse(BaseModel):
    """Response for GET /api/admin/users."""

    users: list[AdminUserRow]
    total: int
    limit: int
    offset: int


class DailyBreakdown(BaseModel):
    """Single day's usage stats."""

    date: date
    tokens: int
    cost_usd: float
    courses: int


class TopFicheUsage(BaseModel):
    """Fiche usage stats for user detail view."""

    fiche_id: int
    name: str
    tokens: int
    cost_usd: float
    courses: int


class AdminUserDetailResponse(BaseModel):
    """Response for GET /api/admin/users/{id}/usage."""

    user: AdminUserRow
    period: str
    summary: PeriodUsage
    daily_breakdown: list[DailyBreakdown]
    top_fiches: list[TopFicheUsage]
