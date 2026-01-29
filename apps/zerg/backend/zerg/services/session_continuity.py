"""Session continuity service for cross-environment Claude Code session resumption.

This service enables seamless --resume of Claude Code sessions across environments:
- Laptop terminal -> Zerg commis
- Zerg commis -> Laptop terminal
- Zerg commis -> Zerg commis

Sessions are stored in Zerg's local database and can be fetched/shipped via the
/api/agents endpoints.

Key insight: Claude Code path encoding is deterministic:
    encoded_cwd = re.sub(r'[^A-Za-z0-9-]', '-', absolute_path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Zerg API configuration (local by default)
ZERG_API_URL = os.getenv("ZERG_API_URL", "http://localhost:47300")

# Valid session ID pattern (alphanumeric, dashes, underscores only)
# Prevents path traversal attacks via malicious session IDs
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def get_claude_config_dir() -> Path:
    """Get the Claude config directory, respecting CLAUDE_CONFIG_DIR env var.

    Priority:
    1. CLAUDE_CONFIG_DIR environment variable
    2. ~/.claude (default)
    """
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".claude"


def validate_session_id(session_id: str) -> None:
    """Validate session ID to prevent path traversal attacks.

    Args:
        session_id: The session ID to validate

    Raises:
        ValueError: If session ID contains unsafe characters
    """
    if not session_id:
        raise ValueError("Session ID cannot be empty")
    if not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session ID format: {session_id}")
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError(f"Session ID contains path traversal characters: {session_id}")


def encode_cwd_for_claude(absolute_path: str) -> str:
    """Encode a working directory path using Claude Code's algorithm.

    Claude Code stores sessions at ~/.claude/projects/{encoded_cwd}/{sessionId}.jsonl
    where encoded_cwd is the absolute path with non-alphanumeric chars replaced by dashes.

    Args:
        absolute_path: Absolute path to the working directory

    Returns:
        Encoded path string matching Claude Code's encoding
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", absolute_path)


async def fetch_session_from_zerg(session_id: str) -> tuple[bytes, str, str]:
    """Fetch a session from Zerg for resumption.

    Args:
        session_id: Session UUID

    Returns:
        Tuple of (jsonl_bytes, cwd, provider_session_id)

    Raises:
        ValueError: If session not found or API error
        httpx.HTTPError: On network errors
    """
    url = f"{ZERG_API_URL}/api/agents/sessions/{session_id}/export"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url)

        if response.status_code == 404:
            raise ValueError(f"Session {session_id} not found")

        response.raise_for_status()

        # Extract metadata from headers
        cwd = response.headers.get("X-Session-CWD", "")
        provider_session_id = response.headers.get("X-Provider-Session-ID", "")

        # Validate provider_session_id to prevent path traversal
        if provider_session_id:
            validate_session_id(provider_session_id)

        return response.content, cwd, provider_session_id


# Backwards compatibility alias
async def fetch_session_from_life_hub(session_id: str) -> tuple[bytes, str, str]:
    """Alias for fetch_session_from_zerg for backwards compatibility."""
    return await fetch_session_from_zerg(session_id)


async def prepare_session_for_resume(
    session_id: str,
    workspace_path: Path,
    claude_config_dir: Path | None = None,
) -> str:
    """Fetch session from Zerg and prepare it for Claude Code --resume.

    Downloads the session JSONL and places it at the path Claude Code expects:
    {claude_config_dir}/projects/{encoded_cwd}/{provider_session_id}.jsonl

    Args:
        session_id: Session UUID to fetch
        workspace_path: The workspace directory where Claude Code will run
        claude_config_dir: Override for Claude config dir (default: from CLAUDE_CONFIG_DIR or ~/.claude)

    Returns:
        The provider_session_id to pass to --resume flag

    Raises:
        ValueError: If session not found or configuration error
    """
    # Fetch session from Zerg
    jsonl_bytes, original_cwd, provider_session_id = await fetch_session_from_zerg(session_id)

    if not provider_session_id:
        raise ValueError(f"Session {session_id} has no provider_session_id - cannot resume")

    # Validate provider_session_id to prevent path traversal (defense in depth)
    validate_session_id(provider_session_id)

    # Determine Claude config directory (respects CLAUDE_CONFIG_DIR env var)
    config_dir = claude_config_dir or get_claude_config_dir()

    # Use workspace path for the encoded_cwd (where the new session will run)
    # This allows resuming a session that started in a different directory
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))

    # Create the projects directory
    session_dir = config_dir / "projects" / encoded_cwd
    session_dir.mkdir(parents=True, exist_ok=True)

    # Write the session file
    session_file = session_dir / f"{provider_session_id}.jsonl"
    session_file.write_bytes(jsonl_bytes)

    logger.info(f"Prepared session {session_id} for resume at {session_file}")

    return provider_session_id


