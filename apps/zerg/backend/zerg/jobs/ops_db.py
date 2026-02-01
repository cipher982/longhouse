"""Stub module for asyncpg-based ops database functionality.

This module previously provided asyncpg connection pooling for the ops
database (job runs, telemetry). In the SQLite-only migration, this
functionality has been removed.

Jobs that need database access should use SQLAlchemy via zerg.database.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def is_job_queue_db_enabled() -> bool:
    """Check if job queue DB is enabled via env var."""
    return os.getenv("JOB_QUEUE_ENABLED", "0") == "1"


def get_scheduler_name() -> str:
    """Return scheduler name for job runs."""
    import socket

    return f"sched-{socket.gethostname()[:8]}"


async def get_pool():
    """Connection pool not available in SQLite-only mode."""
    raise NotImplementedError("asyncpg pool not available in SQLite-only mode")


async def close_pool() -> None:
    """No-op: pool not used in SQLite-only mode."""
    pass


async def emit_job_run(
    job_id: str,
    status: str,
    started_at: datetime,
    ended_at: datetime,
    duration_ms: int,
    error_message: str | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
    scheduler: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit job run to ops database (no-op in SQLite-only mode).

    In SQLite-only mode, job runs are not persisted to the ops database.
    Use zerg.database for local persistence if needed.
    """
    logger.debug("emit_job_run skipped (SQLite-only mode): job_id=%s status=%s", job_id, status)
