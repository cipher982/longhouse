"""Job registry for scheduled jobs.

Provides a centralized registry for all scheduled jobs, with:
- Job configuration (cron, timeout, retries)
- Automatic registration with APScheduler
- Error handling and status tracking
- Life Hub integration for run history
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Awaitable
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


@dataclass
class JobConfig:
    """Configuration for a scheduled job."""

    id: str  # Unique job identifier (e.g., "backup-sentinel")
    cron: str  # Cron expression (e.g., "0 10 * * *")
    func: Callable[[], Awaitable[dict[str, Any]]]  # Async function to execute
    enabled: bool = True
    timeout_seconds: int = 300  # Default 5 minutes
    max_attempts: int = 3
    tags: list[str] = field(default_factory=list)
    project: str | None = None  # Project this job belongs to
    description: str = ""


@dataclass
class JobRunResult:
    """Result of a job execution."""

    job_id: str
    status: str  # "success", "failure", "timeout"
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    result: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None


class JobRegistry:
    """Registry for scheduled jobs."""

    def __init__(self):
        self._jobs: dict[str, JobConfig] = {}
        self._scheduler: AsyncIOScheduler | None = None

    def register(self, config: JobConfig) -> None:
        """Register a job configuration."""
        if config.id in self._jobs:
            logger.warning("Job %s already registered, overwriting", config.id)
        self._jobs[config.id] = config
        logger.info("Registered job: %s (cron=%s, enabled=%s)", config.id, config.cron, config.enabled)

    def get(self, job_id: str) -> JobConfig | None:
        """Get a job configuration by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self, enabled_only: bool = False) -> list[JobConfig]:
        """List all registered jobs."""
        jobs = list(self._jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        return jobs

    def enable(self, job_id: str) -> bool:
        """Enable a job. Returns True if found."""
        if job_id in self._jobs:
            self._jobs[job_id].enabled = True
            return True
        return False

    def disable(self, job_id: str) -> bool:
        """Disable a job. Returns True if found."""
        if job_id in self._jobs:
            self._jobs[job_id].enabled = False
            return True
        return False

    async def run_job(self, job_id: str) -> JobRunResult:
        """Execute a job immediately with retry support.

        Handles:
        - Timeout enforcement
        - Error capture
        - Status tracking
        - Automatic retries based on max_attempts config
        """
        config = self._jobs.get(job_id)
        if not config:
            return JobRunResult(
                job_id=job_id,
                status="failure",
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                duration_ms=0,
                error=f"Job {job_id} not found",
                error_type="NotFoundError",
            )

        started_at = datetime.now(UTC)
        status = "success"
        result = None
        error = None
        error_type = None
        attempts = 0
        max_attempts = config.max_attempts

        while attempts < max_attempts:
            attempts += 1
            try:
                # Execute with timeout
                result = await asyncio.wait_for(
                    config.func(),
                    timeout=config.timeout_seconds,
                )
                # Success - break out of retry loop
                status = "success"
                error = None
                error_type = None
                break
            except asyncio.TimeoutError:
                status = "timeout"
                error = f"Job exceeded {config.timeout_seconds}s timeout (attempt {attempts}/{max_attempts})"
                error_type = "TimeoutError"
                logger.error("Job %s timed out after %ds (attempt %d/%d)", job_id, config.timeout_seconds, attempts, max_attempts)
            except Exception as e:
                status = "failure"
                error = f"{str(e)[:5000]} (attempt {attempts}/{max_attempts})"
                error_type = type(e).__name__
                logger.exception("Job %s failed (attempt %d/%d): %s", job_id, attempts, max_attempts, e)

            # If we haven't exhausted retries and failed, wait before retry
            if attempts < max_attempts and status != "success":
                # Exponential backoff: 2^attempt seconds (2, 4, 8, ...)
                backoff = min(2**attempts, 30)  # Cap at 30 seconds
                logger.info("Retrying job %s in %ds (attempt %d/%d)", job_id, backoff, attempts + 1, max_attempts)
                await asyncio.sleep(backoff)

        ended_at = datetime.now(UTC)
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)

        run_result = JobRunResult(
            job_id=job_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            result=result,
            error=error,
            error_type=error_type,
        )

        # Ship to Life Hub (fire-and-forget)
        await self._ship_to_lifehub(run_result, config)

        return run_result

    async def _ship_to_lifehub(self, result: JobRunResult, config: JobConfig) -> None:
        """Ship job run result to Life Hub for history tracking."""
        try:
            import httpx

            from zerg.config import get_settings

            settings = get_settings()
            if settings.testing or not settings.lifehub_url:
                return

            payload = {
                "job_key": f"zerg:{result.job_id}",
                "job_id": result.job_id,
                "scheduler": "zerg",
                "status": result.status,
                "started_at": result.started_at.isoformat(),
                "ended_at": result.ended_at.isoformat(),
                "duration_ms": result.duration_ms,
                "error_message": result.error,
                "error_type": result.error_type,
                "tags": config.tags,
                "project": config.project,
                "metadata": result.result,
            }

            headers = {}
            if settings.lifehub_api_key:
                headers["X-API-Key"] = settings.lifehub_api_key

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{settings.lifehub_url}/ingest/jobs/run",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()

            logger.debug("Shipped job run %s to Life Hub", result.job_id)

        except Exception as e:
            logger.error("Failed to ship job run to Life Hub: %s", e)

    def schedule_all(self, scheduler: AsyncIOScheduler) -> int:
        """Schedule all enabled jobs with APScheduler.

        Returns count of jobs scheduled.
        """
        self._scheduler = scheduler
        count = 0

        for config in self._jobs.values():
            if not config.enabled:
                continue

            try:
                # Wrap the run_job call for APScheduler
                async def job_wrapper(job_id: str = config.id) -> None:
                    await self.run_job(job_id)

                scheduler.add_job(
                    job_wrapper,
                    CronTrigger.from_crontab(config.cron),
                    id=f"job_{config.id}",
                    replace_existing=True,
                )
                count += 1
                logger.info("Scheduled job %s with cron: %s", config.id, config.cron)

            except Exception as e:
                logger.error("Failed to schedule job %s: %s", config.id, e)

        return count


# Global job registry
job_registry = JobRegistry()


def register_all_jobs(scheduler: AsyncIOScheduler | None = None) -> int:
    """Register and schedule all jobs.

    Call this during startup to:
    1. Import job modules (which register their configs)
    2. Optionally schedule all enabled jobs

    Returns count of jobs scheduled.
    """
    # Import job modules to trigger registration
    # pylint: disable=import-outside-toplevel,unused-import
    try:
        from zerg.jobs.backups import backup_sentinel  # noqa: F401
    except ImportError as e:
        logger.warning("Could not import backup jobs: %s", e)

    try:
        from zerg.jobs.monitoring import disk_health  # noqa: F401
    except ImportError as e:
        logger.warning("Could not import monitoring jobs: %s", e)

    if scheduler:
        return job_registry.schedule_all(scheduler)

    return 0


__all__ = [
    "JobConfig",
    "JobRegistry",
    "JobRunResult",
    "job_registry",
    "register_all_jobs",
]