async def ship_session_to_zerg(
    workspace_path: Path,
    commis_id: str,
    claude_config_dir: Path | None = None,
) -> str | None:
    """Ship a Claude Code session from workspace to Zerg.

    Finds the most recent session file in the workspace's Claude config
    and ships it to Zerg for future resumption.

    Args:
        workspace_path: The workspace directory where Claude Code ran
        commis_id: Commis ID for logging/tracking
        claude_config_dir: Override for Claude config dir (default: from CLAUDE_CONFIG_DIR or ~/.claude)

    Returns:
        The session ID if shipped successfully, None otherwise
    """
    # Determine Claude config directory (respects CLAUDE_CONFIG_DIR env var)
    config_dir = claude_config_dir or get_claude_config_dir()

    # Find session file for this workspace
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
    session_dir = config_dir / "projects" / encoded_cwd

    if not session_dir.exists():
        logger.debug(f"No Claude sessions found for workspace {workspace_path}")
        return None

    # Find most recent .jsonl file
    session_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not session_files:
        logger.debug(f"No session files found in {session_dir}")
        return None

    session_file = session_files[0]
    provider_session_id = session_file.stem

    logger.info(f"Shipping session {provider_session_id} for commis {commis_id}")

    # Read session content as bytes to track offsets for dedup
    session_bytes = session_file.read_bytes()
    session_content = session_bytes.decode("utf-8", errors="replace")

    # Parse JSONL and build ingest payload with byte offsets for dedup
    events = []
    byte_offset = 0
    for line in session_content.splitlines(keepends=True):
        line_stripped = line.strip()
        if not line_stripped:
            byte_offset += len(line.encode("utf-8"))
            continue
        try:
            event = json.loads(line_stripped)
            events.append(
                {
                    "role": event.get("role", "assistant"),
                    "content_text": event.get("content"),
                    "tool_name": event.get("tool_name"),
                    "tool_input_json": event.get("tool_input"),
                    "tool_output_text": event.get("tool_output"),
                    "timestamp": event.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    "source_path": str(session_file),
                    "source_offset": byte_offset,  # Byte offset for dedup
                    "raw_json": line_stripped,  # Original line for lossless archiving
                }
            )
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSONL line: {line_stripped[:100]}")
        byte_offset += len(line.encode("utf-8"))

    if not events:
        logger.warning(f"No events parsed from session file {session_file}")
        return None

    # Determine timestamps from events
    timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
    started_at = min(timestamps) if timestamps else datetime.now(timezone.utc).isoformat()
    ended_at = max(timestamps) if timestamps else None

    # Build ingest payload
    # Note: Don't send 'id' - let API generate UUID. Store provider_session_id in device_id for tracking.
    device_id = f"zerg-commis-{platform.node()}:{provider_session_id}"
    payload = {
        "provider": "claude",
        "provider_session_id": provider_session_id,  # Claude Code session UUID from filename
        "project": workspace_path.name,  # Use directory name as project
        "device_id": device_id,
        "cwd": str(workspace_path.absolute()),
        "started_at": started_at,
        "ended_at": ended_at,
        "events": events,
    }

    # Ship to Zerg ingest endpoint
    url = f"{ZERG_API_URL}/api/agents/ingest"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()

            result = response.json()
            session_id = result.get("session_id")
            logger.info(f"Shipped session {provider_session_id} to Zerg as {session_id}")
            return session_id

    except Exception as e:
        logger.warning(f"Failed to ship session {provider_session_id}: {e}")
        return None


