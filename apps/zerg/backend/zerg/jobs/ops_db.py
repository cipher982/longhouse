"""Job run persistence via SQLAlchemy.

Provides emit_job_run() to persist job execution history to the job_runs
table, and cleanup_old_job_runs() for retention management.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from uuid import uuid4

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
    """Persist a job run record to the job_runs table.

    Non-fatal: logs on failure rather than raising.
    """
    try:
        from zerg.database import db_session
        from zerg.models.models import JobRun

        # Build metadata JSON combining all extra fields
        meta: dict[str, Any] = {}
        if metadata:
            meta.update(metadata)
        if tags:
            meta["tags"] = tags
        if project:
            meta["project"] = project
        if scheduler:
            meta["scheduler"] = scheduler

        metadata_json = json.dumps(meta) if meta else None

        with db_session() as db:
            run = JobRun(
                id=str(uuid4()),
                job_id=job_id,
                status=status,
                started_at=started_at,
                finished_at=ended_at,
                duration_ms=duration_ms,
                error_message=error_message,
                metadata_json=metadata_json,
            )
            db.add(run)
            # db_session context manager auto-commits

        logger.debug("emit_job_run persisted: job_id=%s status=%s", job_id, status)

    except Exception:
        logger.exception("emit_job_run failed (non-fatal): job_id=%s status=%s", job_id, status)


def cleanup_old_job_runs(max_age_days: int = 30, max_per_job: int = 100) -> int:
    """Delete job runs older than max_age_days, keeping at most max_per_job per job_id.

    Returns the total number of rows deleted.
    """
    try:
        from sqlalchemy import text as sa_text

        from zerg.database import db_session

        deleted = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

        with db_session() as db:
            # 1. Delete everything older than cutoff
            result = db.execute(
                sa_text("DELETE FROM job_runs WHERE created_at < :cutoff"),
                {"cutoff": cutoff},
            )
            deleted += result.rowcount

            # 2. Per job_id, keep only the most recent max_per_job rows
            over_limit = db.execute(
                sa_text(
                    """
                    SELECT job_id, COUNT(*) as cnt
                    FROM job_runs
                    GROUP BY job_id
                    HAVING cnt > :limit
                    """
                ),
                {"limit": max_per_job},
            ).fetchall()

            for row in over_limit:
                jid = row[0]
                # Find the created_at of the Nth newest row (the cutoff row)
                nth = db.execute(
                    sa_text(
                        """
                        SELECT created_at FROM job_runs
                        WHERE job_id = :jid
                        ORDER BY created_at DESC
                        LIMIT 1 OFFSET :offset
                        """
                    ),
                    {"jid": jid, "offset": max_per_job - 1},
                ).fetchone()
                if nth:
                    result = db.execute(
                        sa_text("DELETE FROM job_runs WHERE job_id = :jid AND created_at < :cutoff"),
                        {"jid": jid, "cutoff": nth[0]},
                    )
                    deleted += result.rowcount

        logger.info("cleanup_old_job_runs: deleted %d rows", deleted)
        return deleted

    except Exception:
        logger.exception("cleanup_old_job_runs failed (non-fatal)")
        return 0
