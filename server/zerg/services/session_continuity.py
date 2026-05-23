"""Session continuity helpers for cross-environment CLI session resumption.

This module now holds:
- provider-specific resume prep for Claude
- shared session fetch/shipping helpers
- workspace resolution and session locking

Sessions are stored in Longhouse's local database and can be fetched/shipped via
the `/api/agents` endpoints.
"""

from __future__ import annotations

import asyncio
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
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.shipper.token import load_machine_name
from zerg.session_execution_home import SessionExecutionHome

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Longhouse API configuration (local by default)
# Standard port is 8080 for `longhouse serve`
LONGHOUSE_API_URL = os.getenv("LONGHOUSE_API_URL", "http://localhost:8080")

# Valid session ID pattern (alphanumeric, dashes, underscores only)
# Prevents path traversal attacks via malicious session IDs
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ShipSessionResult:
    """Structured result from shipping a continued session back into Longhouse."""

    session_id: str
    events_inserted: int
    events_skipped: int
    session_created: bool


def get_managed_workspace_base() -> Path:
    """Return the base path for Longhouse-managed workspaces."""
    return Path(os.getenv("LONGHOUSE_WORKSPACE_PATH", str(Path.home() / ".longhouse" / "workspaces")))


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


def get_machine_name_label() -> str:
    """Return the configured Longhouse machine label, falling back to hostname."""
    machine_name = load_machine_name(resolve_longhouse_home_from_provider_home(get_claude_config_dir()))
    if machine_name:
        return machine_name
    return platform.node() or "unknown"


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


def _export_session_from_db(session_id: str, db: Session) -> tuple[bytes, str, str]:
    """Export a session directly from the local DB for in-process resume prep."""
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise ValueError(f"Invalid session id: {session_id}") from exc

    from zerg.services.agents_store import AgentsStore

    result = AgentsStore(db).export_session_jsonl(session_uuid, branch_mode="head")
    if not result:
        raise ValueError(f"Session {session_id} not found")

    jsonl_bytes, session = result
    # Session-identity-kernel cleanup: ``provider_session_id`` is no longer a
    # column. Prefer the per-thread alias when one exists; the synthesized
    # str(self.id) fallback keeps unmanaged paths working.
    from zerg.models.agents import SessionThread
    from zerg.models.agents import SessionThreadAlias

    alias_value = (
        db.query(SessionThreadAlias.alias_value)
        .join(SessionThread, SessionThreadAlias.thread_id == SessionThread.id)
        .filter(SessionThread.session_id == session.id)
        .filter(SessionThreadAlias.alias_kind == "provider_session_id")
        .order_by(SessionThreadAlias.id.desc())
        .limit(1)
        .scalar()
    )
    provider_session_id = alias_value or str(session.id)
    if provider_session_id:
        validate_session_id(provider_session_id)
    return jsonl_bytes, session.cwd or "", provider_session_id


async def fetch_session_from_zerg(session_id: str, db: Session | None = None) -> tuple[bytes, str, str]:
    """Fetch a session for resumption.

    Uses the local DB when available to avoid brittle self-HTTP from hosted
    instance containers. Falls back to the export API for external contexts.

    Args:
        session_id: Session UUID
        db: Optional local DB session for direct export

    Returns:
        Tuple of (jsonl_bytes, cwd, provider_session_id)

    Raises:
        ValueError: If session not found or API error
        httpx.HTTPError: On network errors
    """
    if db is not None:
        return _export_session_from_db(session_id, db)

    url = f"{LONGHOUSE_API_URL}/api/agents/sessions/{session_id}/export"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url)

        if response.status_code == 404:
            raise ValueError(f"Session {session_id} not found")

        response.raise_for_status()

        cwd = response.headers.get("X-Session-CWD", "")
        provider_session_id = response.headers.get("X-Provider-Session-ID", "")

        if provider_session_id:
            validate_session_id(provider_session_id)

        return response.content, cwd, provider_session_id


