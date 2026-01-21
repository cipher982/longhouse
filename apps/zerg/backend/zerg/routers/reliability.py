"""Reliability Dashboard API endpoints.

Admin-only endpoints for system health monitoring, error analysis, and performance metrics.
"""

import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_admin
from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.models.models import Runner
from zerg.models.models import WorkerJob

# Secret patterns to redact (same as trace_debugger)
SECRET_PATTERNS = [
    r"(api[_-]?key[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(password[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(secret[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(token[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
    r"(bearer\s+)([A-Za-z0-9\-_]+\.?[A-Za-z0-9\-_]*\.?[A-Za-z0-9\-_]*)",
    r"(authorization[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)",
]


def _redact_string(s: str | None) -> str | None:
    """Redact secrets from a string."""
    if s is None:
        return None
    result = s
    for pattern in SECRET_PATTERNS:
        result = re.sub(pattern, r"\1[REDACTED]", result, flags=re.IGNORECASE)
    return result


router = APIRouter(prefix="/reliability", tags=["reliability"])


@router.get("/system-health")
async def system_health(
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """Aggregated system health status (admin only).

    Returns worker pool status, recent error counts, and overall health indicator.
    """
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)

    # Worker pool: count runners by status
    runner_counts = db.query(Runner.status, func.count(Runner.id)).group_by(Runner.status).all()
    worker_pool = {status: count for status, count in runner_counts}

    # Recent errors: count failed runs in last hour
    error_count = (
        db.query(func.count(AgentRun.id))
        .filter(
            AgentRun.status == RunStatus.FAILED,
            AgentRun.created_at >= hour_ago,
        )
        .scalar()
        or 0
    )

    # Recent worker failures
    worker_error_count = (
        db.query(func.count(WorkerJob.id))
        .filter(
            WorkerJob.status == "failed",  # WorkerJob.status is a string column, not enum
            WorkerJob.created_at >= hour_ago,
        )
        .scalar()
        or 0
    )

    # Determine overall status
    status = "healthy"
    if error_count > 10 or worker_error_count > 10:
        status = "degraded"
    if worker_pool.get("online", 0) == 0 and worker_pool.get("offline", 0) > 0:
        status = "unhealthy"

    return {
        "workers": worker_pool,
        "recent_run_errors": error_count,
        "recent_worker_errors": worker_error_count,
        "status": status,
        "checked_at": now.isoformat(),
    }


@router.get("/errors")
async def error_analysis(
    hours: int = Query(24, le=168, description="Time window in hours"),
    limit: int = Query(50, le=200, description="Maximum errors to return"),
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """Error frequency and patterns (admin only).

    Returns recent failed runs with error details for analysis.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Get failed runs
    run_errors = (
        db.query(AgentRun)
        .filter(
            AgentRun.status == RunStatus.FAILED,
            AgentRun.created_at >= since,
        )
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )

    # Get failed workers
    worker_errors = (
        db.query(WorkerJob)
        .filter(
            WorkerJob.status == "failed",  # WorkerJob.status is a string column
            WorkerJob.created_at >= since,
        )
        .order_by(WorkerJob.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "run_errors": [
            {
                "id": e.id,
                "error": _redact_string(e.error[:200] if e.error else None),
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "trace_id": str(e.trace_id) if e.trace_id else None,
            }
            for e in run_errors
        ],
        "worker_errors": [
            {
                "id": e.id,
                "error": _redact_string(e.error[:200] if e.error else None),
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "task_preview": _redact_string(e.task[:100] if e.task else None),
                "trace_id": str(e.trace_id) if e.trace_id else None,
            }
            for e in worker_errors
        ],
        "total_run_errors": len(run_errors),
        "total_worker_errors": len(worker_errors),
        "hours": hours,
    }


@router.get("/performance")
async def performance_metrics(
    hours: int = Query(24, le=168, description="Time window in hours"),
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """P50/P95 latency metrics (admin only).

    Returns latency percentiles for supervisor runs.
    Limited to 10000 samples to prevent memory issues.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Get durations for completed runs, limited and ordered for consistent sampling
    runs = (
        db.query(AgentRun.duration_ms)
        .filter(
            AgentRun.created_at >= since,
            AgentRun.duration_ms.isnot(None),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(10000)  # Cap to prevent memory issues
        .all()
    )

    durations = sorted([r.duration_ms for r in runs if r.duration_ms is not None])

    if not durations:
        return {"p50": None, "p95": None, "p99": None, "count": 0, "hours": hours}

    p50 = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95)]
    p99 = durations[int(len(durations) * 0.99)] if len(durations) >= 100 else None

    return {
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "count": len(durations),
        "min": min(durations),
        "max": max(durations),
        "hours": hours,
    }


@router.get("/workers/stuck")
async def stuck_workers(
    threshold_mins: int = Query(10, le=60, description="Threshold in minutes"),
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """Workers in running state beyond threshold (admin only).

    Returns workers that have been running longer than the threshold,
    which may indicate stuck or failed processes.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_mins)

    stuck = (
        db.query(WorkerJob)
        .filter(
            WorkerJob.status == "running",
            WorkerJob.started_at < cutoff,
        )
        .limit(50)
        .all()
    )

    return {
        "stuck_count": len(stuck),
        "threshold_mins": threshold_mins,
        "workers": [
            {
                "id": w.id,
                "task": w.task[:100] if w.task else None,
                "started_at": w.started_at.isoformat() if w.started_at else None,
                "worker_id": w.worker_id,
                "trace_id": str(w.trace_id) if w.trace_id else None,
            }
            for w in stuck
        ],
    }


@router.get("/runners")
async def runner_status(
    db: Session = Depends(get_db),
    _user=Depends(require_admin),  # Admin only
):
    """Current runner status (admin only).

    Returns status of all runners in the system.
    """
    runners = db.query(Runner).order_by(Runner.last_seen_at.desc().nullslast()).limit(100).all()

    return {
        "total": len(runners),
        "runners": [
            {
                "id": r.id,
                "name": r.name,
                "status": r.status,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                "capabilities": r.capabilities,
            }
            for r in runners
        ],
    }
