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
- builtin: Jobs in this package (zerg.jobs.*)
- git: Jobs loaded from private repo at runtime
- http: Declarative HTTP DSL jobs (no Python code)

Usage:
    from zerg.jobs import register_all_jobs

    # In scheduler startup:
    register_all_jobs(scheduler)
"""

from .git_sync import GitSyncError
from .git_sync import GitSyncService
from .git_sync import get_git_sync_service
from .git_sync import run_git_sync_loop
from .git_sync import set_git_sync_service
from .http_dsl import HTTPJobError
from .http_dsl import create_http_executor
from .loader import JobLoadError
from .loader import load_job_func
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
    # Loader
    "JobLoadError",
    "load_job_func",
    # HTTP DSL
    "HTTPJobError",
    "create_http_executor",
]
