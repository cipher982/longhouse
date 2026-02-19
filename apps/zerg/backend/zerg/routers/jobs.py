"""API router for scheduled jobs management.

Provides endpoints to:
- List registered jobs
- Manually trigger job execution (for testing)
- View job status
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import require_admin
from zerg.jobs.registry import _normalize_secret_fields
from zerg.jobs.registry import job_registry
from zerg.jobs.registry import register_all_jobs
from zerg.models.models import JobRun
from zerg.models.models import JobSecret
from zerg.models.models import User as UserModel

logger = logging.getLogger(__name__)


# Pydantic schemas for API responses
class SecretFieldInfo(BaseModel):
    """Secret field metadata for API responses."""

    key: str
    label: str | None = None
    type: str = "password"
    placeholder: str | None = None
    description: str | None = None
    required: bool = True


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
    secrets: list[SecretFieldInfo] = []


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


class JobRunHistoryInfo(BaseModel):
    """Single job run record for history queries."""

    id: str
    job_id: str
    status: str
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    error_message: str | None
    error_type: str | None = None
    created_at: str


class JobRunHistoryResponse(BaseModel):
    """Response for job run history queries."""

    runs: list[JobRunHistoryInfo]
    total: int


class JobLastRunResponse(BaseModel):
    """Last run per job for dashboard overview."""

    last_runs: dict[str, JobRunHistoryInfo]  # job_id -> last run


def _job_info_from_config(config) -> JobInfo:
    """Build a JobInfo from a JobConfig, normalizing secret fields."""
    return JobInfo(
        id=config.id,
        cron=config.cron,
        enabled=config.enabled,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        tags=config.tags,
        project=config.project,
        description=config.description,
        secrets=[SecretFieldInfo(**sf) for sf in _normalize_secret_fields(config.secrets)],
    )


router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_admin)],
)


# Ensure jobs are registered on module import
# This loads job modules but doesn't schedule them (no scheduler passed)
_registered = False


async def _ensure_jobs_registered() -> None:
    """Ensure job modules are imported and jobs registered."""
    global _registered
    if not _registered:
        await register_all_jobs(scheduler=None)
        _registered = True


# ---------------------------------------------------------------------------
# Jobs Repo Endpoints (must come before /{job_id} to avoid route shadowing)
# ---------------------------------------------------------------------------


class JobsRepoStatusResponse(BaseModel):
    """Response for jobs repo status."""

    initialized: bool
    has_remote: bool
    remote_url: str | None
    last_commit_time: str | None
    last_commit_message: str | None
    jobs_dir: str
    job_count: int


class JobsRepoInitResponse(BaseModel):
    """Response for jobs repo initialization."""

    success: bool
    message: str
    jobs_dir: str


class JobsRepoSyncResponse(BaseModel):
    """Response for jobs repo sync."""

    success: bool
    message: str
    status: str  # "not_implemented", "synced", "error"


@router.get("/repo", response_model=JobsRepoStatusResponse)
async def get_jobs_repo_status(
    current_user: UserModel = Depends(require_admin),
):
    """Get the status of the jobs repository.

    Returns information about:
    - Whether the repo is initialized (has .git directory)
    - Whether a remote is configured
    - Last commit time and message
    - Number of jobs in the manifest
    """
    try:
        from zerg.services.jobs_repo import get_jobs_repo_status as _get_status

        status = _get_status()
        return JobsRepoStatusResponse(**status)
    except ImportError:
        # Service not yet implemented - return stub response
        from zerg.config import get_settings

        settings = get_settings()
        jobs_dir = getattr(settings, "jobs_dir", "/data/jobs")

        return JobsRepoStatusResponse(
            initialized=False,
            has_remote=False,
            remote_url=None,
            last_commit_time=None,
            last_commit_message=None,
            jobs_dir=jobs_dir,
            job_count=0,
        )
    except Exception as e:
        logger.error("Failed to get jobs repo status: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to get jobs repo status: {e}")


@router.post("/repo/init", response_model=JobsRepoInitResponse)
async def init_jobs_repo(
    current_user: UserModel = Depends(require_admin),
):
    """Initialize the jobs repository.

    Creates the jobs directory structure if it doesn't exist:
    - /data/jobs/manifest.py (job definitions)
    - /data/jobs/jobs/ (job modules)
    - Runs git init

    This is normally done automatically on first boot, but can be
    triggered manually if needed.
    """
    try:
        from zerg.services.jobs_repo import init_jobs_repo as _init_repo

        result = _init_repo()
        logger.info("Jobs repo initialized by user %s", current_user.email)
        return JobsRepoInitResponse(**result)
    except ImportError:
        # Service not yet implemented - return stub response
        from zerg.config import get_settings

        settings = get_settings()
        jobs_dir = getattr(settings, "jobs_dir", "/data/jobs")

        return JobsRepoInitResponse(
            success=False,
            message="Jobs repo service not yet implemented",
            jobs_dir=jobs_dir,
        )
    except Exception as e:
        logger.error("Failed to initialize jobs repo: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to initialize jobs repo: {e}")


@router.post("/repo/sync", response_model=JobsRepoSyncResponse)
async def sync_jobs_repo(
    current_user: UserModel = Depends(require_admin),
):
    """Sync the jobs repository with its remote.

    If a remote is configured, this will:
    - Pull changes from remote
    - Push local commits to remote
    - Handle merge conflicts (if any)

    Note: Remote sync is optional. Jobs work fine with local-only versioning.

    Currently returns not_implemented as remote sync is not yet built.
    """
    try:
        from zerg.services.jobs_repo import sync_jobs_repo as _sync_repo

        result = _sync_repo()
        if result.get("status") != "not_implemented":
            logger.info("Jobs repo synced by user %s", current_user.email)
        return JobsRepoSyncResponse(**result)
    except ImportError:
        # Service not yet implemented
        return JobsRepoSyncResponse(
            success=False,
            message="Remote sync not yet implemented. Jobs work fine with local-only versioning.",
            status="not_implemented",
        )
    except Exception as e:
        logger.error("Failed to sync jobs repo: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to sync jobs repo: {e}")


# ---------------------------------------------------------------------------
# Job Run History Endpoints (must come before /{job_id} to avoid shadowing)
# ---------------------------------------------------------------------------


def _isoformat_utc(dt: object) -> str | None:
    """Format a datetime as ISO string with Z suffix for naive UTC datetimes."""
    if dt is None:
        return None
    from datetime import datetime as _dt

    if isinstance(dt, _dt):
        iso = dt.isoformat()
        return (iso + "Z") if dt.tzinfo is None else iso
    return None


def _job_run_to_info(run: JobRun) -> JobRunHistoryInfo:
    """Convert a JobRun ORM instance to a JobRunHistoryInfo schema."""
    return JobRunHistoryInfo(
        id=run.id,
        job_id=run.job_id,
        status=run.status,
        started_at=_isoformat_utc(run.started_at),
        finished_at=_isoformat_utc(run.finished_at),
        duration_ms=run.duration_ms,
        error_message=run.error_message,
        error_type=getattr(run, "error_type", None),
        created_at=_isoformat_utc(run.created_at) or "",
    )


@router.get("/runs/recent", response_model=JobRunHistoryResponse)
async def get_recent_job_runs(
    limit: int = Query(25, ge=1, le=100, description="Max results to return"),
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get recent job runs across all jobs (for dashboard).

    Returns the most recent runs ordered by created_at descending.
    """
    total = db.query(sa_func.count(JobRun.id)).scalar() or 0
    runs = db.query(JobRun).order_by(JobRun.created_at.desc()).limit(limit).all()
    return JobRunHistoryResponse(
        runs=[_job_run_to_info(r) for r in runs],
        total=total,
    )


