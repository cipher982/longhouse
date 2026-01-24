"""Workspace Manager – git workspace lifecycle for cloud agent execution.

This service manages git workspaces for cloud-based agent execution:
- Cloning repos to isolated workspace directories
- Creating unique branches for each run
- Capturing diffs after agent execution
- Cleanup of workspace directories

The workspace lifecycle:
1. setup() - Clone/fetch repo, create jarvis/<run_id> branch
2. Agent runs in workspace (via CloudExecutor)
3. capture_diff() - Get git diff of changes
4. cleanup() - Remove workspace directory (optional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# Allowed URL schemes for git clone (security)
ALLOWED_GIT_SCHEMES = ("https://", "ssh://", "git@")

# Regex for validating git branch names
# Allows: alphanumeric, hyphen, underscore, forward slash, dot
# Disallows: starting with hyphen/dot, consecutive dots (..), ending with .lock
BRANCH_NAME_PATTERN = re.compile(r"^(?![-.]|.*\.\.)[a-zA-Z0-9/_.-]+(?<!\.lock)$")


def validate_git_repo_url(repo_url: str) -> None:
    """Validate git repository URL for security.

    Prevents:
    - URLs starting with '-' (flag injection)
    - file:// URLs (local filesystem access)
    - Other dangerous schemes
    - SSH option injection via host portion starting with '-'

    Raises
    ------
    ValueError
        If URL is invalid or uses a disallowed scheme
    """
    if not repo_url:
        raise ValueError("Repository URL cannot be empty")

    # Prevent flag injection (URL starting with hyphen)
    if repo_url.startswith("-"):
        raise ValueError("Repository URL cannot start with '-'")

    # Check for allowed schemes
    if not any(repo_url.startswith(scheme) for scheme in ALLOWED_GIT_SCHEMES):
        raise ValueError(f"Repository URL must use one of: {', '.join(ALLOWED_GIT_SCHEMES)}. " f"Got: {repo_url[:50]}...")

    # Prevent SSH option injection via host or user portion
    # The '--' in git clone only protects git's CLI parsing, not SSH's argument parsing
    # URLs like 'ssh://-oProxyCommand=...' or 'git@-oHost:...' can inject SSH options
    # Also: 'ssh://-oProxyCommand=...@github.com/repo' injects via user portion
    if repo_url.startswith("ssh://"):
        # ssh://[user@]host[:port]/path - extract user and host
        # Remove scheme
        without_scheme = repo_url[6:]  # len("ssh://")
        # Check for user@ portion
        if "@" in without_scheme:
            user_part = without_scheme.split("@", 1)[0]
            host_part = without_scheme.split("@", 1)[1]
            # URL-decode to catch percent-encoded bypass attempts (e.g., %2D = '-')
            user_part = unquote(user_part)
            # User portion starting with '-' can inject SSH options
            if user_part.startswith("-"):
                raise ValueError("Repository URL user cannot start with '-' (SSH option injection)")
        else:
            host_part = without_scheme
        # Extract host (before : or /)
        host = host_part.split(":")[0].split("/")[0]
        # URL-decode to catch percent-encoded bypass attempts (e.g., %2D = '-')
        host = unquote(host)
        if not host:
            raise ValueError("Repository URL host cannot be empty")
        if host.startswith("-"):
            raise ValueError("Repository URL host cannot start with '-' (SSH option injection)")

    elif repo_url.startswith("git@"):
        # git@host:path or git@host/path format - extract host
        after_at = repo_url[4:]  # After "git@"
        # URL-decode FIRST to catch percent-encoded bypass attempts (e.g., %2D = '-', %3A = ':')
        after_at = unquote(after_at)

        # Host is everything before ':' or '/' (whichever comes first)
        colon_idx = after_at.find(":")
        slash_idx = after_at.find("/")

        if colon_idx == -1 and slash_idx == -1:
            raise ValueError("Invalid git@ URL format: missing path separator")

        # Find the earliest separator
        if colon_idx == -1:
            sep_idx = slash_idx
        elif slash_idx == -1:
            sep_idx = colon_idx
        else:
            sep_idx = min(colon_idx, slash_idx)

        host = after_at[:sep_idx]
        if not host:
            raise ValueError("Repository URL host cannot be empty")
        if host.startswith("-"):
            raise ValueError("Repository URL host cannot start with '-' (SSH option injection)")


def validate_branch_name(branch: str) -> None:
    """Validate git branch name for security.

    Prevents:
    - Names starting with '-' (flag injection)
    - Invalid characters that could be exploited
    - Names that look like git options

    Raises
    ------
    ValueError
        If branch name is invalid
    """
    if not branch:
        raise ValueError("Branch name cannot be empty")

    # Prevent flag injection
    if branch.startswith("-"):
        raise ValueError("Branch name cannot start with '-'")

    # Validate against pattern
    if not BRANCH_NAME_PATTERN.match(branch):
        raise ValueError(
            f"Invalid branch name: {branch}. " "Must contain only alphanumeric characters, hyphens, underscores, forward slashes, and dots."
        )


# Pattern for valid run_id: alphanumeric, hyphens, underscores only
# This prevents git argument injection via malicious run_id
_VALID_RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_run_id(run_id: str) -> None:
    """Validate run_id for security.

    The run_id is used in branch names (jarvis/{run_id}) and directory paths,
    so it must be alphanumeric with hyphens/underscores only.

    Raises
    ------
    ValueError
        If run_id is invalid
    """
    if not run_id:
        raise ValueError("run_id cannot be empty")

    if not _VALID_RUN_ID_PATTERN.match(run_id):
        raise ValueError(f"Invalid run_id: {run_id}. " "Must contain only alphanumeric characters, hyphens, and underscores.")


# Default workspace base path (overridable via env var)
DEFAULT_WORKSPACE_PATH = "/var/jarvis/workspaces"


@dataclass
class Workspace:
    """Represents an active git workspace for agent execution."""

    run_id: str
    repo_url: str
    path: Path
    branch_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    original_branch: str = "main"

    def __post_init__(self) -> None:
        # Ensure path is a Path object
        if isinstance(self.path, str):
            self.path = Path(self.path)


class WorkspaceManager:
    """Manages git workspaces for cloud agent execution."""

    def __init__(self, base_path: str | Path | None = None):
        """Initialize the workspace manager.

        Parameters
        ----------
        base_path
            Base directory for workspaces. Defaults to JARVIS_WORKSPACE_PATH env var
            or /var/jarvis/workspaces.
        """
        if base_path is None:
            base_path = os.getenv("JARVIS_WORKSPACE_PATH", DEFAULT_WORKSPACE_PATH)
        self.base_path = Path(base_path)

    async def setup(
        self,
        repo_url: str,
        run_id: str,
        *,
        base_branch: str = "main",
    ) -> Workspace:
        """Set up a git workspace for agent execution.

        This method:
        1. Creates a unique workspace directory
        2. Clones the repository (or fetches if already exists)
        3. Creates a new branch: jarvis/<run_id>
        4. Returns a Workspace object

        Parameters
        ----------
        repo_url
            Git repository URL (SSH or HTTPS)
        run_id
            Unique identifier for this execution run
        base_branch
            Branch to base the work on (default: main)

        Returns
        -------
        Workspace
            Object representing the workspace

        Raises
        ------
        RuntimeError
            If git operations fail
        ValueError
            If repo_url or base_branch are invalid (security validation)
        """
        # Security: Validate inputs before any git operations
        validate_git_repo_url(repo_url)
        validate_branch_name(base_branch)
        validate_run_id(run_id)  # Also validates jarvis/{run_id} branch name

        # Create unique workspace directory
        workspace_dir = self.base_path / run_id
        branch_name = f"jarvis/{run_id}"

        logger.info(f"Setting up workspace for run {run_id} at {workspace_dir}")

        # Ensure base directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)

        try:
            if workspace_dir.exists():
                # Workspace exists - fetch and reset
                logger.debug(f"Workspace exists, fetching latest for {run_id}")
                await self._git_fetch(workspace_dir)
                # Auto-detect default branch if using default "main"
                if base_branch == "main":
                    base_branch = await self._git_detect_default_branch(workspace_dir)
                    logger.debug(f"Detected default branch: {base_branch}")
                await self._git_checkout(workspace_dir, base_branch)
                await self._git_reset_hard(workspace_dir, f"origin/{base_branch}")
            else:
                # Clone fresh
                logger.debug(f"Cloning {repo_url} to {workspace_dir}")
                await self._git_clone(repo_url, workspace_dir)
                # Auto-detect default branch if using default "main"
                if base_branch == "main":
                    base_branch = await self._git_detect_default_branch(workspace_dir)
                    logger.debug(f"Detected default branch: {base_branch}")
                await self._git_checkout(workspace_dir, base_branch)

            # Create the jarvis branch
            await self._git_create_branch(workspace_dir, branch_name)

            workspace = Workspace(
                run_id=run_id,
                repo_url=repo_url,
                path=workspace_dir,
                branch_name=branch_name,
                original_branch=base_branch,
            )

            logger.info(f"Workspace ready: {workspace_dir} on branch {branch_name}")
            return workspace

        except Exception as e:
            logger.exception(f"Failed to set up workspace for {run_id}")
            # Clean up partial workspace on failure
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir, ignore_errors=True)
            raise RuntimeError(f"Workspace setup failed: {e}") from e

    async def capture_diff(self, workspace: Workspace) -> str:
        """Capture git diff of all changes made in the workspace.

        This generates a unified diff of all changes since the workspace
        was created, suitable for review or patching.

        Parameters
        ----------
        workspace
            The workspace to capture diff from

        Returns
        -------
        str
            Unified diff of all changes (empty string if no changes)

        Raises
        ------
        RuntimeError
            If git operations fail
        """
        # Stage all changes first
        await self._git_add_all(workspace.path)

        # Get diff of staged changes against the base branch
        diff = await self._git_diff_staged(workspace.path)

        if diff.strip():
            logger.info(f"Captured diff for {workspace.run_id}: {len(diff)} bytes")
        else:
            logger.info(f"No changes in workspace {workspace.run_id}")

        return diff

    async def commit_changes(
        self,
        workspace: Workspace,
        message: str | None = None,
    ) -> str | None:
        """Commit all changes in the workspace.

        Parameters
        ----------
        workspace
            The workspace to commit changes in
        message
            Commit message (auto-generated if not provided)

        Returns
        -------
        str | None
            Commit SHA if changes were committed, None if no changes
        """
        try:
            # Stage all changes
            await self._git_add_all(workspace.path)

            # Check if there are staged changes
            has_changes = await self._git_has_staged_changes(workspace.path)
            if not has_changes:
                logger.info(f"No changes to commit in {workspace.run_id}")
                return None

            # Generate commit message if not provided
            if not message:
                message = f"Jarvis run {workspace.run_id}\n\nAutomated changes by cloud agent execution."

            # Commit
            sha = await self._git_commit(workspace.path, message)
            logger.info(f"Committed changes for {workspace.run_id}: {sha}")
            return sha

        except Exception as e:
            logger.exception(f"Failed to commit changes for {workspace.run_id}")
            raise RuntimeError(f"Commit failed: {e}") from e

    async def push_changes(self, workspace: Workspace) -> bool:
        """Push the workspace branch to origin.

        Parameters
        ----------
        workspace
            The workspace to push

        Returns
        -------
        bool
            True if push succeeded, False if nothing to push
        """
        try:
            await self._run_git(
                workspace.path,
                ["push", "-u", "origin", workspace.branch_name],
            )
            logger.info(f"Pushed {workspace.branch_name} for {workspace.run_id}")
            return True

        except Exception as e:
            logger.exception(f"Failed to push changes for {workspace.run_id}")
            raise RuntimeError(f"Push failed: {e}") from e

    async def cleanup(self, workspace: Workspace) -> None:
        """Remove the workspace directory.

        Parameters
        ----------
        workspace
            The workspace to clean up
        """
        try:
            if workspace.path.exists():
                shutil.rmtree(workspace.path)
                logger.info(f"Cleaned up workspace {workspace.run_id}")
        except Exception as e:
            logger.warning(f"Failed to cleanup workspace {workspace.run_id}: {e}")

    def get_workspace_path(self, run_id: str) -> Path:
        """Get the path for a workspace by run_id.

        Parameters
        ----------
        run_id
            The run identifier

        Returns
        -------
        Path
            Workspace directory path
        """
        return self.base_path / run_id

    # --- Git command helpers ---

    async def _run_git(
        self,
        cwd: Path,
        args: list[str],
        *,
        capture_output: bool = True,
    ) -> str:
        """Run a git command and return output.

        Parameters
        ----------
        cwd
            Working directory
        args
            Git command arguments (without 'git' prefix)
        capture_output
            Whether to capture and return output

        Returns
        -------
        str
            Command output (stdout)

        Raises
        ------
        RuntimeError
            If command fails
        """
        cmd = ["git"] + args
        logger.debug(f"Running: {' '.join(cmd)} in {cwd}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE if capture_output else None,
            stderr=asyncio.subprocess.PIPE if capture_output else None,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Git command failed: {' '.join(args)}\n{error_msg}")

        return stdout.decode() if stdout else ""

    async def _git_clone(self, repo_url: str, dest: Path) -> None:
        """Clone a repository.

        Note: Uses '--' before repo_url to prevent flag injection attacks.
        """
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth=1",
            "--",  # End of options - prevents repo_url from being parsed as flags
            repo_url,
            str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Git clone failed: {error_msg}")

        # Re-fetch full history for branching (shallow clones limit operations)
        try:
            await self._run_git(dest, ["fetch", "--unshallow"], capture_output=False)
        except RuntimeError:
            # Already unshallow or fetch failed - not critical for MVP
            pass

    async def _git_fetch(self, cwd: Path) -> None:
        """Fetch latest from origin."""
        await self._run_git(cwd, ["fetch", "origin"])

    async def _git_detect_default_branch(self, cwd: Path) -> str:
        """Detect the default branch from origin.

        Tries to get the default branch from origin/HEAD symbolic ref.
        Falls back to checking for common branch names (main, master).

        Returns:
            The default branch name (e.g., "main" or "master")
        """
        try:
            # Try to get the default branch from symbolic ref
            output = await self._run_git(cwd, ["symbolic-ref", "refs/remotes/origin/HEAD"])
            # Output format: refs/remotes/origin/main
            if output.strip():
                return output.strip().replace("refs/remotes/origin/", "")
        except RuntimeError:
            pass

        # Fallback: check which common branches exist
        for branch in ["main", "master"]:
            try:
                await self._run_git(cwd, ["rev-parse", "--verify", f"origin/{branch}"])
                return branch
            except RuntimeError:
                continue

        # Last resort - use "main" and let it fail with a clear error
        return "main"

    async def _git_checkout(self, cwd: Path, branch: str) -> None:
        """Checkout a branch using git switch.

        Security: Branch names are validated by validate_branch_name() before
        reaching this method. Using 'git switch' instead of 'git checkout'
        avoids the ambiguity between branches and file paths.
        """
        await self._run_git(cwd, ["switch", branch])

    async def _git_reset_hard(self, cwd: Path, ref: str) -> None:
        """Hard reset to a ref."""
        await self._run_git(cwd, ["reset", "--hard", ref])

    async def _git_create_branch(self, cwd: Path, branch_name: str) -> None:
        """Create and checkout a new branch using git switch."""
        # Check if branch already exists
        try:
            await self._run_git(cwd, ["rev-parse", "--verify", branch_name])
            # Branch exists, just switch to it
            await self._run_git(cwd, ["switch", branch_name])
        except RuntimeError:
            # Branch doesn't exist, create it
            await self._run_git(cwd, ["switch", "-c", branch_name])

    async def _git_add_all(self, cwd: Path) -> None:
        """Stage all changes."""
        await self._run_git(cwd, ["add", "-A"])

    async def _git_diff_staged(self, cwd: Path) -> str:
        """Get diff of staged changes."""
        return await self._run_git(cwd, ["diff", "--staged"])

    async def _git_has_staged_changes(self, cwd: Path) -> bool:
        """Check if there are staged changes."""
        output = await self._run_git(cwd, ["diff", "--staged", "--name-only"])
        return bool(output.strip())

    async def _git_commit(self, cwd: Path, message: str) -> str:
        """Commit staged changes and return SHA."""
        await self._run_git(cwd, ["commit", "-m", message])
        return (await self._run_git(cwd, ["rev-parse", "HEAD"])).strip()


__all__ = ["WorkspaceManager", "Workspace"]
