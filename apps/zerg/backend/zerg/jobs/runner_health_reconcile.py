"""Builtin runner health reconciliation job."""

from __future__ import annotations

import os
from typing import Any

from zerg.database import get_session_factory
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.services.runner_health_reconciler import reconcile_runner_health

JOB_ID = "runner-health-reconcile"


async def run() -> dict[str, Any]:
    """Reconcile runner health and trigger deduped attention flows."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        return await reconcile_runner_health(db)
    finally:
        db.close()


job_registry.register(
    JobConfig(
        id=JOB_ID,
        cron=os.getenv("RUNNER_HEALTH_RECONCILE_CRON", "*/2 * * * *"),
        func=run,
        enabled=True,
        timeout_seconds=120,
        max_attempts=1,
        tags=["runner", "monitoring", "builtin"],
        description="Reconcile runner liveness, incidents, alerts, and Oikos wakeups",
    )
)
