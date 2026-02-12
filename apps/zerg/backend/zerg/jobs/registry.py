"""Job registry for scheduled jobs.

Provides a centralized registry for all scheduled jobs, with:
- Job configuration (cron, timeout, retries)
- Automatic registration with APScheduler
- Error handling and status tracking
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Required
from typing import TypedDict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class SecretField(TypedDict, total=False):
    """Rich metadata for a job secret declaration.

    Mirrors the CredentialField pattern from connectors/registry.py.
    Plain strings in JobConfig.secrets are auto-normalized to SecretField(key=...).
    """

    key: Required[str]  # env var name, e.g. "LIFE_HUB_DB_URL"
    label: str  # human-readable, e.g. "Life Hub Database URL"
    type: str  # input type: "password" | "text" | "url" (default "password")
    placeholder: str  # hint, e.g. "postgresql://..."
    description: str  # longer help text
    required: bool  # default True


def _normalize_secret_fields(raw: list[str | SecretField]) -> list[SecretField]:
    """Normalize mixed list to list of SecretField dicts.

    Skips malformed entries (dicts missing 'key') with a warning.
    """
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(SecretField(key=item))
        elif isinstance(item, dict) and "key" in item:
            result.append(item)
        else:
            logger.warning("Skipping malformed secret entry: %s", item)
    return result


def _extract_secret_keys(raw: list[str | SecretField]) -> list[str]:
    """Extract just the key names from a mixed secrets list.

    Skips malformed entries (dicts missing 'key') with a warning.
    """
    keys = []
    for item in raw:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and "key" in item:
            keys.append(item["key"])
        else:
            logger.warning("Skipping malformed secret entry: %s", item)
    return keys


def _invoke_job_func(config: JobConfig) -> Awaitable[dict[str, Any]]:
    """Invoke job func with or without JobContext based on signature.

    - ``run()`` (zero params): legacy style, called with no args.
    - ``run(ctx)`` (one+ params): new style, receives a ``JobContext``
      with only the declared secrets injected.
    """
    sig = inspect.signature(config.func)
    if sig.parameters:
        # New-style: inject JobContext with declared secrets
        from zerg.database import db_session
        from zerg.jobs.context import JobContext
        from zerg.jobs.secret_resolver import resolve_secrets

        with db_session() as db:
            # For direct (non-queue) execution, use owner_id=1 (single-tenant default)
            secrets = resolve_secrets(owner_id=1, declared_keys=_extract_secret_keys(config.secrets), db=db)

        ctx = JobContext(job_id=config.id, secrets=secrets)
        return config.func(ctx)
    else:
        # Legacy: zero-arg, uses os.environ directly
        return config.func()


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
    queue_mode: bool = True  # Use durable queue (False = direct execution for debugging)
    secrets: list[str | SecretField] = field(default_factory=list)  # Declared secret keys (str or rich SecretField)


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
        self._use_queue: bool = False

    def snapshot_jobs(self) -> dict[str, str]:
        """Snapshot current job IDs and cron expressions for diffing.

        Returns:
            Dict mapping job_id -> cron expression.
        """
        return {job_id: config.cron for job_id, config in self._jobs.items()}

    def clear_manifest_jobs(self, builtin_job_ids: set[str]) -> set[str]:
        """Remove all jobs that aren't builtin (i.e., from manifest).

        Args:
            builtin_job_ids: Set of job IDs to preserve (builtin jobs).

        Returns:
            Set of job IDs that were removed.
        """
        removed = set()
        for job_id in list(self._jobs.keys()):
            if job_id not in builtin_job_ids:
                del self._jobs[job_id]
                removed.add(job_id)
        return removed

    def unregister(self, job_id: str) -> bool:
        """Unregister a job from the registry.

        Returns:
            True if job was found and removed, False otherwise.
        """
        if job_id in self._jobs:
            del self._jobs[job_id]
            logger.info("Unregistered job: %s", job_id)
            return True
        return False

    def register(self, config: JobConfig) -> bool:
        """Register a job configuration.

        Duplicate job IDs are skipped with a warning (not fatal).
        This allows manifest reloads and prevents partial registration failures.

        Returns:
            True if registered, False if skipped (duplicate).
        """
        if config.id in self._jobs:
            logger.warning("Job %s already registered, skipping duplicate", config.id)
            return False
        self._jobs[config.id] = config
        logger.info("Registered job: %s (cron=%s, enabled=%s)", config.id, config.cron, config.enabled)
        return True

    def get(self, job_id: str) -> JobConfig | None:
        """Get a job configuration by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self, enabled_only: bool = False) -> list[JobConfig]:
        """List all registered jobs."""
        jobs = list(self._jobs.values())
        if enabled_only:
            jobs = [j for j in jobs if j.enabled]
        return jobs

    def _try_auto_commit(self, context: str = "job-state-change") -> None:
        """Attempt to auto-commit changes to jobs repo. Failures are logged but not raised."""
        try:
            from zerg.config import get_settings
            from zerg.services.jobs_repo import auto_commit_if_dirty

            settings = get_settings()
            auto_commit_if_dirty(Path(settings.data_dir), context=context)
        except Exception:
            logger.warning("Auto-commit failed", exc_info=True)

    def enable(self, job_id: str) -> bool:
        """Enable a job. Returns True if found."""
        if job_id in self._jobs:
            self._jobs[job_id].enabled = True
            self._try_auto_commit()
            return True
        return False

    def disable(self, job_id: str) -> bool:
        """Disable a job. Returns True if found."""
        if job_id in self._jobs:
            self._jobs[job_id].enabled = False
            self._try_auto_commit()
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
                # Execute with timeout, dispatching based on function signature
                result = await asyncio.wait_for(
                    _invoke_job_func(config),
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

        return JobRunResult(
            job_id=job_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            result=result,
            error=error,
            error_type=error_type,
        )

    def _schedule_job(self, config: JobConfig) -> bool:
        """Schedule a single job with the stored scheduler.

        Returns:
            True if scheduled successfully, False otherwise.
        """
        if not self._scheduler:
            logger.error("No scheduler available to schedule job %s", config.id)
            return False

        if not config.enabled:
            logger.debug("Skipping disabled job %s", config.id)
            return False

        try:
            job_uses_queue = self._use_queue and config.queue_mode

            if job_uses_queue:

                async def queue_wrapper(job_id: str = config.id) -> None:
                    from zerg.jobs.commis import enqueue_scheduled_run

                    await enqueue_scheduled_run(job_id)

                self._scheduler.add_job(
                    queue_wrapper,
                    CronTrigger.from_crontab(config.cron),
                    id=f"job_{config.id}",
                    replace_existing=True,
                )
                logger.info("Scheduled job %s with cron: %s (queue mode)", config.id, config.cron)
            else:

                async def job_wrapper(job_id: str = config.id) -> None:
                    await self.run_job(job_id)

                self._scheduler.add_job(
                    job_wrapper,
                    CronTrigger.from_crontab(config.cron),
                    id=f"job_{config.id}",
                    replace_existing=True,
                )
                logger.info("Scheduled job %s with cron: %s (direct mode)", config.id, config.cron)

            return True

        except Exception as e:
            logger.error("Failed to schedule job %s: %s", config.id, e)
            return False

    def _unschedule_job(self, job_id: str) -> bool:
        """Remove a job from the scheduler.

        Returns:
            True if removed successfully, False otherwise.
        """
        if not self._scheduler:
            return False

        scheduler_job_id = f"job_{job_id}"
        try:
            self._scheduler.remove_job(scheduler_job_id)
            logger.info("Unscheduled job %s", job_id)
            return True
        except Exception:
            # Job may not exist in scheduler
            logger.debug("Job %s not found in scheduler (may not have been scheduled)", job_id)
            return False

    def schedule_all(self, scheduler: AsyncIOScheduler, use_queue: bool = False) -> int:
        """Schedule all enabled jobs with APScheduler.

        Args:
            scheduler: APScheduler instance
            use_queue: If True, enqueue jobs to durable queue instead of running directly

        Returns count of jobs scheduled.
        """
        self._scheduler = scheduler
        self._use_queue = use_queue
        count = 0

        for config in self._jobs.values():
            if self._schedule_job(config):
                count += 1

        return count

    def sync_jobs(self, old_snapshot: dict[str, str]) -> dict[str, int]:
        """Sync scheduler with registry after manifest reload.

        Compares current registry state against old snapshot and:
        - Removes jobs that no longer exist
        - Reschedules jobs with changed cron expressions
        - Adds jobs that are new

        Args:
            old_snapshot: Dict from snapshot_jobs() taken before manifest reload.

        Returns:
            Dict with counts: {"added": N, "removed": N, "rescheduled": N}
        """
        if not self._scheduler:
            logger.warning("No scheduler available for sync")
            return {"added": 0, "removed": 0, "rescheduled": 0}

        current_jobs = self._jobs
        old_job_ids = set(old_snapshot.keys())
        new_job_ids = set(current_jobs.keys())

        # Find differences
        removed_ids = old_job_ids - new_job_ids
        added_ids = new_job_ids - old_job_ids
        common_ids = old_job_ids & new_job_ids

        # Check for cron changes in common jobs
        rescheduled_ids = set()
        for job_id in common_ids:
            old_cron = old_snapshot[job_id]
            new_cron = current_jobs[job_id].cron
            if old_cron != new_cron:
                rescheduled_ids.add(job_id)
                logger.info("Job %s cron changed: %s -> %s", job_id, old_cron, new_cron)

        # Remove deleted jobs from scheduler
        for job_id in removed_ids:
            self._unschedule_job(job_id)
            logger.info("Removed job %s (no longer in manifest)", job_id)

        # Reschedule jobs with changed cron
        for job_id in rescheduled_ids:
            config = current_jobs[job_id]
            self._schedule_job(config)  # replace_existing=True handles the reschedule

        # Add new jobs
        for job_id in added_ids:
            config = current_jobs[job_id]
            self._schedule_job(config)
            logger.info("Added new job %s", job_id)

        return {
            "added": len(added_ids),
            "removed": len(removed_ids),
            "rescheduled": len(rescheduled_ids),
        }


# Global job registry
job_registry = JobRegistry()


async def register_all_jobs(scheduler: AsyncIOScheduler | None = None, use_queue: bool = False) -> int:
    """Register and schedule all jobs.

    Call this during startup to:
    1. Import builtin job modules (which register their configs)
    2. Load external jobs from git manifest (if configured)
    3. Optionally schedule all enabled jobs

    Args:
        scheduler: APScheduler instance (if None, only registers jobs without scheduling)
        use_queue: If True and JOB_QUEUE_ENABLED, enqueue jobs to durable queue

    Returns count of jobs scheduled.
    """
    # Load external jobs from git manifest (if configured)
    # Wrapped in try/except so manifest failures don't block builtin jobs
    try:
        from zerg.jobs.loader import load_jobs_manifest

        await load_jobs_manifest()
    except Exception as e:
        logger.exception("Manifest load failed (builtin jobs remain active): %s", e)

    if scheduler:
        return job_registry.schedule_all(scheduler, use_queue=use_queue)

    return 0


__all__ = [
    "JobConfig",
    "JobRegistry",
    "JobRunResult",
    "SecretField",
    "job_registry",
    "register_all_jobs",
    "_invoke_job_func",
    "_normalize_secret_fields",
    "_extract_secret_keys",
]
