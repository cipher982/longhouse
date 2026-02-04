"""Jobs repository bootstrap service.

Manages the local jobs repository in /data/jobs/:
- Auto-creates directory structure on first boot
- Initializes git repo for local versioning
- Provides status and commit helpers

This is part of Phase 3 of the hosted platform spec (SDP-1).
Jobs live as Python code in /data/jobs/, versioned locally with git.
Remote sync (e.g., GitHub) is optional and configured via UI.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default manifest template for new instances
MANIFEST_TEMPLATE = '''"""
Longhouse Jobs Manifest

Register your jobs here. Example:

from zerg.jobs import job_registry, JobConfig
from jobs.my_job import run as my_job_func

job_registry.register(JobConfig(
    id="my-job",
    cron="0 8 * * *",
    func=my_job_func,
    description="My scheduled job",
))
"""

# Your jobs go here
'''


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    """Run a git command and return (success, output).

    Args:
        args: Git command arguments (without 'git' prefix)
        cwd: Working directory for the command

    Returns:
        Tuple of (success, stdout/stderr combined)
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "git command timed out"
    except FileNotFoundError:
        return False, "git not found in PATH"
    except Exception as e:
        return False, str(e)


def bootstrap_jobs_repo(data_dir: str | Path) -> dict[str, Any]:
    """Bootstrap the jobs repository on first boot.

    Creates:
    - /data/jobs/ directory
    - /data/jobs/manifest.py with starter template
    - /data/jobs/jobs/__init__.py for user job modules
    - Git repository (if not already initialized)

    Args:
        data_dir: Base data directory (e.g., /data or repo_root/data)

    Returns:
        Dict with bootstrap results:
        - created: bool - True if any files were created
        - initialized_git: bool - True if git init was run
        - errors: list[str] - Any errors encountered
    """
    data_path = Path(data_dir)
    jobs_dir = data_path / "jobs"
    manifest_path = jobs_dir / "manifest.py"
    jobs_modules_dir = jobs_dir / "jobs"
    jobs_init_path = jobs_modules_dir / "__init__.py"
    gitignore_path = jobs_dir / ".gitignore"

    result: dict[str, Any] = {
        "created": False,
        "initialized_git": False,
        "errors": [],
        "jobs_dir": str(jobs_dir),
    }

    try:
        # Create jobs directory
        if not jobs_dir.exists():
            jobs_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created jobs directory: %s", jobs_dir)
            result["created"] = True

        # Create manifest.py template
        if not manifest_path.exists():
            manifest_path.write_text(MANIFEST_TEMPLATE)
            logger.info("Created starter manifest: %s", manifest_path)
            result["created"] = True

        # Create jobs/ subdirectory for user modules
        if not jobs_modules_dir.exists():
            jobs_modules_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Created jobs modules directory: %s", jobs_modules_dir)
            result["created"] = True

        # Create __init__.py in jobs/ subdirectory
        if not jobs_init_path.exists():
            jobs_init_path.write_text('"""User job modules."""\n')
            logger.info("Created jobs/__init__.py: %s", jobs_init_path)
            result["created"] = True

        # Create .gitignore
        if not gitignore_path.exists():
            gitignore_content = """# Python
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/

# Environment
.env
.venv/
"""
            gitignore_path.write_text(gitignore_content)
            logger.info("Created .gitignore: %s", gitignore_path)
            result["created"] = True

        # Initialize git repository
        git_dir = jobs_dir / ".git"
        if not git_dir.exists():
            success, output = _run_git(["init"], jobs_dir)
            if success:
                logger.info("Initialized git repository in %s", jobs_dir)
                result["initialized_git"] = True

                # Configure git user for commits (use generic values)
                _run_git(["config", "user.email", "longhouse@localhost"], jobs_dir)
                _run_git(["config", "user.name", "Longhouse"], jobs_dir)

                # Initial commit if we created files
                if result["created"]:
                    _run_git(["add", "-A"], jobs_dir)
                    _run_git(["commit", "-m", "Initial jobs repository"], jobs_dir)
                    logger.info("Created initial commit")
            else:
                logger.warning("Failed to initialize git: %s", output)
                result["errors"].append(f"git init failed: {output}")

    except PermissionError as e:
        error_msg = f"Permission denied creating jobs directory: {e}"
        logger.error(error_msg)
        result["errors"].append(error_msg)
    except Exception as e:
        error_msg = f"Failed to bootstrap jobs repo: {e}"
        logger.exception(error_msg)
        result["errors"].append(error_msg)

    return result


