"""Git sync service for loading job scripts from private repo.

Manages git clone and periodic sync for job scripts, with:
- Blocking clone on startup
- Periodic fetch + reset
- File-based locking for concurrent access

Single-process assumption: Lock is per-process, not distributed.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlunparse

logger = logging.getLogger(__name__)


class GitSyncError(Exception):
    """Git operation failed."""

    pass


class GitSyncService:
    """
    Manages git clone and sync for job scripts.

    Thread-safety: Uses file lock for sync operations.
    Single-process assumption: Lock is per-process, not distributed.
    """

    def __init__(
        self,
        repo_url: str,
        local_path: Path | str,
        branch: str = "main",
        token: str | None = None,
        ssh_key_path: str | None = None,
    ):
        self.repo_url = repo_url
        self.local_path = Path(local_path)
        self.branch = branch
        self.token = token
        self.ssh_key_path = ssh_key_path

        self._lock_file = self.local_path.parent / ".sauron-jobs.lock"
        self._sha_file = self.local_path.parent / ".sauron-jobs.sha"
        self._current_sha: str | None = None
        self._last_sync: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    @property
    def current_sha(self) -> str | None:
        """Current HEAD SHA. Read from file if not in memory."""
        if self._current_sha:
            return self._current_sha
        if self._sha_file.exists():
            self._current_sha = self._sha_file.read_text().strip()
        return self._current_sha

    def _get_auth_url(self) -> str:
        """Build authenticated git URL. Token is NOT logged."""
        if self.token:
            # https://token@github.com/user/repo.git
            parsed = urlparse(self.repo_url)
            authed = parsed._replace(netloc=f"{self.token}@{parsed.netloc}")
            return urlunparse(authed)
        return self.repo_url

    def _get_git_env(self) -> dict:
        """Environment for git commands."""
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",  # Never prompt
        }
        if self.ssh_key_path:
            env["GIT_SSH_COMMAND"] = f"ssh -i {self.ssh_key_path} -o StrictHostKeyChecking=accept-new"
        return env

    @asynccontextmanager
    async def _file_lock(self, exclusive: bool = True):
        """
        Acquire file lock for git operations.

        exclusive=True: For sync operations (blocks readers)
        exclusive=False: For load operations (allows concurrent reads)
        """
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH

        fd = os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT)
        try:
            # Non-blocking attempt first, then blocking
            try:
                fcntl.flock(fd, lock_mode | fcntl.LOCK_NB)
            except BlockingIOError:
                # Already locked, wait
                logger.debug("Waiting for %s lock...", "exclusive" if exclusive else "shared")
                await asyncio.to_thread(fcntl.flock, fd, lock_mode)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def ensure_cloned(self) -> None:
        """
        Clone repo if not present. BLOCKING - Zerg won't start without it.

        Raises:
            GitSyncError: If clone fails
        """
        async with self._file_lock(exclusive=True):
            if (self.local_path / ".git").exists():
                logger.info("Jobs repo already cloned at %s", self.local_path)
                self._current_sha = await self._get_head_sha()
                self._write_sha()
                return

            logger.info("Cloning jobs repo: %s -> %s", self._safe_url(), self.local_path)

            # Ensure parent exists
            self.local_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                await self._run_git(
                    [
                        "clone",
                        "--single-branch",
                        f"--branch={self.branch}",
                        self._get_auth_url(),  # Token in URL, not logged
                        str(self.local_path),
                    ]
                )

                # Configure safe.directory for container environments
                await self._run_git(["config", "--global", "--add", "safe.directory", str(self.local_path)])

                self._current_sha = await self._get_head_sha()
                self._write_sha()
                logger.info("Jobs repo cloned successfully: %s", self._current_sha[:8])

            except Exception as e:
                raise GitSyncError(f"Failed to clone jobs repo: {e}") from e

    async def refresh(self) -> dict:
        """
        Fetch and reset to latest. Returns sync status.

        Uses exclusive lock to prevent job loading during sync.
        """
        async with self._file_lock(exclusive=True):
            old_sha = self._current_sha

            try:
                # Fetch latest
                await self._run_git(["fetch", "origin", self.branch], cwd=self.local_path)

                # Hard reset to origin (handles force pushes)
                await self._run_git(["reset", "--hard", f"origin/{self.branch}"], cwd=self.local_path)

                self._current_sha = await self._get_head_sha()
                self._write_sha()
                self._last_sync = datetime.now(UTC)
                self._last_error = None
                self._consecutive_failures = 0

                changed = old_sha != self._current_sha
                if changed:
                    logger.info(
                        "Jobs repo updated: %s -> %s",
                        old_sha[:8] if old_sha else "None",
                        self._current_sha[:8],
                    )

                return {
                    "success": True,
                    "previous_sha": old_sha,
                    "current_sha": self._current_sha,
                    "changed": changed,
                    "synced_at": self._last_sync.isoformat(),
                }

            except Exception as e:
                self._last_error = str(e)
                self._consecutive_failures += 1
                logger.error("Git sync failed (%dx): %s", self._consecutive_failures, e)
                return {
                    "success": False,
                    "error": str(e),
                    "consecutive_failures": self._consecutive_failures,
                }

    @asynccontextmanager
    async def read_lock(self):
        """
        Acquire shared read lock for job loading.

        Multiple jobs can load concurrently, but sync is blocked.
        """
        async with self._file_lock(exclusive=False):
            yield self._current_sha

    async def _get_head_sha(self) -> str:
        result = await self._run_git(["rev-parse", "HEAD"], cwd=self.local_path, capture=True)
        return result.strip()

    def _write_sha(self) -> None:
        """Persist SHA to file for crash recovery."""
        if self._current_sha:
            self._sha_file.write_text(self._current_sha)

    def _safe_url(self) -> str:
        """Return URL without token for logging."""
        if "@" in self.repo_url:
            # Already has credentials, strip them
            parsed = urlparse(self.repo_url)
            return urlunparse(parsed._replace(netloc=parsed.netloc.split("@")[-1]))
        return self.repo_url

    async def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        capture: bool = False,
    ) -> str:
        """Run git command. Never logs the full URL (may contain token)."""
        cmd = ["git", *args]

        # Log sanitized command (hide token)
        safe_args = ["[REDACTED]" if "ghp_" in a or "@" in a else a for a in args]
        logger.debug("Running: git %s", " ".join(safe_args))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._get_git_env(),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            # Sanitize error message too
            err_msg = stderr.decode().replace(self.token or "", "[TOKEN]")
            raise GitSyncError(f"git {args[0]} failed: {err_msg}")

        return stdout.decode() if stdout else ""

    def get_status(self) -> dict:
        """Get current sync status for API/monitoring."""
        return {
            "current_sha": self._current_sha,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "repo_url": self._safe_url(),
            "branch": self.branch,
            "local_path": str(self.local_path),
        }


async def run_git_sync_loop(
    service: GitSyncService,
    interval_seconds: int,
    error_backoff_seconds: int = 300,
) -> None:
    """
    Background task that periodically syncs git repo.

    - Skips if interval_seconds <= 0
    - Backs off on repeated failures
    """
    if interval_seconds <= 0:
        logger.info("Git sync polling disabled (interval=0)")
        return

    logger.info("Starting git sync loop (interval=%ds)", interval_seconds)

    while True:
        result = await service.refresh()

        if result.get("success"):
            wait = interval_seconds
        else:
            # Exponential backoff capped at error_backoff_seconds
            wait = min(
                interval_seconds * (2 ** result.get("consecutive_failures", 1)),
                error_backoff_seconds,
            )
            logger.warning("Git sync failed, backing off %ds", wait)

        await asyncio.sleep(wait)


# Global instance (initialized by startup)
_git_sync_service: GitSyncService | None = None


def get_git_sync_service() -> GitSyncService | None:
    """Get the global git sync service instance."""
    return _git_sync_service


def set_git_sync_service(service: GitSyncService) -> None:
    """Set the global git sync service instance."""
    global _git_sync_service
    _git_sync_service = service


__all__ = [
    "GitSyncError",
    "GitSyncService",
    "get_git_sync_service",
    "run_git_sync_loop",
    "set_git_sync_service",
]
