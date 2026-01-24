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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Track manifest load metadata for job runs
_manifest_metadata: dict[str, dict] = {}


def get_manifest_metadata(job_id: str) -> dict | None:
    """Get metadata for a manifest-loaded job (git SHA, loaded_at, etc.)."""
    return _manifest_metadata.get(job_id)


def set_manifest_metadata(job_id: str, metadata: dict) -> None:
    """Store metadata for a manifest-loaded job."""
    _manifest_metadata[job_id] = metadata


async def load_jobs_manifest() -> bool:
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

    Note:
        - Acquires git sync read lock to prevent loading during repo update
        - Temporarily modifies sys.path (restored after load)
        - Duplicate job IDs are skipped with a warning (not fatal)
    """
    from zerg.jobs.git_sync import get_git_sync_service

    git_service = get_git_sync_service()
    if not git_service:
        logger.info("Jobs git repo not configured; skipping manifest load")
        return False

    repo_root = Path(git_service.local_path)
    manifest_path = repo_root / "manifest.py"

    if not manifest_path.exists():
        logger.warning("Jobs manifest missing: %s", manifest_path)
        return False

    # Acquire read lock to prevent loading during git sync
    async with git_service.read_lock() as git_sha:
        return _execute_manifest(manifest_path, repo_root, git_sha)


def _execute_manifest(manifest_path: Path, repo_root: Path, git_sha: str | None) -> bool:
    """Execute manifest.py with proper sys.path handling.

    Temporarily adds repo_root to sys.path, then removes it after loading
    to avoid polluting the global import namespace.
    """
    from datetime import UTC
    from datetime import datetime

    from zerg.jobs.registry import job_registry

    repo_root_str = str(repo_root)
    path_added = False

    # Track jobs before/after to identify manifest jobs
    jobs_before = set(job_registry._jobs.keys())

    # Temporarily add repo root to sys.path
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
        path_added = True

    try:
        # Use a custom globals dict to catch registration errors per-job
        runpy.run_path(str(manifest_path), run_name="zerg_jobs_manifest")

        # Track which jobs were added by the manifest
        jobs_after = set(job_registry._jobs.keys())
        new_jobs = jobs_after - jobs_before

        # Store metadata for manifest jobs
        loaded_at = datetime.now(UTC).isoformat()
        for job_id in new_jobs:
            set_manifest_metadata(
                job_id,
                {
                    "script_source": "manifest",
                    "git_sha": git_sha,
                    "loaded_at": loaded_at,
                    "manifest_path": str(manifest_path),
                },
            )

        logger.info(
            "Loaded jobs manifest: %s (sha=%s, jobs=%d)",
            manifest_path,
            git_sha[:8] if git_sha else "unknown",
            len(new_jobs),
        )
        return True

    except Exception:
        logger.exception("Jobs manifest failed to load: %s", manifest_path)
        return False

    finally:
        # Clean up sys.path to avoid polluting global namespace
        if path_added and repo_root_str in sys.path:
            sys.path.remove(repo_root_str)


__all__ = ["load_jobs_manifest", "get_manifest_metadata", "set_manifest_metadata"]
