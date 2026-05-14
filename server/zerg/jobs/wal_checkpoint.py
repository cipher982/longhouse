"""Builtin WAL truncate checkpoint job.

The background PASSIVE checkpoint loop in database.py cannot checkpoint pages
while readers hold references. On large databases the WAL file stays bloated
indefinitely. This job runs PRAGMA wal_checkpoint(TRUNCATE) once a day which
blocks briefly but fully resets the WAL file to zero bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)

JOB_ID = "wal-truncate-checkpoint"


async def run() -> dict[str, Any]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) to fully reset the WAL file."""
    from zerg.database import default_engine

    if default_engine is None:
        return {"status": "skipped", "reason": "no engine"}

    # Only applicable to SQLite databases
    url_str = str(default_engine.url)
    if "sqlite" not in url_str:
        return {"status": "skipped", "reason": "not sqlite"}

    def _do_truncate() -> tuple[int, int, int, int]:
        with default_engine.connect() as conn:
            result = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            row = result.fetchone()
            # SQLite returns (busy, log_frames, checkpointed_frames).
            if not row:
                return (0, 0, 0, 0)
            busy = int(row[0] or 0)
            log_frames = int(row[1] or 0)
            checkpointed_frames = int(row[2] or 0)
            remaining_frames = max(log_frames - checkpointed_frames, 0)
            return busy, log_frames, checkpointed_frames, remaining_frames

    busy, log_frames, checkpointed, remaining = await asyncio.to_thread(_do_truncate)

    if busy:
        logger.warning(
            "WAL TRUNCATE checkpoint was busy: %d frames in log, %d checkpointed, %d remaining",
            log_frames,
            checkpointed,
            remaining,
        )
    else:
        logger.info(
            "WAL TRUNCATE checkpoint complete: %d frames in log, %d checkpointed, %d remaining",
            log_frames,
            checkpointed,
            remaining,
        )

    return {
        "status": "success",
        "busy": busy,
        "log_frames": log_frames,
        "pages_checkpointed": checkpointed,
        "pages_remaining": remaining,
    }


job_registry.register(
    JobConfig(
        id=JOB_ID,
        cron=os.getenv("WAL_TRUNCATE_CHECKPOINT_CRON", "0 3 * * *"),
        func=run,
        enabled=True,
        timeout_seconds=120,
        max_attempts=1,
        tags=["maintenance", "builtin"],
        description="Daily WAL TRUNCATE checkpoint to fully reset WAL file size",
    )
)
