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

    Concurrency-safety: Uses file lock for sync operations.
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
            # Token auth only works with HTTPS URLs, not SSH
            if self.repo_url.startswith("git@") or self.repo_url.startswith("ssh://"):
                logger.warning("Token auth ignored for SSH repo URL; use ssh_key_path instead")
                return self.repo_url
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
        # Allow file:// protocol (needed for local/CI testing with bare repos).
        # Git 2.38.1+ blocks file:// by default (CVE-2022-39253).
        if self.repo_url.startswith("file://"):
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "protocol.file.allow"
            env["GIT_CONFIG_VALUE_0"] = "always"

        if self.ssh_key_path:
            env["GIT_SSH_COMMAND"] = f"ssh -i {self.ssh_key_path} -o StrictHostKeyChecking=accept-new"
        return env

    async def _ensure_remote_url(self) -> None:
        """Ensure origin remote points to the configured repo_url.

        Called inside ensure_cloned (under exclusive lock) when the repo
        directory already exists. Handles:
        - Hot-start with a changed config (different URL)
        - Bootstrap-created repos that have no origin remote
        """
        try:
            current_url = (await self._run_git(["remote", "get-url", "origin"], cwd=self.local_path, capture=True)).strip()
        except Exception:
            current_url = ""

        expected_url = self._get_auth_url()
        if current_url == expected_url:
            return

        if current_url:
            logger.info("Remote URL changed, updating origin: %s", self._safe_url())
            await self._run_git(["remote", "set-url", "origin", expected_url], cwd=self.local_path)
        else:
            logger.info("Adding origin remote: %s", self._safe_url())
            await self._run_git(["remote", "add", "origin", expected_url], cwd=self.local_path)

        # Fetch + reset to pick up remote content
        await self._run_git(["fetch", "origin", self.branch], cwd=self.local_path)
        await self._run_git(["checkout", "-B", self.branch, f"origin/{self.branch}"], cwd=self.local_path)

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

        If the repo already exists but the remote URL has changed (e.g. hot-start
        with a new config), updates the remote URL and does a fresh fetch+reset.

        Raises:
            GitSyncError: If clone fails
        """
        async with self._file_lock(exclusive=True):
            if (self.local_path / ".git").exists():
                # Repo exists — check if remote URL matches
                await self._ensure_remote_url()
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
        # -c safe.directory=* on the command line is the most reliable way
        # to bypass ownership checks in container environments where UIDs differ.
        cmd = ["git", "-c", "safe.directory=*", *args]

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
            # Sanitize error message (only replace token if actually set)
            err_msg = stderr.decode()
            if self.token:
                err_msg = err_msg.replace(self.token, "[TOKEN]")
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


def _update_sync_status(*, sha: str | None, error: str | None) -> None:
    """Persist sync status to JobRepoConfig in the DB.

    Runs synchronously (called from async context via best-effort).
    Failures are logged but never raised — sync status is informational.
    """
    try:
        from zerg.database import db_session
        from zerg.models.models import JobRepoConfig

        with db_session() as db:
            row = db.query(JobRepoConfig).first()
            if not row:
                return
            row.last_sync_sha = sha
            row.last_sync_at = datetime.now(UTC)
            row.last_sync_error = error
            db.commit()
    except Exception:
        logger.debug("Failed to update sync status in DB", exc_info=True)


async def run_git_sync_loop(
    service: GitSyncService,
    interval_seconds: int,
    error_backoff_seconds: int = 300,
) -> None:
    """
    Background task that periodically syncs git repo.

    - Skips if interval_seconds <= 0
    - Backs off on repeated failures
    - On SHA change: reinstalls deps, reloads manifest, resyncs scheduler
    """
    if interval_seconds <= 0:
        logger.info("Git sync polling disabled (interval=0)")
        return

    logger.info("Starting git sync loop (interval=%ds)", interval_seconds)

    while True:
        result = await service.refresh()

        if result.get("success"):
            # Persist success status
            _update_sync_status(sha=result.get("current_sha"), error=None)

            # On SHA change: full reload (deps + manifest + scheduler)
            if result.get("changed"):
                try:
                    from zerg.jobs.loader import reload_manifest_jobs

                    reload_result = await reload_manifest_jobs()
                    logger.info("Post-sync reload: %s", reload_result)
                except Exception:
                    logger.exception("Failed to reload manifest after git sync")
                    _update_sync_status(
                        sha=result.get("current_sha"),
                        error="manifest reload failed after sync",
                    )

            wait = interval_seconds
        else:
            # Persist failure status
            _update_sync_status(sha=None, error=result.get("error", "unknown"))

            # Exponential backoff capped at error_backoff_seconds
            wait = min(
                interval_seconds * (2 ** result.get("consecutive_failures", 1)),
                error_backoff_seconds,
            )
            logger.warning("Git sync failed, backing off %ds", wait)

        await asyncio.sleep(wait)


# Global instance (initialized by startup)
_git_sync_service: GitSyncService | None = None
_git_sync_task: asyncio.Task | None = None


def get_git_sync_service() -> GitSyncService | None:
    """Get the global git sync service instance."""
    return _git_sync_service


def set_git_sync_service(service: GitSyncService) -> None:
    """Set the global git sync service instance."""
    global _git_sync_service
    _git_sync_service = service


def set_git_sync_task(task: asyncio.Task) -> None:
    """Store the background sync loop task handle."""
    global _git_sync_task
    _git_sync_task = task


async def stop_git_sync() -> None:
    """Cancel the running sync loop task (if any) and wait for it to finish.

    Safe to call even if no sync is running.
    """
    global _git_sync_service, _git_sync_task

    if _git_sync_task and not _git_sync_task.done():
        _git_sync_task.cancel()
        try:
            await _git_sync_task
        except (asyncio.CancelledError, Exception):
            pass  # Expected — task was cancelled
        logger.info("Cancelled git sync loop task")
    _git_sync_task = None
    _git_sync_service = None


async def replace_git_sync_service(
    service: GitSyncService,
    interval_seconds: int,
) -> None:
    """Stop existing sync, clone new repo, start new sync loop.

    Used by the hot-start path (API endpoint saves config → starts sync).
    Guards against duplicate loops by cancelling the old task first.
    """
    await stop_git_sync()

    await service.ensure_cloned()
    set_git_sync_service(service)

    # Write initial sync status to DB
    _update_sync_status(sha=service.current_sha, error=None)

    if interval_seconds > 0:
        task = asyncio.create_task(run_git_sync_loop(service, interval_seconds))
        set_git_sync_task(task)

    # Do initial manifest load
    from zerg.jobs.loader import reload_manifest_jobs

    reload_result = await reload_manifest_jobs()
    logger.info("Hot-start reload result: %s", reload_result)


__all__ = [
    "GitSyncError",
    "GitSyncService",
    "get_git_sync_service",
    "replace_git_sync_service",
    "run_git_sync_loop",
    "set_git_sync_service",
    "set_git_sync_task",
    "stop_git_sync",
]
