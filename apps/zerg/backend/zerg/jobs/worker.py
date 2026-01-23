"""Queue worker that executes jobs from durable queue.

This module provides a background worker loop that:
- Claims jobs from the queue with lease-based locking
- Executes jobs with heartbeat to extend leases
- Handles retries with exponential backoff
- Backfills missed runs on startup
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from zerg.jobs.ops_db import emit_job_run
from zerg.jobs.ops_db import is_job_queue_db_enabled
from zerg.jobs.queue import DEFAULT_POLL_SECONDS
from zerg.jobs.queue import QueueJob
from zerg.jobs.queue import QueueOwner
from zerg.jobs.queue import backfill_start
from zerg.jobs.queue import claim_job_by_id
from zerg.jobs.queue import claim_next_job
from zerg.jobs.queue import cleanup_zombies
from zerg.jobs.queue import complete_job
from zerg.jobs.queue import default_owner
from zerg.jobs.queue import enqueue_job
from zerg.jobs.queue import extend_lease
from zerg.jobs.queue import get_last_scheduled_for
from zerg.jobs.queue import make_dedupe_key
from zerg.jobs.queue import reschedule_job
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunResult:
    """Result of a job execution."""

    status: str
    error: str | None = None


def _lease_seconds(timeout_seconds: int) -> int:
    """Calculate lease duration based on job timeout."""
    base = max(timeout_seconds * 2, 300)
    return min(base, 6 * 60 * 60)  # Cap at 6 hours


def _retry_delay(attempt: int) -> int:
    """Calculate retry delay with exponential backoff."""
    # exponential backoff: 1m, 2m, 4m, 8m... capped at 1h
    return min(60 * (2 ** max(attempt - 1, 0)), 3600)


async def enqueue_missed_runs(now: datetime | None = None) -> None:
    """Backfill missed runs based on last scheduled_for timestamps.

    Only enqueues the MOST RECENT missed run per job to avoid flooding
    the queue with stale runs.
    """
    if not is_job_queue_db_enabled():
        logger.info("Job queue disabled (JOB_QUEUE_ENABLED=0)")
        return

    # Also clean up any zombie jobs from previous crashes
    try:
        zombies = await cleanup_zombies()
        if zombies:
            logger.warning(f"Cleaned up {zombies} zombie job(s)")
    except Exception as e:
        logger.error(f"Zombie cleanup failed: {e}")

    now = now or datetime.now(UTC)

    for job in job_registry.list_jobs(enabled_only=True):
        try:
            last_scheduled = await get_last_scheduled_for(job.id)
        except Exception as e:
            logger.error(f"Queue backfill failed for {job.id}: {e}")
            continue

        start = last_scheduled or backfill_start(now)
        if last_scheduled:
            start = start + timedelta(seconds=1)

        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(job.cron)

        # Find the most recent missed fire time (not all of them)
        prev = None
        scheduled = trigger.get_next_fire_time(prev, start)
        most_recent = None

        while scheduled and scheduled <= now:
            most_recent = scheduled
            prev = scheduled
            scheduled = trigger.get_next_fire_time(prev, now)

        # Only enqueue the most recent missed run
        if most_recent:
            dedupe_key = make_dedupe_key(job.id, most_recent)
            queue_id = await enqueue_job(
                job_id=job.id,
                scheduled_for=most_recent,
                dedupe_key=dedupe_key,
                max_attempts=job.max_attempts,
            )
            if queue_id:
                logger.info(f"Backfilled 1 run for {job.id} (scheduled: {most_recent})")


async def enqueue_scheduled_run(job_id: str, scheduled_at: datetime | None = None) -> None:
    """Enqueue a scheduled job run.

    CRITICAL: Use the actual scheduled fire time (from CronTrigger), NOT datetime.now().
    Using "now" breaks dedupe on backfill and causes duplicate runs.
    """
    job = job_registry.get(job_id)
    if not job:
        logger.error(f"enqueue_scheduled_run: unknown job {job_id}")
        return

    now = datetime.now(UTC)

    # If scheduled_at not provided, calculate from cron
    if scheduled_at is None:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(job.cron)
        window_start = now - timedelta(hours=2)
        prev = None
        candidate = trigger.get_next_fire_time(prev, window_start)
        while candidate and candidate <= now:
            scheduled_at = candidate
            prev = candidate
            candidate = trigger.get_next_fire_time(prev, now)

    scheduled_at = scheduled_at or now
    dedupe_key = make_dedupe_key(job_id, scheduled_at)
    queue_id = await enqueue_job(
        job_id=job_id,
        scheduled_for=scheduled_at,
        dedupe_key=dedupe_key,
        max_attempts=job.max_attempts,
    )
    if queue_id:
        logger.info(f"Enqueued {job_id} ({queue_id})")


async def run_queue_worker(poll_seconds: float | None = None) -> None:
    """Main worker loop: claim and execute jobs."""
    owner = default_owner()
    poll_seconds = poll_seconds or DEFAULT_POLL_SECONDS

    logger.info(f"Job queue worker started as {owner.name}")

    while True:
        try:
            job = await claim_next_job(owner)
            if not job:
                await asyncio.sleep(poll_seconds)
                continue

            await _run_job(job, owner)

        except Exception as e:
            logger.error(f"Worker loop error: {e}", exc_info=True)
            await asyncio.sleep(poll_seconds)


async def run_queue_job(queue_id: str, owner: QueueOwner | None = None) -> RunResult:
    """Claim and run a specific queued job (used by API trigger)."""
    owner = owner or default_owner()
    claimed = await claim_job_by_id(queue_id, owner)
    if not claimed:
        return RunResult(status="failed", error="Queue item not claimable")

    try:
        await _run_job(claimed, owner)
    except Exception as e:
        return RunResult(status="failed", error=str(e))

    return RunResult(status="success")


async def _run_job(queue_job: QueueJob, owner: QueueOwner) -> None:
    """Execute a single job with heartbeat and error handling."""
    job_def = job_registry.get(queue_job.job_id)
    if not job_def or not job_def.enabled:
        await complete_job(queue_job.id, "dead", "Job disabled or missing", owner=owner)
        return

    started_at = datetime.now(UTC)
    lease_seconds = _lease_seconds(job_def.timeout_seconds)

    # Extend lease before execution
    if not await extend_lease(queue_job.id, owner, lease_seconds):
        logger.error("Lost lease before execution for %s (%s)", queue_job.job_id, queue_job.id)
        return

    stop_heartbeat = asyncio.Event()

    async def heartbeat():
        """Background task to extend lease periodically."""
        while not stop_heartbeat.is_set():
            await asyncio.sleep(min(lease_seconds / 2, 60))
            ok = await extend_lease(queue_job.id, owner, lease_seconds)
            if not ok:
                logger.error(
                    "Lease lost during execution for %s (%s)",
                    queue_job.job_id,
                    queue_job.id,
                )
                stop_heartbeat.set()

    hb_task = asyncio.create_task(heartbeat())

    status = "success"
    error_text = None

    try:
        # Execute the job function with timeout
        await asyncio.wait_for(
            job_def.func(),
            timeout=job_def.timeout_seconds,
        )
        status = "success"
    except asyncio.TimeoutError:
        status = "failure"
        error_text = f"Job timed out after {job_def.timeout_seconds}s"
        logger.error("Job %s timed out", queue_job.job_id)
    except Exception as e:
        status = "failure"
        error_text = str(e)
        logger.exception("Job %s failed: %s", queue_job.job_id, e)
    finally:
        stop_heartbeat.set()
        hb_task.cancel()

    ended_at = datetime.now(UTC)
    duration_ms = int((ended_at - started_at).total_seconds() * 1000)

    # Emit to ops.runs
    try:
        await emit_job_run(
            job_id=queue_job.job_id,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            error_message=error_text,
            tags=job_def.tags,
            project=job_def.project,
            scheduler="zerg",
        )
    except Exception as e:
        logger.error("Failed to emit job run: %s", e)

    # Update queue status
    if status == "success":
        if not await complete_job(queue_job.id, "success", None, owner=owner):
            logger.error("Failed to mark job success (lease lost): %s", queue_job.id)
    else:
        if queue_job.attempts >= queue_job.max_attempts:
            if not await complete_job(queue_job.id, "dead", error_text[:5000] if error_text else None, owner=owner):
                logger.error("Failed to mark job dead (lease lost): %s", queue_job.id)
        else:
            retry_at = datetime.now(UTC) + timedelta(seconds=_retry_delay(queue_job.attempts))
            if not await reschedule_job(queue_job.id, retry_at, error_text[:5000] if error_text else None, owner=owner):
                logger.error("Failed to reschedule job (lease lost): %s", queue_job.id)
