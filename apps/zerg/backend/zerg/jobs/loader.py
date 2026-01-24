"""Job loader for multi-source job execution.

Handles loading job functions from:
- builtin: Standard Python imports from zerg package
- git: Dynamic loading from cloned private repo
- http: Declarative HTTP DSL (no Python)

Security: Path validation prevents directory traversal attacks.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import re
import sys
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Awaitable
from typing import Callable

if TYPE_CHECKING:
    from zerg.jobs.git_sync import GitSyncService

logger = logging.getLogger(__name__)


class JobLoadError(Exception):
    """Failed to load job script."""

    pass


# Valid entrypoint patterns for git jobs
# Must start with jobs/, contain only word chars and slashes, optionally end with .py
ENTRYPOINT_PATTERN = re.compile(r"^jobs/[\w/]+(?:\.py)?$")


def validate_entrypoint(entrypoint: str, jobs_dir: Path) -> Path:
    """
    Validate entrypoint path to prevent directory traversal.

    Valid:
      - jobs/backup_sentinel.py
      - jobs/worklog (package)
      - jobs/subdir/task.py

    Invalid:
      - ../etc/passwd
      - /absolute/path
      - jobs/../../../etc/passwd
      - jobs/foo;rm -rf /

    Args:
        entrypoint: Relative path within jobs dir
        jobs_dir: Root directory of cloned repo

    Returns:
        Resolved absolute path to the script/package

    Raises:
        ValueError: If path is invalid or escapes jobs_dir
    """
    # Must match pattern
    if not ENTRYPOINT_PATTERN.match(entrypoint):
        raise ValueError(f"Invalid entrypoint format: {entrypoint}")

    # Resolve and check it's within jobs_dir
    resolved = (jobs_dir / entrypoint).resolve()
    jobs_root = (jobs_dir / "jobs").resolve()

    if not str(resolved).startswith(str(jobs_root)):
        raise ValueError(f"Entrypoint escapes jobs directory: {entrypoint}")

    # Must exist as file or package
    if resolved.exists():
        return resolved

    # Try with .py extension
    if resolved.with_suffix(".py").exists():
        return resolved.with_suffix(".py")

    # Try as package (directory with __init__.py)
    if (resolved / "__init__.py").exists():
        return resolved

    raise ValueError(f"Entrypoint not found: {entrypoint}")


def load_job_func_from_file(script_path: Path, jobs_dir: Path) -> Callable[[], Awaitable[dict[str, Any] | None]]:
    """
    Load job function from file with fresh import (no caching).

    Handles both single-file jobs and packages:
      - jobs/foo.py -> loads foo.py, returns run()
      - jobs/bar/ -> loads bar/__init__.py, returns run()

    IMPORTANT: Does NOT add to sys.path permanently.
    Uses isolated module loading to prevent pollution.

    Args:
        script_path: Resolved path to script or package
        jobs_dir: Root directory of cloned repo (for lib/ imports)

    Returns:
        Async run() function from the job module

    Raises:
        JobLoadError: If script can't be loaded or missing run()
    """
    # Determine actual file to load
    if script_path.is_dir():
        # Package - load __init__.py
        init_file = script_path / "__init__.py"
        if not init_file.exists():
            raise JobLoadError(f"Package missing __init__.py: {script_path}")
        load_path = init_file
        module_name = f"sauron_job_{script_path.name}"
    elif script_path.suffix == ".py":
        load_path = script_path
        module_name = f"sauron_job_{script_path.stem}"
    else:
        # Try adding .py
        load_path = script_path.with_suffix(".py")
        module_name = f"sauron_job_{script_path.stem}"

    if not load_path.exists():
        raise JobLoadError(f"Script not found: {load_path}")

    # Generate unique module name to prevent caching issues
    unique_name = f"{module_name}_{int(time.time() * 1000)}"

    # Load module from file (isolated, not cached in sys.modules)
    spec = importlib.util.spec_from_file_location(
        unique_name,
        load_path,
        submodule_search_locations=[str(script_path.parent), str(jobs_dir / "lib")],
    )

    if spec is None or spec.loader is None:
        raise JobLoadError(f"Cannot load spec for: {load_path}")

    module = importlib.util.module_from_spec(spec)

    # Temporarily add lib to path for imports within the module
    lib_dir = str(jobs_dir / "lib")
    jobs_parent = str(jobs_dir / "jobs")

    old_path = sys.path.copy()
    try:
        # Prepend paths for this load only
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        if jobs_parent not in sys.path:
            sys.path.insert(0, jobs_parent)

        spec.loader.exec_module(module)
    except Exception as e:
        raise JobLoadError(f"Failed to execute module {load_path}: {e}") from e
    finally:
        # Restore original sys.path
        sys.path = old_path

    # Get run function
    if not hasattr(module, "run"):
        raise JobLoadError(f"Script missing run() function: {load_path}")

    func = getattr(module, "run")

    # Validate it's async
    if not asyncio.iscoroutinefunction(func):
        raise JobLoadError(f"run() must be async: {load_path}")

    return func


async def load_builtin_job(entrypoint: str) -> tuple[Callable, dict[str, Any]]:
    """
    Load a builtin job from the zerg package.

    Args:
        entrypoint: Python module path (e.g., "zerg.jobs.builtin.qa_agent")

    Returns:
        (run function, metadata dict)
    """
    try:
        module = importlib.import_module(entrypoint)
    except ImportError as e:
        raise JobLoadError(f"Cannot import builtin job: {entrypoint}: {e}") from e

    if not hasattr(module, "run"):
        raise JobLoadError(f"Builtin job missing run(): {entrypoint}")

    func = getattr(module, "run")

    try:
        version = importlib.metadata.version("zerg")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    return func, {
        "script_source": "builtin",
        "entrypoint": entrypoint,
        "script_sha": f"builtin:{version}",
    }


async def load_git_job(
    entrypoint: str,
    git_service: "GitSyncService",
) -> tuple[Callable, dict[str, Any]]:
    """
    Load a git job from the cloned private repo.

    Args:
        entrypoint: Relative path (e.g., "jobs/backup_sentinel.py")
        git_service: GitSyncService instance

    Returns:
        (run function, metadata dict)
    """
    jobs_dir = Path(git_service.local_path)

    # Validate entrypoint
    script_path = validate_entrypoint(entrypoint, jobs_dir)

    # Acquire read lock and get current SHA
    async with git_service.read_lock() as sha:
        func = load_job_func_from_file(script_path, jobs_dir)
        loaded_at = datetime.now(UTC)

    return func, {
        "script_source": "git",
        "entrypoint": entrypoint,
        "script_sha": sha,
        "loaded_at": loaded_at.isoformat(),
    }


async def load_http_job(config: dict[str, Any]) -> tuple[Callable, dict[str, Any]]:
    """
    Load an HTTP DSL job from config.

    Args:
        config: HTTP DSL configuration dict

    Returns:
        (executor function, metadata dict)
    """
    from zerg.jobs.http_dsl import create_http_executor

    func = create_http_executor(config)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]

    return func, {
        "script_source": "http",
        "config_hash": config_hash,
    }


class JobRow:
    """Job definition row from database (type stub for loader)."""

    def __init__(
        self,
        job_id: str,
        script_source: str,
        entrypoint: str | None,
        config: dict[str, Any] | None,
    ):
        self.job_id = job_id
        self.script_source = script_source
        self.entrypoint = entrypoint
        self.config = config or {}


async def load_job_func(
    job: JobRow,
    git_service: "GitSyncService | None" = None,
) -> tuple[Callable, dict[str, Any]]:
    """
    Load job function based on script_source.

    Args:
        job: Job row with script_source, entrypoint, config
        git_service: GitSyncService instance (required for git jobs)

    Returns:
        (callable, metadata_dict) - Function to execute and audit metadata

    Raises:
        JobLoadError: If job can't be loaded
    """
    if job.script_source == "builtin":
        if not job.entrypoint:
            raise JobLoadError(f"Builtin job {job.job_id} missing entrypoint")
        return await load_builtin_job(job.entrypoint)

    elif job.script_source == "git":
        if not job.entrypoint:
            raise JobLoadError(f"Git job {job.job_id} missing entrypoint")
        if not git_service:
            raise JobLoadError(f"Git job {job.job_id} requires git_service")
        return await load_git_job(job.entrypoint, git_service)

    elif job.script_source == "http":
        if not job.config:
            raise JobLoadError(f"HTTP job {job.job_id} missing config")
        return await load_http_job(job.config)

    else:
        raise JobLoadError(f"Unknown script_source: {job.script_source}")


__all__ = [
    "JobLoadError",
    "JobRow",
    "load_builtin_job",
    "load_git_job",
    "load_http_job",
    "load_job_func",
    "load_job_func_from_file",
    "validate_entrypoint",
]
