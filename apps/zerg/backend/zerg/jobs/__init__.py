"""Scheduled jobs framework for Zerg.

This module provides the infrastructure for running scheduled jobs (migrated from Sauron).
Jobs are registered with APScheduler and executed on cron schedules.

Job Categories:
- backups: Backup verification and sync (backup-sentinel, minio-sync)
- monitoring: Infrastructure monitoring (disk-health, traccar-watchdog)
- digests: Daily digests and reports (worklog, google-ads-digest)
- syncs: Data synchronization (gmail-sync)
- maintenance: Cleanup and maintenance tasks

Usage:
    from zerg.jobs import register_all_jobs

    # In scheduler startup:
    register_all_jobs(scheduler)
"""

from .registry import JobConfig
from .registry import JobRegistry
from .registry import job_registry
from .registry import register_all_jobs

__all__ = [
    "JobConfig",
    "JobRegistry",
    "job_registry",
    "register_all_jobs",
]