# Backwards compatibility alias
async def ship_session_to_life_hub(
    workspace_path: Path,
    commis_id: str,
    claude_config_dir: Path | None = None,
) -> str | None:
    """Alias for ship_session_to_zerg for backwards compatibility."""
    return await ship_session_to_zerg(workspace_path, commis_id, claude_config_dir)


__all__ = [
    "encode_cwd_for_claude",
    "fetch_session_from_zerg",
    "fetch_session_from_life_hub",  # Backwards compatibility
    "prepare_session_for_resume",
    "ship_session_to_zerg",
    "ship_session_to_life_hub",  # Backwards compatibility
    "SessionLockManager",
    "SessionLock",
    "WorkspaceResolver",
    "ResolvedWorkspace",
    "session_lock_manager",
]


# ---------------------------------------------------------------------------
# Session Lock Manager
# ---------------------------------------------------------------------------
# In-memory async locks to prevent concurrent resumes of the same session.
# Returns 409 if session is locked, with option to fork.


@dataclass
class SessionLock:
    """Information about a held session lock."""

    session_id: str
    holder: str  # Who holds the lock (e.g., request ID or "web-chat")
    acquired_at: float  # time.time()
    ttl_seconds: int = 300  # 5 minute default TTL

    @property
    def is_expired(self) -> bool:
        """Check if this lock has expired."""
        return time.time() > (self.acquired_at + self.ttl_seconds)

    @property
    def time_remaining(self) -> float:
        """Seconds remaining on this lock."""
        remaining = (self.acquired_at + self.ttl_seconds) - time.time()
        return max(0, remaining)


class SessionLockManager:
    """Manages per-session async locks to prevent concurrent resumes.

    Features:
    - Non-blocking acquisition (returns immediately if locked)
    - TTL-based expiration for crash recovery
    - Lock holder tracking for debugging
    """

    def __init__(self) -> None:
        self._locks: dict[str, SessionLock] = {}
        self._mutex = asyncio.Lock()

    async def acquire(
        self,
        session_id: str,
        holder: str = "web-chat",
        ttl_seconds: int = 300,
    ) -> SessionLock | None:
        """Try to acquire lock for a session.

        Args:
            session_id: Session UUID
            holder: Identifier for who's holding the lock
            ttl_seconds: Lock TTL (default 5 minutes)

        Returns:
            SessionLock if acquired, None if already locked
        """
        async with self._mutex:
            # Opportunistically cleanup expired locks to prevent memory leaks
            self._cleanup_expired_unlocked()

            # Check for existing lock
            existing = self._locks.get(session_id)
            if existing and not existing.is_expired:
                return None

            # Remove expired lock or acquire new
            lock = SessionLock(
                session_id=session_id,
                holder=holder,
                acquired_at=time.time(),
                ttl_seconds=ttl_seconds,
            )
            self._locks[session_id] = lock
            logger.debug(f"Acquired session lock: {session_id} by {holder}")
            return lock

    async def release(self, session_id: str, holder: str | None = None) -> bool:
        """Release a session lock.

        Args:
            session_id: Session UUID
            holder: If provided, only release if holder matches

        Returns:
            True if released, False if not found or holder mismatch
        """
        async with self._mutex:
            existing = self._locks.get(session_id)
            if not existing:
                return False

            if holder and existing.holder != holder:
                logger.warning(f"Lock release rejected: {session_id} held by {existing.holder}, not {holder}")
                return False

            del self._locks[session_id]
            logger.debug(f"Released session lock: {session_id}")
            return True

    async def get_lock_info(self, session_id: str) -> SessionLock | None:
        """Get information about a lock if it exists and is not expired."""
        async with self._mutex:
            # Opportunistically cleanup expired locks to prevent memory leaks
            self._cleanup_expired_unlocked()

            existing = self._locks.get(session_id)
            if existing and not existing.is_expired:
                return existing
            return None

    async def is_locked(self, session_id: str) -> bool:
        """Check if a session is currently locked."""
        lock = await self.get_lock_info(session_id)
        return lock is not None

    def _cleanup_expired_unlocked(self) -> int:
        """Remove expired locks without acquiring mutex. Must be called with mutex held.

        Returns count of cleaned up locks.
        """
        expired = [sid for sid, lock in self._locks.items() if lock.is_expired]
        for sid in expired:
            del self._locks[sid]
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired session locks")
        return len(expired)

    async def cleanup_expired(self) -> int:
        """Remove expired locks. Returns count of cleaned up locks."""
        async with self._mutex:
            return self._cleanup_expired_unlocked()


