"""Pydantic models for ops endpoints to ensure proper OpenAPI schema generation."""

from typing import List
from typing import Optional

from pydantic import BaseModel


class BudgetInfo(BaseModel):
    """Budget information with limit and usage."""
    limit_cents: int
    used_usd: float
    percent: Optional[float]


class LatencyStats(BaseModel):
    """Latency statistics."""
    p50: int
    p95: int


class OpsTopAgent(BaseModel):
    """Top performing agent information."""
    agent_id: int
    name: str
    owner_email: str
    runs: int
    cost_usd: Optional[float]
    p95_ms: int


class OpsSummary(BaseModel):
    """Operations summary with all KPIs."""
    runs_today: int
    cost_today_usd: Optional[float]
    budget_user: BudgetInfo
    budget_global: BudgetInfo
    active_users_24h: int
    agents_total: int
    agents_scheduled: int
    latency_ms: LatencyStats
    errors_last_hour: int
    top_agents_today: List[OpsTopAgent]


class OpsSeriesPoint(BaseModel):
    """Single point in a time series."""
    hour_iso: str  # Service returns this field name consistently
    value: float


class TimeSeriesResponse(BaseModel):
    """Time series response."""
    series: List[OpsSeriesPoint]


class TopAgentsResponse(BaseModel):
    """Response containing top agents list."""
    top_agents: List[OpsTopAgent]