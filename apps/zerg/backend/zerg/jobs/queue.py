"""Stub module for asyncpg-based job queue functionality.

This module previously provided a durable job queue using asyncpg and
PostgreSQL. In the SQLite-only migration, this functionality has been
removed.

For job queue functionality, use the commis_job_queue which works with SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0


@dataclass
class QueueOwner:
    """Owner identity for queue operations."""

    name: str
    pid: int


@dataclass
class QueueJob:
    """A queued job entry."""

    id: str
    job_id: str
    scheduled_for: datetime
    attempts: int
    max_attempts: int
    status: str
    last_error: str | None = None


def default_owner() -> QueueOwner:
    """Return default queue owner based on hostname and PID."""
    import os
    import socket

    return QueueOwner(name=socket.gethostname()[:32], pid=os.getpid())


def make_dedupe_key(job_id: str, scheduled_for: datetime) -> str:
    """Generate dedupe key for a job run."""
    return f"{job_id}:{scheduled_for.isoformat()}"


def backfill_start(now: datetime) -> datetime:
    """Return the start time for backfilling missed runs."""
    from datetime import timedelta

    return now - timedelta(hours=24)


async def enqueue_job(
    job_id: str,
    scheduled_for: datetime,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
) -> str | None:
    """Enqueue a job (disabled in SQLite-only mode)."""
    logger.debug("enqueue_job skipped (SQLite-only mode): job_id=%s", job_id)
    return None


async def claim_next_job(owner: QueueOwner) -> QueueJob | None:
    """Claim next available job (disabled in SQLite-only mode)."""
    return None


async def claim_job_by_id(queue_id: str, owner: QueueOwner) -> QueueJob | None:
    """Claim a specific job by ID (disabled in SQLite-only mode)."""
    return None


async def complete_job(
    queue_id: str,
    status: str,
    error: str | None = None,
    owner: QueueOwner | None = None,
) -> bool:
    """Mark job as complete (disabled in SQLite-only mode)."""
    return True


async def reschedule_job(
    queue_id: str,
    retry_at: datetime,
    error: str | None = None,
    owner: QueueOwner | None = None,
) -> bool:
    """Reschedule a job for retry (disabled in SQLite-only mode)."""
    return True


async def extend_lease(
    queue_id: str,
    owner: QueueOwner,
    lease_seconds: int,
) -> bool:
    """Extend job lease (disabled in SQLite-only mode)."""
    return True


async def get_last_scheduled_for(job_id: str) -> datetime | None:
    """Get last scheduled time for a job (disabled in SQLite-only mode)."""
    return None


async def cleanup_zombies() -> int:
    """Clean up zombie jobs (disabled in SQLite-only mode)."""
    return 0


async def get_recent_queue_entries(limit: int = 20) -> list[dict[str, Any]]:
    """Get recent queue entries (disabled in SQLite-only mode)."""
    return []
