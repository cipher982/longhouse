"""Manifest loader for external jobs.

Loads jobs from a private git repo's manifest.py file.
The manifest imports JobConfig and job_registry from zerg and registers jobs.

This replaces the previous multi-source loader (builtin/git/http DSL) with
a simpler pattern: builtin jobs register themselves on import, external
jobs are registered by executing manifest.py from the cloned repo.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import runpy
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Track manifest load metadata for job runs
_manifest_metadata: dict[str, dict] = {}

# Process-wide lock: serializes manifest reloads so hot-start and sync-loop
# can't interleave (clear modules, clear registry, re-execute manifest).
_reload_lock = asyncio.Lock()


def get_manifest_metadata(job_id: str) -> dict | None:
    """Get metadata for a manifest-loaded job (git SHA, loaded_at, etc.)."""
    return _manifest_metadata.get(job_id)


def set_manifest_metadata(job_id: str, metadata: dict) -> None:
    """Store metadata for a manifest-loaded job."""
    _manifest_metadata[job_id] = metadata


def _get_builtin_job_ids() -> set[str]:
    """Return the set of builtin job IDs (tagged 'builtin' in registry)."""
    from zerg.jobs.registry import job_registry

    return {cfg.id for cfg in job_registry.list_jobs() if "builtin" in (cfg.tags or [])}


def _clear_job_modules() -> int:
    """Remove cached job modules from sys.modules so code changes are picked up.

    Python caches modules in ``sys.modules`` — without this, ``runpy.run_path``
    on the manifest will re-import stale bytecode for ``jobs.*`` submodules.

    Also evicts the ``zerg_jobs_manifest`` entry that ``runpy.run_path`` creates
    and invalidates import caches so finders pick up new/changed files.

    Returns the number of modules evicted.
    """
    to_remove = [key for key in sys.modules if key == "jobs" or key.startswith("jobs.") or key == "zerg_jobs_manifest"]
    for key in to_remove:
        del sys.modules[key]
    importlib.invalidate_caches()
    if to_remove:
        logger.info("Evicted %d cached job modules: %s", len(to_remove), to_remove)
    return len(to_remove)


async def reload_manifest_jobs() -> dict:
    """Full reload: snapshot → clear modules → install deps → load manifest → sync scheduler.

    Used by both the sync loop (on SHA change) and hot-start from the API.
    Serialized via _reload_lock to prevent interleaved reloads.

    Returns:
        Dict with sync_result keys (added, removed, rescheduled) or error info.
    """
    async with _reload_lock:
        return await _reload_manifest_jobs_locked()


async def _reload_manifest_jobs_locked() -> dict:
    """Inner reload logic (must be called under _reload_lock)."""
    from zerg.jobs.registry import job_registry

    # 1. Snapshot current jobs for scheduler diff
    old_snapshot = job_registry.snapshot_jobs()

    # 2. Compute builtin IDs to preserve
    builtin_ids = _get_builtin_job_ids()

    # 3. Invalidate cached job modules in sys.modules
    _clear_job_modules()

    # 4. Load manifest (installs deps internally, clears non-builtin jobs)
    success = await load_jobs_manifest(clear_existing=True, builtin_job_ids=builtin_ids)

    if not success:
        # Finding 3: resync scheduler even on failure so stale triggers are removed.
        # Jobs were already cleared in load_jobs_manifest; sync removes their triggers.
        sync_result = job_registry.sync_jobs(old_snapshot)
        logger.warning(
            "Manifest reload failed — scheduler synced to remove stale triggers: removed=%d",
            sync_result["removed"],
        )
        return {"success": False, "error": "manifest load failed", **sync_result}

    # 5. Sync scheduler (add/remove/reschedule cron triggers)
    sync_result = job_registry.sync_jobs(old_snapshot)
    logger.info(
        "Manifest reload complete: added=%d removed=%d rescheduled=%d",
        sync_result["added"],
        sync_result["removed"],
        sync_result["rescheduled"],
    )
    return {"success": True, **sync_result}


async def load_jobs_manifest(clear_existing: bool = False, builtin_job_ids: set[str] | None = None) -> bool:
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

    Args:
        clear_existing: If True, clear all manifest jobs before reloading.
            This enables proper sync where removed jobs get unregistered.
        builtin_job_ids: Set of job IDs to preserve when clear_existing=True.
            If None and clear_existing=True, no jobs are preserved.

    Returns:
        True if manifest was loaded successfully, False otherwise.
        Returns False (doesn't raise) so builtin jobs continue working.

    Note:
        - Acquires git sync read lock to prevent loading during repo update (if git configured)
        - Temporarily modifies sys.path (restored after load)
        - Duplicate job IDs are skipped with a warning (not fatal)
        - Falls back to local-only mode (jobs_dir) when git sync is not configured
    """
    from zerg.jobs.git_sync import get_git_sync_service
    from zerg.jobs.registry import job_registry

    git_service = get_git_sync_service()
    if git_service:
        # Git-based manifest loading (preferred path, backwards compatible)
        repo_root = Path(git_service.local_path)
        manifest_path = repo_root / "manifest.py"

        if not manifest_path.exists():
            logger.warning("Jobs manifest missing: %s", manifest_path)
            return False

        # Acquire read lock to prevent loading during git sync
        # IMPORTANT: clear_existing must be inside the lock to prevent race conditions
        # where registry is cleared but then blocks waiting for background sync
        async with git_service.read_lock() as git_sha:
            # Install job pack dependencies (non-fatal)
            try:
                from zerg.services.jobs_repo import install_jobs_deps

                deps_result = await asyncio.to_thread(install_jobs_deps, repo_root)
                if deps_result.get("error"):
                    logger.warning("Job deps install failed (non-fatal): %s", deps_result["error"])
            except Exception as e:
                logger.warning("Job deps install failed (non-fatal): %s", e)

            # Clear manifest jobs before reload if requested
            if clear_existing:
                preserved = builtin_job_ids or set()
                removed = job_registry.clear_manifest_jobs(preserved)
                if removed:
                    logger.info("Cleared %d manifest jobs before reload: %s", len(removed), removed)

            return _execute_manifest(manifest_path, repo_root, git_sha)
    else:
        # Local-only mode: load from jobs_dir when git sync is not configured
        from zerg.config import get_settings

        settings = get_settings()
        repo_root = Path(settings.jobs_dir)
        manifest_path = repo_root / "manifest.py"

        if not manifest_path.exists():
            logger.info("No jobs manifest found at %s (local-only mode)", manifest_path)
            return False

        # Install job pack dependencies (non-fatal)
        try:
            from zerg.services.jobs_repo import install_jobs_deps

            deps_result = await asyncio.to_thread(install_jobs_deps, repo_root)
            if deps_result.get("error"):
                logger.warning("Job deps install failed (non-fatal): %s", deps_result["error"])
        except Exception as e:
            logger.warning("Job deps install failed (non-fatal): %s", e)

        # Clear manifest jobs before reload if requested
        if clear_existing:
            preserved = builtin_job_ids or set()
            removed = job_registry.clear_manifest_jobs(preserved)
            if removed:
                logger.info("Cleared %d manifest jobs before reload: %s", len(removed), removed)

        return _execute_manifest(manifest_path, repo_root, None)


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
        # Use "git" as script_source (valid for ops.jobs check constraint)
        loaded_at = datetime.now(UTC).isoformat()
        for job_id in new_jobs:
            set_manifest_metadata(
                job_id,
                {
                    "script_source": "git",
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
        # Rollback: remove any jobs that were partially registered during this failed load
        # to prevent them from being treated as "builtin" in future syncs
        jobs_after_error = set(job_registry._jobs.keys())
        partial_jobs = jobs_after_error - jobs_before
        if partial_jobs:
            logger.warning("Rolling back %d partially registered jobs: %s", len(partial_jobs), partial_jobs)
            for job_id in partial_jobs:
                job_registry.unregister(job_id)
        return False

    finally:
        # Clean up sys.path to avoid polluting global namespace
        if path_added and repo_root_str in sys.path:
            sys.path.remove(repo_root_str)


__all__ = [
    "load_jobs_manifest",
    "reload_manifest_jobs",
    "get_manifest_metadata",
    "set_manifest_metadata",
    "_get_builtin_job_ids",
    "_clear_job_modules",
]
