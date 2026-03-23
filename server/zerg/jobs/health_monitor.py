"""Job health monitor — detects manifest failures, missed schedules, and consecutive failures.

Builtin job running every 6 hours. Alerts via SES email.

Checks:
1. Manifest load: JobRepoConfig exists but 0 manifest jobs → alert
2. Missed schedules: Last run stale beyond 2× cron interval → alert
3. Consecutive failures: Last 3 runs all failed → alert
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry

logger = logging.getLogger(__name__)


def _has_repo_config() -> bool:
    """Check if a repo config exists in the DB."""
    try:
        from zerg.database import db_session
        from zerg.models.models import JobRepoConfig

        with db_session() as db:
            return db.query(JobRepoConfig).first() is not None
    except Exception:
        return False


def _get_recent_runs_sync(job_id: str, limit: int = 3) -> list[dict]:
    """Get recent queue entries for a specific job (sync, run via to_thread)."""
    from zerg.jobs.queue import _connect

    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT status, scheduled_for, finished_at, last_error
            FROM job_queue
            WHERE job_id = :job_id
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"job_id": job_id, "limit": limit},
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def _get_recent_runs(job_id: str, limit: int = 3) -> list[dict]:
    """Get recent queue entries (non-blocking)."""
    return await asyncio.to_thread(_get_recent_runs_sync, job_id, limit)


async def run() -> dict[str, Any]:
    """Run job health checks and send alerts for issues found."""
    from zerg.shared.email import send_alert_email

    issues: list[str] = []
    now = datetime.now(UTC)

    # ── Check 1: Manifest load ──────────────────────────────────────
    if _has_repo_config():
        all_jobs = job_registry.list_jobs()
        manifest_jobs = [j for j in all_jobs if "builtin" not in (j.tags or [])]
        if not manifest_jobs:
            issues.append(
                "MANIFEST EMPTY: Repo config exists but 0 manifest jobs are loaded. " "Check git sync logs and manifest.py for errors."
            )

    # ── Check 2: Missed schedules ───────────────────────────────────
    for config in job_registry.list_jobs(enabled_only=True):
        try:
            # Compute cron interval using APScheduler's CronTrigger
            trigger = CronTrigger.from_crontab(config.cron)
            # Get two consecutive fire times to determine the interval
            t1 = trigger.get_next_fire_time(None, now)
            if not t1:
                continue
            # Advance "now" past t1 so get_next_fire_time returns the *next* fire
            t2 = trigger.get_next_fire_time(t1, t1 + timedelta(seconds=1))
            if not t2:
                continue
            interval = t2 - t1
            staleness_threshold = interval * 2

            runs = await _get_recent_runs(config.id, limit=1)
            if not runs:
                # No runs at all — only alert if job has been registered long enough
                continue

            last_run_time = runs[0].get("scheduled_for") or runs[0].get("finished_at")
            if last_run_time:
                if isinstance(last_run_time, str):
                    last_run_time = datetime.fromisoformat(last_run_time)
                if last_run_time.tzinfo is None:
                    last_run_time = last_run_time.replace(tzinfo=UTC)

                age = now - last_run_time
                if age > staleness_threshold:
                    issues.append(
                        f"MISSED SCHEDULE: {config.id} — last run {age} ago " f"(threshold: {staleness_threshold}). Cron: {config.cron}"
                    )
        except Exception as e:
            logger.debug("Skipping schedule check for %s: %s", config.id, e)

    # ── Check 3: Consecutive failures ───────────────────────────────
    for config in job_registry.list_jobs(enabled_only=True):
        try:
            runs = await _get_recent_runs(config.id, limit=3)
            if len(runs) >= 3 and all(r.get("status") in ("failure", "dead") for r in runs):
                last_error = runs[0].get("last_error", "unknown")
                issues.append(f"CONSECUTIVE FAILURES: {config.id} — last 3 runs all failed. " f"Latest error: {str(last_error)[:200]}")
        except Exception as e:
            logger.debug("Skipping failure check for %s: %s", config.id, e)

    # ── Send alert if issues found ──────────────────────────────────
    if issues:
        subject = f"Job health: {len(issues)} issue(s) detected"
        body = "Job Health Monitor Report\n" + "=" * 40 + "\n\n"
        body += "\n\n".join(f"• {issue}" for issue in issues)
        body += f"\n\nChecked at: {now.isoformat()}"
        body += f"\nTotal registered jobs: {len(job_registry.list_jobs())}"

        send_alert_email(
            subject,
            body,
            level="WARNING",
            alert_type="job_health",
            job_id="job-health-monitor",
        )
        logger.warning("Job health monitor: %d issues found", len(issues))
    else:
        logger.info("Job health monitor: all checks passed")

    return {
        "success": True,
        "issues_found": len(issues),
        "issues": issues,
        "checked_at": now.isoformat(),
    }


# Register the job
job_registry.register(
    JobConfig(
        id="job-health-monitor",
        cron=os.getenv("JOB_HEALTH_MONITOR_CRON", "0 */6 * * *"),
        func=run,
        enabled=True,
        timeout_seconds=120,
        max_attempts=1,
        tags=["monitoring", "builtin"],
        description="Detect manifest failures, missed schedules, and consecutive job failures",
    )
)