# Global singleton
session_lock_manager = SessionLockManager()


# ---------------------------------------------------------------------------
# Workspace Resolution
# ---------------------------------------------------------------------------
# Clone git repo to temp dir if workspace not available locally.


@dataclass
class ResolvedWorkspace:
    """Result of workspace resolution."""

    path: Path
    is_temp: bool = False  # True if this is a temp clone
    git_repo: str | None = None
    git_branch: str | None = None
    error: str | None = None

    def cleanup(self) -> None:
        """Remove temp workspace if created."""
        if self.is_temp and self.path.exists():
            try:
                shutil.rmtree(self.path)
                logger.debug(f"Cleaned up temp workspace: {self.path}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp workspace {self.path}: {e}")


@dataclass
class WorkspaceResolver:
    """Resolves workspace paths for session resume.

    If the original workspace path exists locally, uses it directly.
    Otherwise, clones the git repo to a temp directory.
    """

    temp_base: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "zerg-session-workspaces")

    def __post_init__(self) -> None:
        self.temp_base.mkdir(parents=True, exist_ok=True)

    async def resolve(
        self,
        original_cwd: str | None,
        git_repo: str | None = None,
        git_branch: str | None = None,
        session_id: str | None = None,
    ) -> ResolvedWorkspace:
        """Resolve a workspace for session resume.

        Priority:
        1. If original_cwd exists locally, use it
        2. If git_repo provided, clone to temp
        3. Return error if neither works

        Args:
            original_cwd: Original working directory from session
            git_repo: Git repository URL (e.g., from session metadata)
            git_branch: Git branch to checkout
            session_id: Session ID for temp dir naming

        Returns:
            ResolvedWorkspace with path or error
        """
        # Try original path first
        if original_cwd:
            original_path = Path(original_cwd)
            if original_path.exists() and original_path.is_dir():
                logger.debug(f"Using original workspace: {original_path}")
                return ResolvedWorkspace(
                    path=original_path,
                    is_temp=False,
                    git_repo=git_repo,
                    git_branch=git_branch,
                )

        # Try cloning git repo
        if git_repo:
            return await self._clone_repo(git_repo, git_branch, session_id)

        # No workspace available
        return ResolvedWorkspace(
            path=Path("."),
            error="No workspace available: original path not found and no git repo provided",
        )

    async def _clone_repo(
        self,
        git_repo: str,
        git_branch: str | None,
        session_id: str | None,
    ) -> ResolvedWorkspace:
        """Clone a git repo to a temp directory."""
        # Create unique temp dir
        suffix = session_id[:12] if session_id else str(int(time.time()))
        temp_dir = self.temp_base / f"session-{suffix}"

        try:
            # Remove existing temp dir if present (inside try to handle permission errors)
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            # Build clone command
            cmd = ["git", "clone", "--depth=1"]
            if git_branch:
                cmd.extend(["-b", git_branch])
            cmd.extend([git_repo, str(temp_dir)])

            logger.info(f"Cloning {git_repo} to {temp_dir}")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(f"Git clone failed: {error_msg}")
                return ResolvedWorkspace(
                    path=temp_dir,
                    is_temp=True,
                    error=f"Git clone failed: {error_msg[:200]}",
                )

            return ResolvedWorkspace(
                path=temp_dir,
                is_temp=True,
                git_repo=git_repo,
                git_branch=git_branch,
            )

        except Exception as e:
            logger.exception(f"Error cloning repo {git_repo}")
            return ResolvedWorkspace(
                path=temp_dir,
                is_temp=True,
                error=f"Clone error: {str(e)[:200]}",
            )

    def cleanup_all(self) -> int:
        """Clean up all temp workspaces. Returns count removed."""
        if not self.temp_base.exists():
            return 0

        count = 0
        for item in self.temp_base.iterdir():
            if item.is_dir() and item.name.startswith("session-"):
                try:
                    shutil.rmtree(item)
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to cleanup {item}: {e}")

        logger.info(f"Cleaned up {count} temp workspaces")
        return count


# Global workspace resolver
workspace_resolver = WorkspaceResolver()
