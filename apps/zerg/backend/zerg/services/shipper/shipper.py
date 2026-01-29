"""Core shipper logic for syncing Claude Code sessions to Zerg.

The SessionShipper:
1. Scans ~/.claude/projects/ for JSONL session files
2. Parses new events (incremental via byte offset tracking)
3. Ships batches to Zerg's /api/agents/ingest endpoint
4. Updates state to enable future incremental sync
5. Spools payloads locally when API unreachable (offline resilience)
"""

from __future__ import annotations

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

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.spool import OfflineSpool
from zerg.services.shipper.state import ShipperState

logger = logging.getLogger(__name__)


@dataclass
class ShipperConfig:
    """Configuration for the session shipper."""

    zerg_api_url: str = "http://localhost:47300"
    claude_config_dir: Path | None = None  # Defaults to ~/.claude
    scan_interval_seconds: int = 30
    batch_size: int = 100
    timeout_seconds: float = 30.0
    api_token: str | None = None  # Token for authenticated API access

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
        self.state = state or ShipperState()
        self.spool = spool or OfflineSpool()

    def _find_session_files(self) -> list[Path]:
        """Find all JSONL session files in projects directory."""
        projects_dir = self.config.projects_dir

        if not projects_dir.exists():
            logger.debug(f"Projects directory does not exist: {projects_dir}")
            return []

        # Find all .jsonl files recursively
        files = list(projects_dir.glob("**/*.jsonl"))

        # Sort by modification time (newest first)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        return files

    def _has_new_content(self, path: Path) -> bool:
        """Check if a session file has new content since last ship."""
        try:
            file_size = path.stat().st_size
            last_offset = self.state.get_offset(str(path))
            return file_size > last_offset
        except (OSError, IOError):
            return False

    async def scan_and_ship(self) -> ShipResult:
        """One-shot scan of all projects, ship new events.

        Returns:
            ShipResult with counts and any errors
        """
        result = ShipResult()

        # Find all session files
        session_files = self._find_session_files()
        result.sessions_scanned = len(session_files)

        logger.info(f"Found {len(session_files)} session files")

        # Filter to files with new content
        files_to_ship = [f for f in session_files if self._has_new_content(f)]
        logger.info(f"{len(files_to_ship)} files have new content")

        # Ship each file
        for path in files_to_ship:
            try:
                ship_result = await self.ship_session(path)
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

    async def ship_session(self, session_file: Path) -> dict:
        """Ship events from a session file.

        Args:
            session_file: Path to the JSONL session file

        Returns:
            Dict with events_inserted, events_skipped, events_spooled, new_offset
        """
        file_path_str = str(session_file)
        last_offset = self.state.get_offset(file_path_str)

        # Parse new events from file
        events = list(parse_session_file(session_file, offset=last_offset))

        if not events:
            # No new events, but update offset to current file size
            new_offset = session_file.stat().st_size
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
        )

        # Try to ship to Zerg
        try:
            api_result = await self._post_ingest(payload)

            # Update state with new offset (use file size to ensure we don't reparse)
            new_offset = session_file.stat().st_size

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

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            # Connection issues - spool for later retry
            logger.warning(f"API unreachable, spooling {len(events)} events: {e}")
            self.spool.enqueue(payload)

            # Still update offset so we don't re-parse these events
            new_offset = session_file.stat().st_size
            self.state.set_offset(
                file_path_str,
                new_offset,
                session_id,
                metadata.session_id,
            )

            return {
                "events_inserted": 0,
                "events_skipped": 0,
                "events_spooled": len(events),
                "new_offset": new_offset,
            }

    def _build_ingest_payload(
        self,
        session_id: str,
        events: list[ParsedEvent],
        metadata: Any,
        source_path: str,
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
            "provider": "claude",
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
        """Post payload to Zerg ingest endpoint."""
        url = f"{self.config.zerg_api_url}/api/agents/ingest"

        headers = {"Content-Type": "application/json"}
        if self.config.api_token:
            headers["X-Agents-Token"] = self.config.api_token

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    async def replay_spool(self, batch_size: int = 100, max_retries: int = 5) -> dict:
        """Replay spooled payloads that failed to ship.

        Attempts to ship all pending payloads from the spool.
        Items that fail max_retries times are permanently marked as failed.

        Args:
            batch_size: Number of payloads to process per batch
            max_retries: Mark items as permanently failed after this many attempts

        Returns:
            Dict with replayed, failed, remaining counts
        """
        replayed = 0
        failed = 0
        processed_ids = set()  # Track what we've processed this run

        while True:
            batch = self.spool.dequeue_batch(limit=batch_size)
            if not batch:
                break

            # Filter out items we've already processed this run
            batch = [item for item in batch if item.id not in processed_ids]
            if not batch:
                break

            for item in batch:
                processed_ids.add(item.id)

                try:
                    await self._post_ingest(item.payload)
                    self.spool.mark_shipped(item.id)
                    replayed += 1
                    logger.debug(f"Replayed spooled payload {item.id}")

                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    # Still can't connect - stop trying this batch
                    logger.warning(f"Spool replay failed, API still unreachable: {e}")
                    return {
                        "replayed": replayed,
                        "failed": failed,
                        "remaining": self.spool.pending_count(),
                    }

                except httpx.HTTPStatusError as e:
                    # API error - mark as failed (may transition to permanent failure)
                    permanently_failed = self.spool.mark_failed(item.id, str(e), max_retries=max_retries)
                    failed += 1
                    if permanently_failed:
                        logger.warning(f"Spooled payload {item.id} permanently failed")
                    else:
                        logger.debug(f"Spooled payload {item.id} failed, will retry")

                except Exception as e:
                    # Unexpected error - mark as failed
                    permanently_failed = self.spool.mark_failed(item.id, str(e), max_retries=max_retries)
                    failed += 1
                    logger.error(f"Unexpected error replaying {item.id}: {e}")

        # Clean up old entries after successful replay
        self.spool.cleanup_old()

        return {
            "replayed": replayed,
            "failed": failed,
            "remaining": self.spool.pending_count(),
        }
