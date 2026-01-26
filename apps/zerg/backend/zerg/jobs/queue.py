"""Durable job queue backed by Postgres (ops.job_queue).

This module provides a lease-based job queue for reliable job execution:
- Jobs are enqueued with dedupe keys to prevent duplicates
- Commis claim jobs with FOR UPDATE SKIP LOCKED for concurrency
- Leases prevent jobs from being stuck if a commis crashes
- Automatic retry with exponential backoff on failure
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from zerg.jobs.ops_db import get_job_queue_db_url
from zerg.jobs.ops_db import get_pool

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = int(os.getenv("ZERG_QUEUE_MAX_ATTEMPTS", "3"))
DEFAULT_LEASE_SECONDS = int(os.getenv("ZERG_QUEUE_LEASE_SECONDS", "900"))
DEFAULT_POLL_SECONDS = float(os.getenv("ZERG_QUEUE_POLL_SECONDS", "5"))
DEFAULT_BACKFILL_HOURS = int(os.getenv("ZERG_QUEUE_BACKFILL_HOURS", "24"))


@dataclass(frozen=True, slots=True)
class QueueJob:
    """A job in the queue."""

    id: str
    job_id: str
    status: str
    scheduled_for: datetime
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    payload: dict[str, Any] | None
    dedupe_key: str | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class QueueOwner:
    """Identity of a queue commis."""

    name: str


def default_owner() -> QueueOwner:
    """Generate a default owner name from hostname and PID."""
    return QueueOwner(name=f"{socket.gethostname()}:{os.getpid()}")


def _ensure_db() -> bool:
    """Check if job queue is enabled and DB is configured."""
    from zerg.config import get_settings

    settings = get_settings()
    if not settings.job_queue_enabled:
        logger.info("Job queue disabled (JOB_QUEUE_ENABLED=0)")
        return False
    if not get_job_queue_db_url():
        logger.error("DATABASE_URL not configured; job queue disabled")
        return False
    return True


async def enqueue_job(
    *,
    job_id: str,
    scheduled_for: datetime,
    dedupe_key: str | None,
    payload: dict[str, Any] | None = None,
    max_attempts: int | None = None,
) -> str | None:
    """Enqueue a job run. Returns queue id or None if deduped."""
    if not _ensure_db():
        return None

    queue_id = str(uuid.uuid4())
    max_attempts = max_attempts or DEFAULT_MAX_ATTEMPTS
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ops.job_queue (
                id, job_id, status, scheduled_for, attempts, max_attempts,
                dedupe_key, payload, created_at, updated_at
            ) VALUES ($1, $2, 'queued', $3, 0, $4, $5, $6, NOW(), NOW())
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """,
            queue_id,
            job_id,
            scheduled_for,
            max_attempts,
            dedupe_key,
            payload,
        )

    if not row:
        return None
    return str(row["id"])


async def claim_next_job(owner: QueueOwner, lease_seconds: int | None = None) -> QueueJob | None:
    """Claim the next queued or expired job for execution."""
    if not _ensure_db():
        return None

    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM ops.job_queue
                WHERE status IN ('queued', 'running')
                  AND scheduled_for <= NOW()
                  AND attempts < max_attempts
                  AND (
                        status = 'queued'
                        OR lease_expires_at IS NULL
                        OR lease_expires_at < NOW()
                  )
                ORDER BY scheduled_for ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE ops.job_queue
            SET status = 'running',
                lease_owner = $1,
                lease_expires_at = NOW() + ($2 || ' seconds')::interval,
                attempts = attempts + 1,
                started_at = NOW(),
                updated_at = NOW()
            WHERE id IN (SELECT id FROM candidate)
            RETURNING *
            """,
            owner.name,
            str(lease_seconds),
        )

    return _row_to_job(row) if row else None


async def claim_job_by_id(
    queue_id: str,
    owner: QueueOwner,
    lease_seconds: int | None = None,
) -> QueueJob | None:
    """Claim a specific queued job by id."""
    if not _ensure_db():
        return None

    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE ops.job_queue
            SET status = 'running',
                lease_owner = $1,
                lease_expires_at = NOW() + ($2 || ' seconds')::interval,
                attempts = attempts + 1,
                started_at = NOW(),
                updated_at = NOW()
            WHERE id = $3
              AND status = 'queued'
            RETURNING *
            """,
            owner.name,
            str(lease_seconds),
            queue_id,
        )

    return _row_to_job(row) if row else None


