"""Sauron API for Jarvis control.

Minimal FastAPI endpoints for:
- Health checks
- Job status queries
- Manual job triggers
- Git sync control
"""

import logging
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Sauron API",
    description="Centralized ops scheduler - control plane for Jarvis",
    version="2.0.0",
)


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str
    timestamp: str


class JobInfo(BaseModel):
    """Job information."""

    id: str
    cron: str
    enabled: bool
    timeout_seconds: int
    tags: list[str]
    project: str | None
    description: str


class JobListResponse(BaseModel):
    """List of jobs response."""

    jobs: list[JobInfo]
    count: int


class StatusResponse(BaseModel):
    """Scheduler status response."""

    scheduler_running: bool
    jobs_count: int
    git_sync_status: dict | None
    next_runs: list[dict]


class TriggerResponse(BaseModel):
    """Job trigger response."""

    job_id: str
    queued: bool
    queue_id: str | None
    message: str


class SyncResponse(BaseModel):
    """Git sync response."""

    success: bool
    message: str
    sha: str | None


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    from sauron import __version__

    return HealthResponse(
        status="healthy",
        version=__version__,
        timestamp=datetime.now(UTC).isoformat(),
    )


@app.get("/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    """Get scheduler status and next runs."""
    from zerg.jobs import get_git_sync_service, job_registry

    # Get git sync status
    git_service = get_git_sync_service()
    git_status = git_service.get_status() if git_service else None

    # Get jobs count
    jobs = job_registry.list_jobs(enabled_only=True)

    # Build next runs list (would need scheduler reference for real times)
    # For now, just return job cron schedules
    next_runs = []
    for job in jobs[:10]:  # Limit to 10
        next_runs.append({
            "job_id": job.id,
            "cron": job.cron,
        })

    return StatusResponse(
        scheduler_running=True,
        jobs_count=len(jobs),
        git_sync_status=git_status,
        next_runs=next_runs,
    )


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(enabled_only: bool = False) -> JobListResponse:
    """List all registered jobs."""
    from zerg.jobs import job_registry

    jobs = job_registry.list_jobs(enabled_only=enabled_only)

    return JobListResponse(
        jobs=[
            JobInfo(
                id=job.id,
                cron=job.cron,
                enabled=job.enabled,
                timeout_seconds=job.timeout_seconds,
                tags=job.tags,
                project=job.project,
                description=job.description,
            )
            for job in jobs
        ],
        count=len(jobs),
    )


@app.get("/jobs/{job_id}", response_model=JobInfo)
async def get_job(job_id: str) -> JobInfo:
    """Get details for a specific job."""
    from zerg.jobs import job_registry

    job = job_registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return JobInfo(
        id=job.id,
        cron=job.cron,
        enabled=job.enabled,
        timeout_seconds=job.timeout_seconds,
        tags=job.tags,
        project=job.project,
        description=job.description,
    )


@app.post("/jobs/{job_id}/trigger", response_model=TriggerResponse)
async def trigger_job(job_id: str) -> TriggerResponse:
    """Manually trigger a job.

    Enqueues the job to the durable queue for immediate execution.
    """
    from zerg.jobs import job_registry
    from zerg.jobs.queue import enqueue_job
    from zerg.jobs.queue import make_dedupe_key

    job = job_registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if not job.enabled:
        raise HTTPException(status_code=400, detail=f"Job '{job_id}' is disabled")

    # Enqueue for immediate execution
    now = datetime.now(UTC)
    dedupe_key = make_dedupe_key(job_id, now)

    try:
        queue_id = await enqueue_job(
            job_id=job_id,
            scheduled_for=now,
            dedupe_key=dedupe_key,
            max_attempts=job.max_attempts,
        )

        if queue_id:
            logger.info(f"Triggered job {job_id} -> queue_id={queue_id}")
            return TriggerResponse(
                job_id=job_id,
                queued=True,
                queue_id=queue_id,
                message=f"Job queued for immediate execution",
            )
        else:
            return TriggerResponse(
                job_id=job_id,
                queued=False,
                queue_id=None,
                message="Job already queued (dedupe prevented duplicate)",
            )

    except Exception as e:
        logger.exception(f"Failed to trigger job {job_id}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/sync", response_model=SyncResponse)
async def force_sync() -> SyncResponse:
    """Force git sync of the jobs repo and reload manifest."""
    from zerg.jobs import get_git_sync_service
    from zerg.jobs.loader import load_jobs_manifest

    git_service = get_git_sync_service()
    if not git_service:
        return SyncResponse(
            success=False,
            message="Git sync not configured (no JOBS_GIT_REPO_URL)",
            sha=None,
        )

    try:
        result = await git_service.refresh()
        # Reload manifest after sync to pick up new/changed jobs
        await load_jobs_manifest()
        logger.info(f"Git sync + manifest reload: {git_service.current_sha}")
        return SyncResponse(
            success=True,
            message=result.get("message", "Synced and reloaded"),
            sha=git_service.current_sha,
        )
    except Exception as e:
        logger.exception("Git sync failed")
        return SyncResponse(
            success=False,
            message=str(e),
            sha=None,
        )


@app.post("/jobs/{job_id}/enable")
async def enable_job(job_id: str) -> dict:
    """Enable a job."""
    from zerg.jobs import job_registry

    if job_registry.enable(job_id):
        return {"job_id": job_id, "enabled": True}
    raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")


@app.post("/jobs/{job_id}/disable")
async def disable_job(job_id: str) -> dict:
    """Disable a job."""
    from zerg.jobs import job_registry

    if job_registry.disable(job_id):
        return {"job_id": job_id, "enabled": False}
    raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
