"""Builtin AI-first operational watchman job."""

from __future__ import annotations

import os
from typing import Any

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.services.ops_watchman import _watchman_enabled
from zerg.services.ops_watchman import run_watchman_cycle


async def run() -> dict[str, Any]:
    """Execute one AI ops watchman cycle."""
    if not _watchman_enabled():
        return {"status": "skipped", "reason": "OPS_WATCHMAN_ENABLED=0"}
    return await run_watchman_cycle(db_session_factory=db_session)


if _watchman_enabled():
    job_registry.register(
        JobConfig(
            id="ai-ops-watchman",
            cron=os.getenv("OPS_WATCHMAN_CRON", "*/5 * * * *"),
            func=run,
            enabled=True,
            timeout_seconds=int(os.getenv("OPS_WATCHMAN_JOB_TIMEOUT_SECONDS", "120")),
            max_attempts=1,
            tags=["monitoring", "builtin", "ai"],
            description="AI-first tenant-local monitoring via Grok 4.3 watchman analysis",
        )
    )