async def prepare_claude_session_for_resume(
    session_id: str,
    workspace_path: Path,
    claude_config_dir: Path | None = None,
    db: Session | None = None,
) -> str:
    """Fetch session from Zerg and prepare it for Claude Code `--resume`.

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
    jsonl_bytes, _original_cwd, provider_session_id = await fetch_session_from_zerg(session_id, db=db)

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


def get_codex_config_dir() -> Path:
    """Get the Codex config directory, respecting CODEX_HOME env var."""
    config_dir = os.getenv("CODEX_HOME")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".codex"


async def ship_session_to_zerg(
    workspace_path: Path,
    commis_id: str,
    claude_config_dir: Path | None = None,
    *,
    db: Session | None = None,
    session_id: str | None = None,
    thread_root_session_id: str | None = None,
    continued_from_session_id: str | None = None,
    continuation_kind: str | None = None,
    origin_label: str | None = None,
    branched_from_event_id: int | None = None,
    provider: str = "claude",
    explicit_session_file: Path | None = None,
) -> ShipSessionResult | None:
    """Ship a CLI session from workspace to Zerg.

    For Claude: finds the most recent session file in the workspace's Claude config.
    For Codex: uses explicit_session_file or searches ~/.codex/sessions/.
    Ships to Zerg for future resumption.

    Args:
        workspace_path: The workspace directory where the CLI ran
        commis_id: Commis ID for logging/tracking
        claude_config_dir: Override for Claude config dir (default: from CLAUDE_CONFIG_DIR or ~/.claude)
        provider: Session provider ("claude" or "codex")
        explicit_session_file: If provided, ship this file directly (skips file search)

    Returns:
        The session ID if shipped successfully, None otherwise
    """
    session_file: Path | None = explicit_session_file

    if session_file is None:
        if provider == "codex":
            session_file = _find_latest_codex_session_file()
        else:
            # Claude: search in projects directory
            config_dir = claude_config_dir or get_claude_config_dir()
            encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
            session_dir = config_dir / "projects" / encoded_cwd

            if not session_dir.exists():
                logger.debug(f"No Claude sessions found for workspace {workspace_path}")
                return None

            session_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not session_files:
                logger.debug(f"No session files found in {session_dir}")
                return None
            session_file = session_files[0]

    if session_file is None or not session_file.exists():
        logger.debug(f"No session file found for provider={provider}")
        return None

    provider_session_id = session_file.stem
    # For Codex rollout files, extract session ID from "rollout-{timestamp}-{uuid}" pattern
    if provider == "codex" and provider_session_id.startswith("rollout-"):
        parts = provider_session_id.split("-", 3)  # rollout, date, time, uuid...
        if len(parts) >= 4:
            # The UUID is everything after "rollout-YYYY-MM-DDTHH-MM-SS-"
            # Filename: rollout-2026-03-26T10-24-39-019d2a51-8721-7653-ad24-c3a3dad5d04f
            # Split on the timestamp pattern to extract UUID
            import re as _re

            match = _re.search(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)", provider_session_id)
            if match:
                provider_session_id = match.group(1)

    logger.info(f"Shipping session {provider_session_id} for commis {commis_id}")

    # Build lossless source-line archive first so schema drift still ships.
    source_path = str(session_file)
    source_lines = []
    with session_file.open("rb") as fh:
        byte_offset = 0
        for raw in fh:
            source_lines.append(
                {
                    "source_path": source_path,
                    "source_offset": byte_offset,
                    "raw_json": raw.rstrip(b"\r\n").decode("utf-8", errors="replace"),
                }
            )
            byte_offset += len(raw)

    if not source_lines:
        logger.warning(f"Session file {session_file} is empty, skipping ship")
        return None

    # Parse known Claude schema for structured events, but do not depend on it
    # for archival fidelity.
    from zerg.services.shipper.parser import extract_session_metadata
    from zerg.services.shipper.parser import parse_session_file

    metadata = extract_session_metadata(session_file)
    try:
        parsed_events = list(parse_session_file(session_file))
    except Exception as exc:
        logger.warning(
            "Failed to parse session file %s; shipping source lines only: %s",
            session_file,
            exc,
        )
        parsed_events = []
    events = [e.to_event_ingest(source_path) for e in parsed_events]
    if not events:
        logger.info(
            "No parseable events in %s; shipping %d source lines only",
            session_file,
            len(source_lines),
        )

    # Determine session timestamps.
    if events:
        timestamps = [e.get("timestamp") for e in events if e.get("timestamp")]
        started_at = metadata.started_at.isoformat() if metadata.started_at else min(timestamps)
        ended_at = metadata.ended_at.isoformat() if metadata.ended_at else (max(timestamps) if timestamps else None)
    else:
        started_at = (metadata.started_at or datetime.now(timezone.utc)).isoformat()
        ended_at = metadata.ended_at.isoformat() if metadata.ended_at else None

    # Build ingest payload.
    # Explicit lineage/session ids let cloud branching update a known child row
    # instead of silently inventing a sibling session on every resume.
    device_id = f"zerg-commis-{platform.node()}:{provider_session_id}"
    payload = {
        "provider": provider,
        "environment": get_machine_name_label(),
        "provider_session_id": provider_session_id,
        "project": metadata.project or workspace_path.name,
        "device_id": device_id,
        "cwd": metadata.cwd or str(workspace_path.absolute()),
        "git_branch": metadata.git_branch,
        "started_at": started_at,
        "ended_at": ended_at,
        "events": events,
        "source_lines": source_lines,
        "continuation_kind": continuation_kind or "cloud",
        "origin_label": origin_label or "Cloud",
    }
    if (continuation_kind or "").strip().lower() == "cloud":
        payload["execution_home"] = SessionExecutionHome.CLOUD_TAKEOVER.value
    elif (continuation_kind or "").strip().lower() == "runner":
        payload["execution_home"] = SessionExecutionHome.MANAGED_HOSTED.value
    if session_id:
        payload["id"] = session_id
    if thread_root_session_id:
        payload["thread_root_session_id"] = thread_root_session_id
    if continued_from_session_id:
        payload["continued_from_session_id"] = continued_from_session_id
    if branched_from_event_id is not None:
        payload["branched_from_event_id"] = branched_from_event_id

    try:
        if db is not None:
            from zerg.services.agents_store import AgentsStore
            from zerg.services.agents_store import SessionIngest

            result = AgentsStore(db).ingest_session(SessionIngest.model_validate(payload))
            logger.info(
                "Locally ingested shipped session %s as %s (events inserted=%s skipped=%s)",
                provider_session_id,
                result.session_id,
                result.events_inserted,
                result.events_skipped,
            )
            return ShipSessionResult(
                session_id=str(result.session_id),
                events_inserted=result.events_inserted,
                events_skipped=result.events_skipped,
                session_created=result.session_created,
            )

        # Ship to Zerg ingest endpoint
        url = f"{LONGHOUSE_API_URL}/api/agents/ingest"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            ingested_session_id = result.get("session_id")
            if not ingested_session_id:
                logger.warning("Ship response missing session_id for provider session %s", provider_session_id)
                return None
            logger.info(f"Shipped session {provider_session_id} to Zerg as {ingested_session_id}")
            return ShipSessionResult(
                session_id=str(ingested_session_id),
                events_inserted=int(result.get("events_inserted", 0) or 0),
                events_skipped=int(result.get("events_skipped", 0) or 0),
                session_created=bool(result.get("session_created", False)),
            )

    except Exception as e:
        logger.warning(f"Failed to ship session {provider_session_id}: {e}")
        return None


def _find_latest_codex_session_file() -> Path | None:
    """Find the most recently modified Codex session file."""
    codex_home = get_codex_config_dir()
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return None

    # Search for most recent rollout-*.jsonl across all date subdirectories
    session_files = sorted(sessions_dir.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return session_files[0] if session_files else None


__all__ = [
    "encode_cwd_for_claude",
    "fetch_session_from_zerg",
    "prepare_claude_session_for_resume",
    "ship_session_to_zerg",
    "ShipSessionResult",
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
    Otherwise, clones the git repo to a temp directory or falls back to a
    managed scratch workspace for non-repo sessions.
    """

    temp_base: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "zerg-session-workspaces")
    scratch_base: Path = field(default_factory=lambda: get_managed_workspace_base() / "continuations")

    def __post_init__(self) -> None:
        self.temp_base.mkdir(parents=True, exist_ok=True)
        self.scratch_base.mkdir(parents=True, exist_ok=True)

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
        3. If session_id provided, create/reuse a managed scratch workspace
        4. Return error if neither works

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

        # Fall back to a managed scratch workspace for non-repo sessions.
        if session_id:
            return self._ensure_managed_scratch_workspace(session_id, original_cwd=original_cwd)

        # No workspace available
        return ResolvedWorkspace(
            path=Path("."),
            error="No workspace available: original path not found and no git repo provided",
        )

    def _ensure_managed_scratch_workspace(
        self,
        session_id: str,
        *,
        original_cwd: str | None,
    ) -> ResolvedWorkspace:
        """Create or reuse a stable managed scratch workspace for a session."""
        validate_session_id(session_id)

        workspace_dir = self.scratch_base / f"session-{session_id}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Using managed scratch workspace %s for session %s (missing original cwd=%s)",
            workspace_dir,
            session_id,
            original_cwd or "<none>",
        )
        return ResolvedWorkspace(path=workspace_dir, is_temp=False)

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
