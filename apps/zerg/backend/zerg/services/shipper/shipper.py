"""Core shipper logic for syncing Claude Code sessions to Zerg.

The SessionShipper:
1. Scans ~/.claude/projects/ for JSONL session files
2. Parses new events (incremental via byte offset tracking)
3. Ships batches to Zerg's /api/agents/ingest endpoint
4. Updates state to enable future incremental sync
5. Spools byte-range pointers locally when API unreachable (offline resilience)
6. Gzip compresses payloads for efficient network transfer
7. Handles HTTP 429 rate limiting with exponential backoff
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

# Import providers to trigger auto-registration in global registry
import zerg.services.shipper.providers.claude  # noqa: F401
import zerg.services.shipper.providers.codex  # noqa: F401
import zerg.services.shipper.providers.gemini  # noqa: F401
from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.parser import parse_session_file_with_offset
from zerg.services.shipper.providers import SessionProvider
from zerg.services.shipper.providers import registry as provider_registry
from zerg.services.shipper.providers.claude import ClaudeProvider
from zerg.services.shipper.spool import OfflineSpool
from zerg.services.shipper.state import ShipperState

logger = logging.getLogger(__name__)


class RateLimitExhaustedError(Exception):
    """Raised when max 429 retries are exhausted."""

    pass


@dataclass
class ShipperConfig:
    """Configuration for the session shipper."""

    api_url: str = "http://localhost:8080"  # Standard port for `longhouse serve`
    claude_config_dir: Path | None = None  # Defaults to ~/.claude
    scan_interval_seconds: int = 30
    batch_size: int = 100
    timeout_seconds: float = 30.0
    api_token: str | None = None  # Token for authenticated API access
    enable_gzip: bool = True  # Gzip compress payloads (reduces bandwidth)
    max_retries_429: int = 3  # Max retries on HTTP 429
    base_backoff_seconds: float = 1.0  # Base backoff for 429 retries
    max_batch_bytes: int = 5 * 1024 * 1024  # 5MB max source data per batch

    def __post_init__(self):
        if self.claude_config_dir is None:
            config_dir = os.getenv("CLAUDE_CONFIG_DIR")
            if config_dir:
                self.claude_config_dir = Path(config_dir)
            else:
                self.claude_config_dir = Path.home() / ".claude"
        # Load token from environment if not explicitly provided
        if self.api_token is None:
            self.api_token = os.getenv("AGENTS_API_TOKEN")

    @property
    def projects_dir(self) -> Path:
        """Get the projects directory."""
        return self.claude_config_dir / "projects"


@dataclass
class ShipResult:
    """Result of a ship operation."""

    sessions_scanned: int = 0
    sessions_shipped: int = 0
    events_shipped: int = 0
    events_skipped: int = 0  # Duplicates
    events_spooled: int = 0  # Queued for retry
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class SessionShipper:
    """Ships Claude Code sessions to Zerg.

    Usage:
        shipper = SessionShipper()
        result = await shipper.scan_and_ship()
    """

    def __init__(
        self,
        config: ShipperConfig | None = None,
        state: ShipperState | None = None,
        spool: OfflineSpool | None = None,
    ):
        """Initialize the shipper.

        Args:
            config: Shipper configuration
            state: State tracker (for incremental sync)
            spool: Offline spool for resilience (auto-created if None)
        """
        self.config = config or ShipperConfig()
        self.state = state or ShipperState(claude_config_dir=self.config.claude_config_dir)
        self.spool = spool or OfflineSpool(claude_config_dir=self.config.claude_config_dir)

    def _get_providers(self) -> list[SessionProvider]:
        """Build the list of providers to scan.

        Uses a ClaudeProvider scoped to self.config.claude_config_dir
        (respecting test overrides), plus any non-claude providers from
        the global registry.
        """
        providers: list[SessionProvider] = []

        # Claude provider scoped to this shipper's config
        providers.append(ClaudeProvider(config_dir=self.config.claude_config_dir))

        # Add non-claude providers from global registry
        for p in provider_registry.all():
            if p.name != "claude":
                providers.append(p)

        return providers

    def _find_session_files(self) -> list[Path]:
        """Find all JSONL session files in projects directory.

        Legacy method kept for backward compatibility (used by watcher).
        For multi-provider discovery, see _find_all_session_files().
        """
        projects_dir = self.config.projects_dir

        if not projects_dir.exists():
            logger.debug(f"Projects directory does not exist: {projects_dir}")
            return []

        # Find all .jsonl files recursively
        files = list(projects_dir.glob("**/*.jsonl"))

        # Sort by modification time (newest first)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        return files

    def _find_all_session_files(self) -> list[tuple[Path, str]]:
        """Find session files across all providers.

        Uses _get_providers() which respects config overrides for Claude
        and includes additional providers from the global registry.

        Returns list of (path, provider_name) tuples, newest first.
        """
        results: list[tuple[Path, float, str]] = []
        for provider in self._get_providers():
            for path in provider.discover_files():
                try:
                    mtime = path.stat().st_mtime
                    results.append((path, mtime, provider.name))
                except OSError:
                    continue
        # Sort by mtime descending
        results.sort(key=lambda x: x[1], reverse=True)
        return [(path, name) for path, _, name in results]

    @staticmethod
    def _new_offset_for(session_file: Path) -> int:
        """Compute the state offset to store after shipping a file.

        For JSON files (non-appendable), stores mtime as int so that
        _has_new_content can detect rewrites.  For JSONL files, stores
        the byte size for incremental reads.
        """
        stat = session_file.stat()
        if session_file.suffix == ".json":
            return int(stat.st_mtime)
        return stat.st_size

    def _has_new_content(self, path: Path) -> bool:
        """Check if a session file has new content since last ship."""
        # JSON-based providers (non-appendable) — use mtime comparison
        if path.suffix == ".json":
            try:
                file_mtime = path.stat().st_mtime
                last_mtime = self.state.get_offset(str(path))
                # offset field stores mtime (as int) for JSON files
                return file_mtime > last_mtime or last_mtime == 0
            except (OSError, IOError):
                return False
        # JSONL-based providers — use byte offset comparison
        try:
            file_size = path.stat().st_size
            file_path_str = str(path)
            stored_offset = max(
                self.state.get_offset(file_path_str),
                self.state.get_queued_offset(file_path_str),
            )
            # Detect file truncation/rotation: file shrank below stored offset
            if file_size < stored_offset:
                logger.warning(f"File truncated or rotated: {path} (size={file_size}, stored_offset={stored_offset}). Resetting offsets.")
                self.state.reset_offsets(file_path_str)
                return file_size > 0
            return file_size > stored_offset
        except (OSError, IOError):
            return False

    def startup_recovery(self) -> int:
        """Re-enqueue any gaps between queued_offset and acked_offset.

        Called on startup to recover from incomplete shipments.
        Returns number of recovery entries enqueued.
        """
        unacked = self.state.get_unacked_files()
        count = 0
        for file_path, acked_offset, queued_offset, provider, session_id in unacked:
            self.spool.enqueue(
                provider=provider or "claude",
                file_path=file_path,
                start_offset=acked_offset,
                end_offset=queued_offset,
                session_id=session_id,
            )
            count += 1
            logger.info(f"Recovery: re-enqueued {file_path} bytes [{acked_offset}:{queued_offset}]")
        return count

    async def scan_and_ship(self) -> ShipResult:
        """One-shot scan of all projects, ship new events.

        Iterates all providers to discover session files.

        Returns:
            ShipResult with counts and any errors
        """
        result = ShipResult()

        # Find all session files across all providers
        session_files = self._find_all_session_files()
        result.sessions_scanned = len(session_files)

        logger.info(f"Found {len(session_files)} session files")

        # Filter to files with new content
        files_to_ship = [(f, pname) for f, pname in session_files if self._has_new_content(f)]
        logger.info(f"{len(files_to_ship)} files have new content")

        # Ship each file
        for path, provider_name in files_to_ship:
            try:
                ship_result = await self.ship_session(path, provider_name=provider_name)
                # Collect errors from backpressure (Bug 1)
                if ship_result.get("errors"):
                    result.errors.extend(ship_result["errors"])
                if ship_result["events_inserted"] > 0 or ship_result["events_skipped"] > 0 or ship_result["events_spooled"] > 0:
                    result.sessions_shipped += 1
                    result.events_shipped += ship_result["events_inserted"]
                    result.events_skipped += ship_result["events_skipped"]
                    result.events_spooled += ship_result["events_spooled"]
            except Exception as e:
                error_msg = f"Failed to ship {path.name}: {e}"
                logger.error(error_msg)
                result.errors.append(error_msg)

        return result

    async def ship_session(self, session_file: Path, *, provider_name: str = "claude") -> dict:
        """Ship events from a session file.

        Args:
            session_file: Path to the JSONL session file
            provider_name: Name of the provider that owns this file (default: "claude")

        Returns:
            Dict with events_inserted, events_skipped, events_spooled, new_offset
        """
        file_path_str = str(session_file)
        acked_offset = self.state.get_offset(file_path_str)
        queued_offset = self.state.get_queued_offset(file_path_str)
        # Bug 2 fix: use max of acked and queued to avoid re-reading spooled range
        read_offset = max(acked_offset, queued_offset)

        # Use provider if available, otherwise fall back to direct parser calls
        # Bug 3 fix: use parse_session_file_with_offset to track last good byte offset
        provider = provider_registry.get(provider_name)
        if session_file.suffix == ".json":
            # JSON files are non-appendable — no partial line concern
            if provider:
                events = list(provider.parse_file(session_file, offset=read_offset))
            else:
                events = list(parse_session_file(session_file, offset=read_offset))
            new_offset = self._new_offset_for(session_file)
        else:
            # JSONL files — track last good offset to avoid losing partial lines
            # Always use parse_session_file_with_offset for offset tracking,
            # even when a provider is available (providers delegate to the same parser)
            events, last_good_offset = parse_session_file_with_offset(session_file, offset=read_offset)
            new_offset = last_good_offset

        if not events:
            # No new events, but update offset
            existing = self.state.get_session(file_path_str)
            if existing:
                self.state.set_offset(
                    file_path_str,
                    new_offset,
                    existing.session_id,
                    existing.provider_session_id,
                )
            return {
                "events_inserted": 0,
                "events_skipped": 0,
                "events_spooled": 0,
                "new_offset": new_offset,
            }

        # Extract session metadata
        if provider:
            metadata = provider.extract_metadata(session_file)
        else:
            metadata = extract_session_metadata(session_file)

        # Get or create session ID
        existing = self.state.get_session(file_path_str)
        if existing:
            session_id = existing.session_id
        else:
            session_id = str(uuid4())

        # Build ingest payload
        payload = self._build_ingest_payload(
            session_id=session_id,
            events=events,
            metadata=metadata,
            source_path=file_path_str,
            provider_name=provider_name,
        )

        # Try to ship to Zerg
        try:
            api_result = await self._post_ingest(payload)

            # Success: advance both queued and acked offsets
            self.state.set_offset(
                file_path_str,
                new_offset,
                api_result.get("session_id", session_id),
                metadata.session_id,
            )

            return {
                "events_inserted": api_result.get("events_inserted", 0),
                "events_skipped": api_result.get("events_skipped", 0),
                "events_spooled": 0,
                "new_offset": new_offset,
            }

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # Connection/timeout issues - spool pointer for later retry
            logger.warning(f"API unreachable, spooling pointer for {len(events)} events: {e}")
            # Bug 1 fix: check enqueue return value before advancing offset
            enqueued = self.spool.enqueue(
                provider=provider_name,
                file_path=file_path_str,
                start_offset=read_offset,
                end_offset=new_offset,
                session_id=session_id,
            )

            if enqueued:
                # Advance queued_offset but not acked_offset
                self.state.set_queued_offset(
                    file_path_str,
                    new_offset,
                    provider=provider_name,
                    session_id=session_id,
                    provider_session_id=metadata.session_id,
                )

                return {
                    "events_inserted": 0,
                    "events_skipped": 0,
                    "events_spooled": len(events),
                    "new_offset": new_offset,
                }
            else:
                return {
                    "events_inserted": 0,
                    "events_skipped": 0,
                    "events_spooled": 0,
                    "new_offset": read_offset,
                    "errors": ["Spool at capacity, data not enqueued"],
                }

        except RateLimitExhaustedError as e:
            # Rate limit exhausted after max retries - spool for later retry
            logger.warning(f"Rate limit exhausted, spooling pointer for {len(events)} events: {e}")
            enqueued = self.spool.enqueue(
                provider=provider_name,
                file_path=file_path_str,
                start_offset=read_offset,
                end_offset=new_offset,
                session_id=session_id,
            )

            if enqueued:
                self.state.set_queued_offset(
                    file_path_str,
                    new_offset,
                    provider=provider_name,
                    session_id=session_id,
                    provider_session_id=metadata.session_id,
                )

            return {
                "events_inserted": 0,
                "events_skipped": 0,
                "events_spooled": len(events) if enqueued else 0,
                "new_offset": new_offset if enqueued else read_offset,
            }

        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code

            # Auth errors (401/403) - hard fail, don't spool (will never succeed)
            if status_code in (401, 403):
                logger.error(f"Auth error ({status_code}), not spooling: {e}")
                raise

            # Server errors (5xx) - spool for retry
            if status_code >= 500:
                logger.warning(f"Server error ({status_code}), spooling pointer for {len(events)} events: {e}")
                enqueued = self.spool.enqueue(
                    provider=provider_name,
                    file_path=file_path_str,
                    start_offset=read_offset,
                    end_offset=new_offset,
                    session_id=session_id,
                )

                if enqueued:
                    self.state.set_queued_offset(
                        file_path_str,
                        new_offset,
                        provider=provider_name,
                        session_id=session_id,
                        provider_session_id=metadata.session_id,
                    )

                return {
                    "events_inserted": 0,
                    "events_skipped": 0,
                    "events_spooled": len(events) if enqueued else 0,
                    "new_offset": new_offset if enqueued else read_offset,
                }

            # Other 4xx errors - log and skip (bad payload, won't retry)
            logger.warning(f"Client error ({status_code}), skipping {len(events)} events: {e}")
            self.state.set_offset(
                file_path_str,
                new_offset,
                session_id,
                metadata.session_id,
            )

            return {
                "events_inserted": 0,
                "events_skipped": len(events),
                "events_spooled": 0,
                "new_offset": new_offset,
            }

    def _build_ingest_payload(
        self,
        session_id: str,
        events: list[ParsedEvent],
        metadata: Any,
        source_path: str,
        provider_name: str = "claude",
    ) -> dict:
        """Build the ingest API payload."""
        # Convert events to API format
        event_dicts = [e.to_event_ingest(source_path) for e in events]

        # Determine timestamps
        timestamps = [e.timestamp for e in events if e.timestamp]
        started_at = metadata.started_at or (min(timestamps) if timestamps else datetime.now(timezone.utc))
        ended_at = metadata.ended_at or (max(timestamps) if timestamps else None)

        return {
            "id": session_id,
            "provider": provider_name,
            "environment": "production",
            "project": metadata.project,
            "device_id": f"shipper-{os.uname().nodename}",
            "cwd": metadata.cwd,
            "git_repo": None,  # Could extract from .git/config
            "git_branch": metadata.git_branch,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at else None,
            "provider_session_id": metadata.session_id,
            "events": event_dicts,
        }

    async def _post_ingest(self, payload: dict) -> dict:
        """Post payload to Zerg ingest endpoint.

        Features:
        - Gzip compression for efficient network transfer
        - HTTP 429 handling with exponential backoff and Retry-After support
        """
        url = f"{self.config.api_url}/api/agents/ingest"

        headers = {"Content-Type": "application/json"}
        if self.config.api_token:
            headers["X-Agents-Token"] = self.config.api_token

        # Gzip compress the payload if enabled
        if self.config.enable_gzip:
            json_bytes = json.dumps(payload).encode("utf-8")
            content = gzip.compress(json_bytes)
            headers["Content-Encoding"] = "gzip"
        else:
            content = json.dumps(payload).encode("utf-8")

        # Retry loop for 429 handling
        retries = 0
        backoff = self.config.base_backoff_seconds

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            while True:
                response = await client.post(url, content=content, headers=headers)

                # Handle 429 Too Many Requests
                if response.status_code == 429:
                    if retries >= self.config.max_retries_429:
                        logger.warning(f"Rate limited after {retries} retries, spooling for later")
                        raise RateLimitExhaustedError(f"Rate limited after {retries} retries")

                    # Use Retry-After header if present, otherwise exponential backoff
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_seconds = float(retry_after)
                        except ValueError:
                            wait_seconds = backoff
                    else:
                        wait_seconds = backoff

                    logger.info(f"Rate limited (429), waiting {wait_seconds:.1f}s before retry {retries + 1}/{self.config.max_retries_429}")
                    await asyncio.sleep(wait_seconds)
                    retries += 1
                    backoff *= 2  # Exponential backoff
                    continue

                response.raise_for_status()
                return response.json()

    async def replay_spool(self, batch_size: int = 100, max_retries: int = 5) -> dict:
        """Replay spooled pointers that failed to ship.

        Re-reads source files at stored byte ranges, re-parses events,
        builds payload, and ships. Items that fail max_retries times are
        permanently marked as dead.

        Args:
            batch_size: Number of entries to process per batch
            max_retries: Mark items as permanently dead after this many attempts

        Returns:
            Dict with replayed, failed, remaining counts
        """
        replayed = 0
        failed = 0

        batch = self.spool.dequeue_batch(limit=batch_size)
        if not batch:
            return {"replayed": 0, "failed": 0, "remaining": self.spool.pending_count()}

        for entry in batch:
            try:
                # Re-read source file at stored byte range
                source_path = Path(entry.file_path)
                if not source_path.exists():
                    logger.warning(f"Source file missing for spool entry {entry.id}: {entry.file_path}")
                    self.spool.mark_failed(entry.id, "Source file missing", max_retries=1)
                    failed += 1
                    continue

                # Check file is large enough for our byte range
                file_size = source_path.stat().st_size
                if file_size < entry.end_offset:
                    logger.warning(
                        f"Source file truncated for spool entry {entry.id}: {entry.file_path} (size={file_size}, need={entry.end_offset})"
                    )
                    self.spool.mark_failed(entry.id, "Source file truncated", max_retries=1)
                    failed += 1
                    continue

                # Re-parse events from the byte range
                provider = provider_registry.get(entry.provider)
                if provider:
                    events = list(provider.parse_file(source_path, offset=entry.start_offset))
                else:
                    events = list(parse_session_file(source_path, offset=entry.start_offset))

                # Filter to events within our byte range
                events = [e for e in events if e.source_offset < entry.end_offset]

                if not events:
                    # No events in this range — mark as shipped and advance acked_offset
                    # to close the gap (Bug 7: prevents startup_recovery re-enqueuing forever)
                    self.spool.mark_shipped(entry.id)
                    self.state.set_acked_offset(entry.file_path, entry.end_offset)
                    replayed += 1
                    continue

                # Build and ship payload
                if provider:
                    metadata = provider.extract_metadata(source_path)
                else:
                    metadata = extract_session_metadata(source_path)

                session_id = entry.session_id or str(uuid4())
                payload = self._build_ingest_payload(
                    session_id=session_id,
                    events=events,
                    metadata=metadata,
                    source_path=entry.file_path,
                    provider_name=entry.provider,
                )

                await self._post_ingest(payload)
                self.spool.mark_shipped(entry.id)

                # Advance acked_offset
                self.state.set_acked_offset(entry.file_path, entry.end_offset)

                replayed += 1
                logger.debug(f"Replayed spool entry {entry.id}")

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                # Still can't connect - stop trying this batch
                logger.warning(f"Spool replay failed, API still unreachable: {e}")
                return {
                    "replayed": replayed,
                    "failed": failed,
                    "remaining": self.spool.pending_count(),
                }

            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                # Auth errors (401/403) - immediately mark as dead
                if status_code in (401, 403):
                    self.spool.mark_failed(entry.id, f"Auth error ({status_code})", max_retries=1)
                    failed += 1
                    logger.error(f"Spool entry {entry.id} auth error ({status_code}), marked dead")
                    continue

                # Server errors (5xx) - mark as failed, will retry later
                if status_code >= 500:
                    permanently_failed = self.spool.mark_failed(entry.id, str(e), max_retries=max_retries)
                    failed += 1
                    if permanently_failed:
                        logger.warning(f"Spool entry {entry.id} permanently dead after max retries")
                    continue

                # Other 4xx errors - mark as dead (bad payload)
                self.spool.mark_failed(entry.id, f"Client error ({status_code})", max_retries=1)
                failed += 1
                logger.warning(f"Spool entry {entry.id} client error ({status_code}), marked dead")

            except Exception as e:
                # Unexpected error - mark as failed
                self.spool.mark_failed(entry.id, str(e), max_retries=max_retries)
                failed += 1
                logger.error(f"Unexpected error replaying {entry.id}: {e}")

        return {
            "replayed": replayed,
            "failed": failed,
            "remaining": self.spool.pending_count(),
        }
