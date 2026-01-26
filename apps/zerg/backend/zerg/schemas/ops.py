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


class OpsTopFiche(BaseModel):
    """Top performing fiche information."""

    fiche_id: int
    name: str
    owner_email: str
    courses: int
    cost_usd: Optional[float]
    p95_ms: int


class OpsSummary(BaseModel):
    """Operations summary with all KPIs."""

    courses_today: int
    cost_today_usd: Optional[float]
    budget_user: BudgetInfo
    budget_global: BudgetInfo
    active_users_24h: int
    fiches_total: int
    fiches_scheduled: int
    latency_ms: LatencyStats
    errors_last_hour: int
    top_fiches_today: List[OpsTopFiche]


class OpsSeriesPoint(BaseModel):
    """Single point in a time series."""

    hour_iso: str  # Service returns this field name consistently
    value: float


class TimeSeriesResponse(BaseModel):
    """Time series response."""

    series: List[OpsSeriesPoint]


class TopFichesResponse(BaseModel):
    """Response containing top fiches list."""

    top_fiches: List[OpsTopFiche]