def get_repo_status(data_dir: str | Path) -> dict[str, Any]:
    """Get the status of the jobs repository.

    Args:
        data_dir: Base data directory (e.g., /data)

    Returns:
        Dict with repository status:
        - initialized: bool - True if git repo exists
        - has_remote: bool - True if a remote is configured
        - remote_url: str | None - Remote URL if configured
        - last_commit: dict | None - Last commit info (sha, message, date)
        - dirty: bool - True if there are uncommitted changes
        - files: int - Number of tracked files
    """
    data_path = Path(data_dir)
    jobs_dir = data_path / "jobs"
    git_dir = jobs_dir / ".git"

    result: dict[str, Any] = {
        "initialized": False,
        "has_remote": False,
        "remote_url": None,
        "last_commit": None,
        "dirty": False,
        "files": 0,
        "jobs_dir": str(jobs_dir),
        "exists": jobs_dir.exists(),
    }

    if not jobs_dir.exists():
        return result

    # Check if git is initialized
    result["initialized"] = git_dir.exists()

    if not result["initialized"]:
        return result

    # Get remote URL
    success, output = _run_git(["remote", "get-url", "origin"], jobs_dir)
    if success and output:
        result["has_remote"] = True
        result["remote_url"] = output

    # Get last commit info
    success, output = _run_git(
        ["log", "-1", "--format=%H%n%s%n%aI"],
        jobs_dir,
    )
    if success and output:
        lines = output.split("\n")
        if len(lines) >= 3:
            result["last_commit"] = {
                "sha": lines[0],
                "message": lines[1],
                "date": lines[2],
            }

    # Check for uncommitted changes
    success, output = _run_git(["status", "--porcelain"], jobs_dir)
    if success:
        result["dirty"] = bool(output.strip())

    # Count tracked files
    success, output = _run_git(["ls-files"], jobs_dir)
    if success:
        files = [f for f in output.split("\n") if f.strip()]
        result["files"] = len(files)

    return result


def commit_changes(data_dir: str | Path, message: str) -> dict[str, Any]:
    """Commit all changes in the jobs repository.

    Stages all changes (new, modified, deleted files) and creates a commit.

    Args:
        data_dir: Base data directory (e.g., /data)
        message: Commit message

    Returns:
        Dict with commit results:
        - success: bool - True if commit was created
        - sha: str | None - Commit SHA if successful
        - error: str | None - Error message if failed
        - no_changes: bool - True if there was nothing to commit
    """
    data_path = Path(data_dir)
    jobs_dir = data_path / "jobs"
    git_dir = jobs_dir / ".git"

    result: dict[str, Any] = {
        "success": False,
        "sha": None,
        "error": None,
        "no_changes": False,
    }

    if not git_dir.exists():
        result["error"] = "Jobs repository not initialized"
        return result

    # Check for changes
    success, output = _run_git(["status", "--porcelain"], jobs_dir)
    if success and not output.strip():
        result["no_changes"] = True
        result["success"] = True  # No error, just nothing to do
        return result

    # Stage all changes
    success, output = _run_git(["add", "-A"], jobs_dir)
    if not success:
        result["error"] = f"Failed to stage changes: {output}"
        return result

    # Create commit
    success, output = _run_git(["commit", "-m", message], jobs_dir)
    if not success:
        # Check if it failed because nothing to commit (race condition)
        if "nothing to commit" in output.lower():
            result["no_changes"] = True
            result["success"] = True
            return result
        result["error"] = f"Failed to create commit: {output}"
        return result

    # Get commit SHA
    success, sha = _run_git(["rev-parse", "HEAD"], jobs_dir)
    if success:
        result["sha"] = sha.strip()

    result["success"] = True
    logger.info("Created commit %s: %s", result["sha"][:8] if result["sha"] else "?", message)
    return result


def auto_commit_if_dirty(data_dir: str | Path, context: str = "auto") -> dict[str, Any]:
    """Commit changes if repository has uncommitted changes.

    Convenience wrapper around commit_changes that:
    1. Checks if there are uncommitted changes
    2. If so, creates an auto-commit with timestamp

    Args:
        data_dir: Base data directory
        context: Context string for commit message (e.g., "job edit", "manifest update")

    Returns:
        Dict with results (same as commit_changes, plus 'skipped' if clean)
    """
    status = get_repo_status(data_dir)

    if not status["initialized"]:
        return {"success": False, "error": "Repository not initialized", "skipped": False}

    if not status["dirty"]:
        return {"success": True, "skipped": True, "no_changes": True}

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = f"Auto-commit ({context}) - {timestamp}"

    return commit_changes(data_dir, message)


__all__ = [
    "bootstrap_jobs_repo",
    "get_repo_status",
    "commit_changes",
    "auto_commit_if_dirty",
    "MANIFEST_TEMPLATE",
]
