"""SQLite-safe commis job queue operations.

This module provides dialect-aware job claiming and heartbeat operations
that work on both Postgres and SQLite.

Postgres uses: FOR UPDATE SKIP LOCKED (row-level locking)
SQLite uses: BEGIN IMMEDIATE + UPDATE ... RETURNING (write lock on tx start)

Key concepts:
- claimed_at: When a worker claimed the job (for stale detection)
- heartbeat_at: Last heartbeat from worker (proves worker is alive)
- worker_id: Identifies which worker claimed the job

Stale job reclaim: Jobs with no heartbeat for STALE_THRESHOLD_SECONDS
are reset to 'queued' so another worker can claim them.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import TYPE_CHECKING

from sqlalchemy import text

from zerg.database import db_session

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Configuration (can be overridden via env vars)
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("COMMIS_HEARTBEAT_INTERVAL", "30"))
STALE_THRESHOLD_SECONDS = int(os.getenv("COMMIS_STALE_THRESHOLD", "120"))


def get_worker_id() -> str:
    """Generate a unique worker identifier for this process."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _is_sqlite(db: Session) -> bool:
    """Check if the current database session is SQLite."""
    return db.bind is not None and db.bind.dialect.name == "sqlite"


def claim_jobs_postgres(
    db: Session,
    limit: int,
    worker_id: str,
) -> list[int]:
    """Claim pending jobs using Postgres FOR UPDATE SKIP LOCKED.

    This is the most efficient approach for Postgres - it atomically selects
    and locks rows in a single query, preventing race conditions.

    Args:
        db: Database session
        limit: Maximum number of jobs to claim
        worker_id: Identifier for this worker

    Returns:
        List of claimed job IDs
    """
    # ORDER BY id ASC as tie-breaker for deterministic FIFO when timestamps match
    result = db.execute(
        text("""
            UPDATE commis_jobs
            SET status = 'running',
                started_at = NOW(),
                claimed_at = NOW(),
                heartbeat_at = NOW(),
                worker_id = :worker_id
            WHERE id IN (
                SELECT id FROM commis_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
        """),
        {"limit": limit, "worker_id": worker_id},
    )
    job_ids = [row[0] for row in result.fetchall()]
    db.commit()
    return job_ids


def claim_jobs_sqlite(
    db: Session,
    limit: int,
    worker_id: str,
) -> list[int]:
    """Claim pending jobs using SQLite UPDATE RETURNING.

    SQLite's write transactions serialize at the statement level. The UPDATE
    statement acquires a reserved lock that blocks other writers until commit.
    Combined with UPDATE ... RETURNING (SQLite 3.35+), this provides atomic
    job claiming without explicit BEGIN IMMEDIATE.

    Note: SQLite's default DEFERRED transaction mode is sufficient because
    the UPDATE statement itself acquires the necessary write lock.

    Args:
        db: Database session
        limit: Maximum number of jobs to claim
        worker_id: Identifier for this worker

    Returns:
        List of claimed job IDs
    """
    # SQLite uses datetime() for current timestamp
    # ORDER BY id ASC as tie-breaker for deterministic FIFO when timestamps match
    result = db.execute(
        text("""
            UPDATE commis_jobs
            SET status = 'running',
                started_at = datetime('now'),
                claimed_at = datetime('now'),
                heartbeat_at = datetime('now'),
                worker_id = :worker_id
            WHERE id IN (
                SELECT id FROM commis_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
            )
            RETURNING id
        """),
        {"limit": limit, "worker_id": worker_id},
    )
    job_ids = [row[0] for row in result.fetchall()]
    db.commit()
    return job_ids


def claim_jobs(
    db: Session,
    limit: int,
    worker_id: str | None = None,
) -> list[int]:
    """Claim pending commis jobs atomically (dialect-aware).

    Dispatches to the appropriate implementation based on database dialect.

    Args:
        db: Database session
        limit: Maximum number of jobs to claim
        worker_id: Optional worker identifier (defaults to hostname:pid)

    Returns:
        List of claimed job IDs
    """
    if worker_id is None:
        worker_id = get_worker_id()

    if _is_sqlite(db):
        return claim_jobs_sqlite(db, limit, worker_id)
    else:
        return claim_jobs_postgres(db, limit, worker_id)


def update_heartbeat(db: Session, job_id: int, worker_id: str) -> bool:
    """Update heartbeat for a running job.

    Called periodically by the processor to prove the worker is still alive.
    Only updates if the job is still 'running' and owned by this worker.

    Args:
        db: Database session
        job_id: Job ID to update
        worker_id: Worker ID that owns the job

    Returns:
        True if heartbeat was updated, False if job is no longer owned
    """
    if _is_sqlite(db):
        result = db.execute(
            text("""
                UPDATE commis_jobs
                SET heartbeat_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = :job_id
                  AND status = 'running'
                  AND worker_id = :worker_id
            """),
            {"job_id": job_id, "worker_id": worker_id},
        )
    else:
        result = db.execute(
            text("""
                UPDATE commis_jobs
                SET heartbeat_at = NOW(),
                    updated_at = NOW()
                WHERE id = :job_id
                  AND status = 'running'
                  AND worker_id = :worker_id
            """),
            {"job_id": job_id, "worker_id": worker_id},
        )
    db.commit()
    return result.rowcount > 0


def reclaim_stale_jobs(db: Session) -> int:
    """Reclaim jobs from workers that have gone silent.

    A job is considered stale if:
    - status = 'running'
    - heartbeat_at is older than STALE_THRESHOLD_SECONDS ago
    - OR heartbeat_at is NULL (legacy job without heartbeat)

    Stale jobs are reset to 'queued' so another worker can pick them up.

    Args:
        db: Database session

    Returns:
        Number of jobs reclaimed
    """
    threshold = STALE_THRESHOLD_SECONDS

    if _is_sqlite(db):
        result = db.execute(
            text("""
                UPDATE commis_jobs
                SET status = 'queued',
                    worker_id = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    started_at = NULL,
                    updated_at = datetime('now')
                WHERE status = 'running'
                  AND (
                      heartbeat_at IS NULL
                      OR heartbeat_at < datetime('now', '-' || :threshold || ' seconds')
                  )
            """),
            {"threshold": str(threshold)},
        )
    else:
        # Postgres: use interval arithmetic with proper parameter binding
        # Note: INTERVAL doesn't support parameter binding directly, so use
        # timestamp arithmetic with MAKE_INTERVAL()
        result = db.execute(
            text("""
                UPDATE commis_jobs
                SET status = 'queued',
                    worker_id = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    started_at = NULL,
                    updated_at = NOW()
                WHERE status = 'running'
                  AND (
                      heartbeat_at IS NULL
                      OR heartbeat_at < NOW() - MAKE_INTERVAL(secs => :threshold)
                  )
            """),
            {"threshold": threshold},
        )
    db.commit()
    count = result.rowcount
    if count > 0:
        logger.warning(f"Reclaimed {count} stale commis jobs")
    return count


async def reclaim_stale_jobs_async() -> int:
    """Async wrapper for stale job reclaim (for background tasks)."""
    with db_session() as db:
        return reclaim_stale_jobs(db)
