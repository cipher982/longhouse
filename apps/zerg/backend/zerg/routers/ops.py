"""Admin-only Ops Dashboard APIs."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_admin
from zerg.models.models import User as UserModel
from zerg.schemas.ops import OpsSummary, TimeSeriesResponse, TopAgentsResponse
from zerg.services.ops_service import get_summary as svc_get_summary
from zerg.services.ops_service import get_timeseries as svc_get_timeseries
from zerg.services.ops_service import get_top_agents as svc_get_top_agents

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])


@router.get("/summary", response_model=OpsSummary)
def get_summary(current_user: UserModel = Depends(require_admin), db: Session = Depends(get_db)):
    """Return primary KPIs for the Ops dashboard (admin-only)."""
    return svc_get_summary(db, current_user)


@router.get("/timeseries", response_model=TimeSeriesResponse)
def get_timeseries(
    metric: str = Query(
        ...,
        pattern="^(runs_by_hour|errors_by_hour|cost_by_hour|runs_by_day|errors_by_day|cost_by_day)$",
    ),
    window: str = Query("today", pattern="^(today|7d|30d)$"),
    db: Session = Depends(get_db),
):
    try:
        series_data = svc_get_timeseries(db, metric=metric, window=window)
        return TimeSeriesResponse(series=series_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/top", response_model=TopAgentsResponse)
def get_top(
    kind: str = Query("agents", pattern="^agents$"),
    window: str = Query("today", pattern="^(today|7d|30d)$"),
    limit: int = 5,
    db: Session = Depends(get_db),
):
    if kind != "agents":
        raise HTTPException(status_code=400, detail="Only kind=agents supported")
    try:
        top_agents = svc_get_top_agents(db, window=window, limit=limit)
        return TopAgentsResponse(top_agents=top_agents)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