async def extend_lease(queue_id: str, owner: QueueOwner, lease_seconds: int) -> bool:
    """Extend lease for a running job. Returns False if lease is lost."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ops.job_queue
            SET lease_expires_at = NOW() + ($1 || ' seconds')::interval,
                updated_at = NOW()
            WHERE id = $2
              AND status = 'running'
              AND lease_owner = $3
            """,
            str(lease_seconds),
            queue_id,
            owner.name,
        )
    return _updated_rows(result) == 1


async def complete_job(
    queue_id: str,
    status: str,
    last_error: str | None = None,
    owner: QueueOwner | None = None,
) -> bool:
    """Mark job as success or failure/dead (terminal state)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if owner:
            result = await conn.execute(
                """
                UPDATE ops.job_queue
                SET status = $1,
                    last_error = $2,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = $3
                  AND status = 'running'
                  AND lease_owner = $4
                """,
                status,
                last_error,
                queue_id,
                owner.name,
            )
        else:
            result = await conn.execute(
                """
                UPDATE ops.job_queue
                SET status = $1,
                    last_error = $2,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = $3
                """,
                status,
                last_error,
                queue_id,
            )
    return _updated_rows(result) == 1


async def reschedule_job(
    queue_id: str,
    scheduled_for: datetime,
    last_error: str | None,
    owner: QueueOwner | None = None,
) -> bool:
    """Reschedule a failed job for a later retry."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if owner:
            result = await conn.execute(
                """
                UPDATE ops.job_queue
                SET status = 'queued',
                    scheduled_for = $1,
                    last_error = $2,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $3
                  AND status = 'running'
                  AND lease_owner = $4
                """,
                scheduled_for,
                last_error,
                queue_id,
                owner.name,
            )
        else:
            result = await conn.execute(
                """
                UPDATE ops.job_queue
                SET status = 'queued',
                    scheduled_for = $1,
                    last_error = $2,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $3
                """,
                scheduled_for,
                last_error,
                queue_id,
            )
    return _updated_rows(result) == 1


async def get_last_scheduled_for(job_id: str) -> datetime | None:
    """Get the most recent scheduled_for timestamp for a job."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT MAX(scheduled_for) AS last_scheduled
            FROM ops.job_queue
            WHERE job_id = $1
            """,
            job_id,
        )
    if not row or not row["last_scheduled"]:
        return None
    return row["last_scheduled"].astimezone(UTC)


async def cleanup_zombies() -> int:
    """Mark expired running jobs as dead if they exceeded max attempts."""
    if not _ensure_db():
        return 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE ops.job_queue
            SET status = 'dead',
                last_error = 'Zombie run detected (crashed during final attempt)',
                lease_owner = NULL,
                lease_expires_at = NULL,
                finished_at = NOW(),
                updated_at = NOW()
            WHERE status = 'running'
              AND lease_expires_at < NOW()
              AND attempts >= max_attempts
            """
        )
        return _updated_rows(result)


async def get_recent_queue_entries(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent queue entries for debugging."""
    if not _ensure_db():
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, job_id, status, scheduled_for, attempts, max_attempts,
                   lease_owner, lease_expires_at, last_error, created_at, finished_at
            FROM ops.job_queue
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(row) for row in rows]


def make_dedupe_key(job_id: str, scheduled_for: datetime) -> str:
    """Generate a dedupe key for a job run."""
    return f"{job_id}:{scheduled_for.strftime('%Y%m%dT%H%M%SZ')}"


def backfill_start(now: datetime) -> datetime:
    """Calculate the start time for backfill queries."""
    return now - timedelta(hours=DEFAULT_BACKFILL_HOURS)


def _row_to_job(row: Any) -> QueueJob:
    """Convert a database row to a QueueJob."""
    lease_expires_at = row["lease_expires_at"]
    return QueueJob(
        id=str(row["id"]),
        job_id=row["job_id"],
        status=row["status"],
        scheduled_for=row["scheduled_for"].astimezone(UTC),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=lease_expires_at.astimezone(UTC) if lease_expires_at else None,
        payload=row["payload"],
        dedupe_key=row["dedupe_key"],
        last_error=row["last_error"],
    )


def _updated_rows(result: str) -> int:
    """Extract row count from asyncpg result string."""
    # result is typically "UPDATE <count>"
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
