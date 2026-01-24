"""Manifest loader for external jobs.

Loads jobs from a private git repo's manifest.py file.
The manifest imports JobConfig and job_registry from zerg and registers jobs.

This replaces the previous multi-source loader (builtin/git/http DSL) with
a simpler pattern: builtin jobs register themselves on import, external
jobs are registered by executing manifest.py from the cloned repo.
"""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path

from zerg.jobs.git_sync import get_git_sync_service

logger = logging.getLogger(__name__)


def load_jobs_manifest() -> bool:
    """
    Load jobs from the private repo's manifest.py.

    The manifest.py file should import JobConfig and job_registry from zerg,
    then register jobs like:

        from zerg.jobs import job_registry, JobConfig
        from jobs.backup_sentinel import run as backup_run

        job_registry.register(JobConfig(
            id="backup-sentinel",
            cron="0 10 * * *",
            func=backup_run,
        ))

    Returns:
        True if manifest was loaded successfully, False otherwise.
        Returns False (doesn't raise) so builtin jobs continue working.
    """
    git_service = get_git_sync_service()
    if not git_service:
        logger.info("Jobs git repo not configured; skipping manifest load")
        return False

    repo_root = Path(git_service.local_path)
    manifest_path = repo_root / "manifest.py"

    if not manifest_path.exists():
        logger.warning("Jobs manifest missing: %s", manifest_path)
        return False

    # Add repo root to sys.path so `from jobs import utils` works
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    try:
        runpy.run_path(str(manifest_path), run_name="zerg_jobs_manifest")
        logger.info("Loaded jobs manifest: %s", manifest_path)
        return True
    except Exception:
        logger.exception("Jobs manifest failed to load: %s", manifest_path)
        return False


__all__ = ["load_jobs_manifest"]