@router.get("/runs/last", response_model=JobLastRunResponse)
async def get_last_job_runs(
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get the most recent run for each job.

    Returns a dict mapping job_id to its latest JobRunHistoryInfo.
    More accurate than the capped /runs/recent for the "Last Run" column.
    """
    from sqlalchemy import distinct

    # Get all unique job_ids
    job_ids = [row[0] for row in db.query(distinct(JobRun.job_id)).all()]

    last_runs: dict[str, JobRunHistoryInfo] = {}
    for jid in job_ids:
        run = db.query(JobRun).filter(JobRun.job_id == jid).order_by(JobRun.created_at.desc()).first()
        if run:
            last_runs[jid] = _job_run_to_info(run)

    return JobLastRunResponse(last_runs=last_runs)


@router.get("/", response_model=JobListResponse)
async def list_jobs(
    enabled_only: bool = False,
    current_user: UserModel = Depends(require_admin),
):
    """List all registered scheduled jobs.

    Args:
        enabled_only: If True, only return enabled jobs
    """
    await _ensure_jobs_registered()

    jobs = job_registry.list_jobs(enabled_only=enabled_only)
    job_infos = [_job_info_from_config(j) for j in jobs]

    return JobListResponse(jobs=job_infos, total=len(job_infos))


@router.get("/{job_id}", response_model=JobInfo)
async def get_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Get details for a specific job."""
    await _ensure_jobs_registered()

    config = job_registry.get(job_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return _job_info_from_config(config)


@router.get("/{job_id}/runs", response_model=JobRunHistoryResponse)
async def get_job_runs(
    job_id: str,
    limit: int = Query(25, ge=1, le=100, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get run history for a specific job.

    Returns runs for the given job ordered by created_at descending,
    with pagination support via limit/offset.
    """
    total = db.query(sa_func.count(JobRun.id)).filter(JobRun.job_id == job_id).scalar() or 0
    runs = db.query(JobRun).filter(JobRun.job_id == job_id).order_by(JobRun.created_at.desc()).offset(offset).limit(limit).all()
    return JobRunHistoryResponse(
        runs=[_job_run_to_info(r) for r in runs],
        total=total,
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
    """
    await _ensure_jobs_registered()

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


def _check_required_secrets(job_id: str, owner_id: int, db: Session) -> list[str]:
    """Check which required secrets are missing for a job.

    Returns a list of missing required secret keys (empty = all good).
    """
    config = job_registry.get(job_id)
    if not config:
        return []

    normalized = _normalize_secret_fields(config.secrets)
    required_keys = [sf["key"] for sf in normalized if sf.get("required", True)]
    if not required_keys:
        return []

    # Check DB for configured secrets
    db_keys: set[str] = set()
    rows = (
        db.query(JobSecret.key)
        .filter(
            JobSecret.owner_id == owner_id,
            JobSecret.key.in_(required_keys),
        )
        .all()
    )
    db_keys = {row.key for row in rows}

    # Check env vars as fallback
    missing = []
    for key in required_keys:
        if key not in db_keys and not os.environ.get(key):
            missing.append(key)

    return missing


@router.post("/{job_id}/enable", response_model=JobInfo)
async def enable_job(
    job_id: str,
    force: bool = Query(False, description="Bypass secret checks"),
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Enable a job for scheduled execution.

    Checks that all required secrets are configured before enabling.
    Returns 409 Conflict if required secrets are missing (use force=true to bypass).
    """
    await _ensure_jobs_registered()

    config = job_registry.get(job_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if not force:
        missing = _check_required_secrets(job_id, current_user.id, db)
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Missing required secrets",
                    "missing": missing,
                },
            )

    if not job_registry.enable(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if force:
        logger.warning("Job force-enabled with potential missing secrets: %s (by user %s)", job_id, current_user.email)
    else:
        logger.info("Job enabled: %s (by user %s)", job_id, current_user.email)

    return _job_info_from_config(config)


@router.post("/{job_id}/disable", response_model=JobInfo)
async def disable_job(
    job_id: str,
    current_user: UserModel = Depends(require_admin),
):
    """Disable a job from scheduled execution."""
    await _ensure_jobs_registered()

    if not job_registry.disable(job_id):
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    config = job_registry.get(job_id)
    logger.info("Job disabled: %s (by user %s)", job_id, current_user.email)

    return _job_info_from_config(config)


# Queue state endpoint schemas
class QueueEntryInfo(BaseModel):
    """Queue entry information for API responses."""

    id: str
    job_id: str
    status: str
    scheduled_for: str
    attempts: int
    max_attempts: int
    lease_owner: str | None
    last_error: str | None
    created_at: str
    finished_at: str | None


class QueueStateResponse(BaseModel):
    """Response for queue state query."""

    entries: list[QueueEntryInfo]
    total: int
    queue_enabled: bool


@router.get("/queue/state", response_model=QueueStateResponse)
async def get_queue_state(
    limit: int = 20,
    current_user: UserModel = Depends(require_admin),
):
    """Get recent queue entries (admin only).

    Returns recent entries from the job queue for debugging.
    Queue must be enabled (JOB_QUEUE_ENABLED=1) for entries to exist.
    """
    from zerg.jobs.ops_db import is_job_queue_db_enabled

    queue_enabled = is_job_queue_db_enabled()

    if not queue_enabled:
        return QueueStateResponse(
            entries=[],
            total=0,
            queue_enabled=False,
        )

    try:
        from zerg.jobs.queue import get_recent_queue_entries

        rows = await get_recent_queue_entries(limit)
        entries = [
            QueueEntryInfo(
                id=str(row["id"]),
                job_id=row["job_id"],
                status=row["status"],
                scheduled_for=_isoformat_utc(row["scheduled_for"]) or "",
                attempts=row["attempts"],
                max_attempts=row["max_attempts"],
                lease_owner=row["lease_owner"],
                last_error=row["last_error"],
                created_at=_isoformat_utc(row["created_at"]) or "",
                finished_at=_isoformat_utc(row["finished_at"]),
            )
            for row in rows
        ]

        return QueueStateResponse(
            entries=entries,
            total=len(entries),
            queue_enabled=True,
        )
    except Exception as e:
        logger.error("Failed to fetch queue state: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch queue state: {e}")
