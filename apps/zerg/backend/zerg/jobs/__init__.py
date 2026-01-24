"""Scheduled jobs framework for Zerg.

This module provides the infrastructure for running scheduled jobs (migrated from Sauron).
Jobs are registered with APScheduler and executed on cron schedules.

Job Categories:
- backups: Backup verification and sync (backup-sentinel, minio-sync)
- monitoring: Infrastructure monitoring (disk-health, traccar-watchdog)
- digests: Daily digests and reports (worklog, google-ads-digest)
- syncs: Data synchronization (gmail-sync)
- maintenance: Cleanup and maintenance tasks

Job Sources:
- builtin: Jobs in this package (zerg.jobs.*) that self-register on import
- manifest: External jobs from private git repo via manifest.py

External jobs are loaded from a git repo configured via JOBS_GIT_REPO_URL.
The repo should contain a manifest.py that imports JobConfig and job_registry
from zerg and registers jobs:

    from zerg.jobs import job_registry, JobConfig
    from jobs.backup_sentinel import run as backup_run

    job_registry.register(JobConfig(
        id="backup-sentinel",
        cron="0 10 * * *",
        func=backup_run,
    ))

Usage:
    from zerg.jobs import register_all_jobs

    # In scheduler startup (async):
    await register_all_jobs(scheduler)
"""

from .git_sync import GitSyncError
from .git_sync import GitSyncService
from .git_sync import get_git_sync_service
from .git_sync import run_git_sync_loop
from .git_sync import set_git_sync_service
from .loader import get_manifest_metadata
from .loader import load_jobs_manifest
from .registry import JobConfig
from .registry import JobRegistry
from .registry import job_registry
from .registry import register_all_jobs

__all__ = [
    # Registry
    "JobConfig",
    "JobRegistry",
    "job_registry",
    "register_all_jobs",
    # Git sync
    "GitSyncError",
    "GitSyncService",
    "get_git_sync_service",
    "run_git_sync_loop",
    "set_git_sync_service",
    # Manifest loader
    "load_jobs_manifest",
    "get_manifest_metadata",
]
