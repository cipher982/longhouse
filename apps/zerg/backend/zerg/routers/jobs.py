"""API router for scheduled jobs management.

Provides endpoints to:
- List registered jobs
- Manually trigger job execution (for testing)
- View job status
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel

from zerg.dependencies.auth import require_admin
from zerg.jobs.registry import job_registry
from zerg.jobs.registry import register_all_jobs
from zerg.models.models import User as UserModel

logger = logging.getLogger(__name__)


# Pydantic schemas for API responses
class JobInfo(BaseModel):
    """Job information for API responses."""

    id: str
    cron: str
    enabled: bool
    timeout_seconds: int
    max_attempts: int
    tags: list[str]
    project: str | None
    description: str


class JobListResponse(BaseModel):
    """Response for listing jobs."""

    jobs: list[JobInfo]
    total: int


class JobRunResponse(BaseModel):
    """Response for job execution."""

    job_id: str
    status: str  # "success", "failure", "timeout"
    duration_ms: int
    result: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None


router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_admin)],
)


# Ensure jobs are registered on module import
# This loads job modules but doesn't schedule them (no scheduler passed)
_registered = False


def _ensure_jobs_registered() -> None:
    """Ensure job modules are imported and jobs registered."""
    global _registered
    if not _registered:
        register_all_jobs(scheduler=None)
        _registered = True


@router.get("/", response_model=JobListResponse)
def list_jobs(
    enabled_only: bool = False,
    current_user: UserModel = Depends(require_admin),
):
    """List all registered scheduled jobs.

    Args:
        enabled_only: If True, only return enabled jobs
    """
    _ensure_jobs_registered()

    jobs = job_registry.list_jobs(enabled_only=enabled_only)
    job_infos = [
        JobInfo(
            id=j.id,
            cron=j.cron,
            enabled=j.enabled,
            timeout_seconds=j.timeout_seconds,
            max_attempts=j.max_attempts,
            tags=j.tags,
            project=j.project,
            description=j.description,
        )
        for j in jobs
    ]

    return JobListResponse(jobs=job_infos, total=len(job_infos))


@router.get("/{job_id}", response_model=JobInfo)
def get_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Get details for a specific job."""
    _ensure_jobs_registered()

    config = job_registry.get(job_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return JobInfo(
        id=config.id,
        cron=config.cron,
        enabled=config.enabled,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        tags=config.tags,
        project=config.project,
        description=config.description,
    )


@router.post("/{job_id}/run", response_model=JobRunResponse)
async def run_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Manually trigger a job execution.

    This runs the job immediately regardless of its cron schedule or
    enabled status. Useful for:
    - Smoke testing migrated jobs
    - Manual ad-hoc execution
    - Debugging job behavior

    The job runs with full retry support and timeout enforcement.
    Results are shipped to Life Hub for tracking.
    """
    _ensure_jobs_registered()

    config = job_registry.get(job_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    logger.info("Manual job trigger: %s (by user %s)", job_id, current_user.email)

    result = await job_registry.run_job(job_id)

    return JobRunResponse(
        job_id=result.job_id,
        status=result.status,
        duration_ms=result.duration_ms,
        result=result.result,
        error=result.error,
        error_type=result.error_type,
    )


@router.post("/{job_id}/enable", response_model=JobInfo)
def enable_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Enable a job for scheduled execution."""
    _ensure_jobs_registered()

    if not job_registry.enable(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    config = job_registry.get(job_id)
    logger.info("Job enabled: %s (by user %s)", job_id, current_user.email)

    return JobInfo(
        id=config.id,
        cron=config.cron,
        enabled=config.enabled,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        tags=config.tags,
        project=config.project,
        description=config.description,
    )


@router.post("/{job_id}/disable", response_model=JobInfo)
def disable_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Disable a job from scheduled execution."""
    _ensure_jobs_registered()

    if not job_registry.disable(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    config = job_registry.get(job_id)
    logger.info("Job disabled: %s (by user %s)", job_id, current_user.email)

    return JobInfo(
        id=config.id,
        cron=config.cron,
        enabled=config.enabled,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        tags=config.tags,
        project=config.project,
        description=config.description,
    )
