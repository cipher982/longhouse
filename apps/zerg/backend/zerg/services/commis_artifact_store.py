"""Commis Artifact Store – filesystem persistence for disposable commis fiches.

This service manages the filesystem structure for commis artifacts, enabling
commis to persist all outputs (tool calls, messages, results) to disk for
later retrieval by concierge fiches.

INVARIANTS:
- result.txt is canonical. Never delete or auto-truncate.
- metadata.json contains derived views (summaries, extracted fields).
- Derived data MUST be recomputable from canonical artifacts.
- System decisions (status) never depend on LLM-generated summaries.

Directory structure:
    /data/commis/
    ├── index.json                    # Master index of all commis
    └── {commis_id}/                  # e.g., "2024-12-03T14-32-00_disk-check"
        ├── metadata.json             # Status, timestamps, task, config
        ├── result.txt                # Final natural language result
        ├── thread.jsonl              # Full conversation (messages)
        └── tool_calls/               # Raw tool outputs
            ├── 001_ssh_exec.txt
            ├── 002_http_request.json
            └── ...

The commis_id format is: "{timestamp}_{slug}_{suffix}" e.g., "2024-12-03T14-32-00_disk-check_a1b2c3"
where the slug is derived from the task description (first 30 chars, kebab-case).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.config import get_settings

logger = logging.getLogger(__name__)

try:
    import fcntl  # type: ignore

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows/fallback path
    fcntl = None
    _HAS_FCNTL = False

_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_LOCKS_GUARD = threading.Lock()


class CommisArtifactStore:
    """Manages filesystem storage for commis artifacts."""

    def __init__(self, base_path: str | None = None):
        """Initialize the artifact store.

        Parameters
        ----------
        base_path
            Root directory for commis artifacts. Resolution order:
            1. If base_path is provided, use it (fail if not writable)
            2. If SWARMLET_DATA_PATH env var is set, use it (fail if not writable)
            3. Otherwise, use get_settings().data_dir / "commis"
        """
        env_path = os.getenv("SWARMLET_DATA_PATH")

        if base_path:
            self.base_path = Path(base_path)
        elif env_path:
            self.base_path = Path(env_path)
        else:
            self.base_path = get_settings().data_dir / "commis"

        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"CommisArtifactStore: failed to init {self.base_path}: {e}")
            # Fallback to /tmp only as a last resort in dev/testing
            if not base_path and not env_path and get_settings().testing:
                self.base_path = Path("/tmp/swarmlet/commis")
                self.base_path.mkdir(parents=True, exist_ok=True)
                logger.warning(f"CommisArtifactStore: using emergency fallback {self.base_path}")
            else:
                raise

        self.index_path = self.base_path / "index.json"

        # Initialize index if it doesn't exist
        if not self.index_path.exists():
            self._write_index([])

    def _slugify(self, text: str, max_length: int = 30) -> str:
        """Convert text to a filesystem-safe slug.

        Parameters
        ----------
        text
            Input text to slugify
        max_length
            Maximum length of the slug

        Returns
        -------
        str
            Kebab-case slug suitable for filesystem
        """
        # Convert to lowercase and replace spaces/underscores with hyphens
        slug = text.lower().strip()
        slug = re.sub(r"[\s_]+", "-", slug)
        # Remove non-alphanumeric characters except hyphens
        slug = re.sub(r"[^a-z0-9\-]", "", slug)
        # Remove leading/trailing hyphens and collapse multiple hyphens
        slug = re.sub(r"-+", "-", slug).strip("-")
        # Truncate to max length
        return slug[:max_length]

    def _generate_commis_id(self, task: str) -> str:
        """Generate a unique commis ID from timestamp and task.

        Parameters
        ----------
        task
            Task description

        Returns
        -------
        str
            Commis ID in format: "{timestamp}_{slug}"
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        slug = self._slugify(task)
        suffix = uuid.uuid4().hex[:6]
        return f"{timestamp}_{slug}_{suffix}"

    def _get_commis_dir(self, commis_id: str) -> Path:
        """Get the directory path for a commis.

        Parameters
        ----------
        commis_id
            Unique commis identifier

        Returns
        -------
        Path
            Directory path for the commis
        """
        return self.base_path / commis_id

    def _read_index(self) -> list[dict[str, Any]]:
        """Read the master index file.

        Returns
        -------
        list[dict]
            List of commis metadata entries
        """
        with self._index_lock():
            return self._read_index_unlocked()

    def _read_index_unlocked(self) -> list[dict[str, Any]]:
        """Read the master index file without acquiring a lock."""
        try:
            with open(self.index_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_index(self, index: list[dict[str, Any]]) -> None:
        """Write the master index file.

        Parameters
        ----------
        index
            List of commis metadata entries
        """
        with self._index_lock():
            self._write_index_unlocked(index)

    def _write_index_unlocked(self, index: list[dict[str, Any]]) -> None:
        """Write the master index file without acquiring a lock."""
        with open(self.index_path, "w") as f:
            json.dump(index, f, indent=2)

    def _update_index(self, commis_id: str, metadata: dict[str, Any]) -> None:
        """Update or insert a commis entry in the index.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        metadata
            Commis metadata to store
        """
        with self._index_lock():
            index = self._read_index_unlocked()

            # Find existing entry or append new one
            for i, entry in enumerate(index):
                if entry.get("commis_id") == commis_id:
                    index[i] = metadata
                    break
            else:
                index.append(metadata)

            self._write_index_unlocked(index)

    def _get_process_lock(self) -> threading.Lock:
        """Return a process-local lock for the index path."""
        key = str(self.index_path.resolve())
        with _INDEX_LOCKS_GUARD:
            lock = _INDEX_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _INDEX_LOCKS[key] = lock
            return lock

    @contextmanager
    def _index_lock(self):
        """Acquire an exclusive lock for index read/write operations."""
        if _HAS_FCNTL:
            lock_path = self.index_path.with_suffix(self.index_path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
        else:  # pragma: no cover - fallback on platforms without fcntl
            lock = self._get_process_lock()
            with lock:
                yield

    def create_commis(
        self,
        task: str,
        config: dict[str, Any] | None = None,
        owner_id: int | None = None,
        commis_id: str | None = None,
    ) -> str:
        """Create a new commis directory structure.

        Parameters
        ----------
        task
            Task description for the commis
        config
            Optional configuration dict (e.g., model, tools, timeout)
        owner_id
            Optional ID of the user who owns this commis (for security filtering)
        commis_id
            Optional custom commis_id (auto-generated if not provided)

        Returns
        -------
        str
            Unique commis_id

        Raises
        ------
        ValueError
            If commis_id already exists (collision)
        """
        if commis_id is None:
            commis_id = self._generate_commis_id(task)
        commis_dir = self._get_commis_dir(commis_id)

        # Check for collision (shouldn't happen with timestamp)
        if commis_dir.exists():
            raise ValueError(f"Commis directory already exists: {commis_id}")

        # Create directory structure
        commis_dir.mkdir(parents=True, exist_ok=True)
        tool_calls_dir = commis_dir / "tool_calls"
        tool_calls_dir.mkdir(exist_ok=True)

        # Initialize config
        commis_config = config or {}
        # Store owner_id in config if provided
        if owner_id is not None:
            commis_config["owner_id"] = owner_id

        # Initialize metadata
        metadata = {
            "commis_id": commis_id,
            "task": task,
            "config": commis_config,
            "status": "created",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "error": None,
        }

        # Write metadata file
        metadata_path = commis_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Update index
        self._update_index(commis_id, metadata)

        logger.info(f"Created commis directory: {commis_id}")
        return commis_id

    def save_tool_output(self, commis_id: str, tool_name: str, output: str, sequence: int) -> str:
        """Save tool output to a file.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        tool_name
            Name of the tool that was executed
        output
            Tool output (text or JSON)
        sequence
            Sequence number for ordering tool calls

        Returns
        -------
        str
            Relative path to the saved file (e.g., "tool_calls/001_ssh_exec.txt")
        """
        commis_dir = self._get_commis_dir(commis_id)
        tool_calls_dir = commis_dir / "tool_calls"

        # Generate filename
        filename = f"{sequence:03d}_{tool_name}.txt"
        filepath = tool_calls_dir / filename

        # Write output
        with open(filepath, "w") as f:
            f.write(output)

        logger.debug(f"Saved tool output: {commis_id}/{filename}")
        return f"tool_calls/{filename}"

    def save_message(self, commis_id: str, message: dict[str, Any]) -> None:
        """Append a message to the thread.jsonl file.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        message
            Message dict (role, content, etc.)
        """
        commis_dir = self._get_commis_dir(commis_id)
        thread_path = commis_dir / "thread.jsonl"

        # Append message as JSON line
        with open(thread_path, "a") as f:
            f.write(json.dumps(message) + "\n")

    def save_result(self, commis_id: str, result: str) -> None:
        """Save final result to result.txt.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        result
            Final natural language result from the commis
        """
        commis_dir = self._get_commis_dir(commis_id)
        result_path = commis_dir / "result.txt"

        with open(result_path, "w") as f:
            f.write(result)

        logger.info(f"Saved commis result: {commis_id}")

    def save_artifact(self, commis_id: str, filename: str, content: str) -> Path:
        """Save an arbitrary artifact file.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        filename
            Name for the artifact file (e.g., "diff.patch", "output.log")
        content
            Content to save

        Returns
        -------
        Path
            Path to the saved artifact file
        """
        commis_dir = self._get_commis_dir(commis_id)
        artifact_path = commis_dir / filename

        # Ensure parent directories exist for nested paths
        artifact_path.parent.mkdir(parents=True, exist_ok=True)

        with open(artifact_path, "w") as f:
            f.write(content)

        logger.info(f"Saved artifact {filename} for commis {commis_id}")
        return artifact_path

    def complete_commis(self, commis_id: str, status: str = "success", error: str | None = None) -> None:
        """Mark commis as complete and update metadata.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        status
            Final status ("success", "failed", "timeout")
        error
            Optional error message if status is "failed"
        """
        commis_dir = self._get_commis_dir(commis_id)
        metadata_path = commis_dir / "metadata.json"

        # Read current metadata
        with open(metadata_path, "r") as f:
            metadata = json.load(f)

        # Update completion fields
        now = datetime.now(timezone.utc)
        metadata["status"] = status
        metadata["finished_at"] = now.isoformat()
        metadata["error"] = error

        # Calculate duration if started_at exists
        if metadata.get("started_at"):
            started = datetime.fromisoformat(metadata["started_at"])
            duration = (now - started).total_seconds() * 1000
            metadata["duration_ms"] = int(duration)

        # Write updated metadata
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Update index
        self._update_index(commis_id, metadata)

        logger.info(f"Completed commis: {commis_id} (status={status})")

    def start_commis(self, commis_id: str) -> None:
        """Mark commis as started (updates metadata).

        Parameters
        ----------
        commis_id
            Unique commis identifier
        """
        commis_dir = self._get_commis_dir(commis_id)
        metadata_path = commis_dir / "metadata.json"

        # Read current metadata
        with open(metadata_path, "r") as f:
            metadata = json.load(f)

        # Update started timestamp
        metadata["status"] = "running"
        metadata["started_at"] = datetime.now(timezone.utc).isoformat()

        # Write updated metadata
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Update index
        self._update_index(commis_id, metadata)

        logger.info(f"Started commis: {commis_id}")

    def get_commis_metadata(self, commis_id: str, owner_id: int | None = None) -> dict[str, Any]:
        """Read commis metadata.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        owner_id
            Optional owner ID to enforce access control

        Returns
        -------
        dict
            Commis metadata

        Raises
        ------
        FileNotFoundError
            If commis does not exist
        PermissionError
            If commis belongs to a different owner
        """
        commis_dir = self._get_commis_dir(commis_id)
        metadata_path = commis_dir / "metadata.json"

        with open(metadata_path, "r") as f:
            metadata = json.load(f)

        # Check ownership if owner_id provided
        if owner_id is not None:
            commis_owner = metadata.get("config", {}).get("owner_id")
            # Only enforce if commis has an owner set
            if commis_owner is not None and commis_owner != owner_id:
                raise PermissionError(f"Access denied to commis {commis_id}")

        return metadata

    def get_commis_result(self, commis_id: str) -> str:
        """Read commis result.

        Parameters
        ----------
        commis_id
            Unique commis identifier

        Returns
        -------
        str
            Final result text

        Raises
        ------
        FileNotFoundError
            If result.txt does not exist
        """
        commis_dir = self._get_commis_dir(commis_id)
        result_path = commis_dir / "result.txt"

        with open(result_path, "r") as f:
            return f.read()

    def read_commis_file(self, commis_id: str, relative_path: str) -> str:
        """Read any file within a commis directory.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        relative_path
            Path relative to commis directory (e.g., "tool_calls/001_ssh_exec.txt")

        Returns
        -------
        str
            File contents

        Raises
        ------
        FileNotFoundError
            If file does not exist
        ValueError
            If relative_path attempts directory traversal
        """
        # Security: prevent directory traversal
        if ".." in relative_path or relative_path.startswith("/"):
            raise ValueError("Invalid relative path (no traversal allowed)")

        commis_dir = self._get_commis_dir(commis_id)
        file_path = commis_dir / relative_path

        # Ensure resolved path is still within commis directory
        if not file_path.resolve().is_relative_to(commis_dir.resolve()):
            raise ValueError("Path escapes commis directory")

        with open(file_path, "r") as f:
            return f.read()

    def list_commis(
        self,
        limit: int = 50,
        status: str | None = None,
        since: datetime | None = None,
        owner_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List commis with optional filters.

        Parameters
        ----------
        limit
            Maximum number of commis to return
        status
            Filter by status ("success", "failed", "running", etc.)
        since
            Filter commis created after this timestamp
        owner_id
            Filter by owner ID (for security)

        Returns
        -------
        list[dict]
            List of commis metadata entries
        """
        index = self._read_index()

        # Apply filters
        filtered = index
        if owner_id is not None:
            # Filter by owner_id in config
            # Note: older commis might not have owner_id, they are effectively "public" or "orphan"
            # For strict security, we might want to exclude them, but for now we filter only if they have an ID
            # that doesn't match.
            filtered = [w for w in filtered if w.get("config", {}).get("owner_id") == owner_id]

        if status:
            filtered = [w for w in filtered if w.get("status") == status]
        if since:
            since_iso = since.isoformat()
            filtered = [w for w in filtered if w.get("created_at", "") >= since_iso]

        # Sort by created_at descending (newest first)
        filtered.sort(key=lambda w: w.get("created_at", ""), reverse=True)

        # Apply limit
        return filtered[:limit]

    def search_commis(
        self,
        pattern: str,
        file_glob: str = "*.txt",
        commis_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search across commis artifacts using regex pattern.

        Parameters
        ----------
        pattern
            Regex pattern to search for
        file_glob
            File glob pattern (e.g., "*.txt", "tool_calls/*.txt")
        commis_ids
            Optional list of commis_ids to restrict the search scope

        Returns
        -------
        list[dict]
            List of matches with context:
            [
                {
                    "commis_id": "...",
                    "file": "result.txt",
                    "line": 42,
                    "content": "matching line content",
                    "metadata": {...}
                },
                ...
            ]
        """
        import re

        matches = []
        compiled_pattern = re.compile(pattern)

        # Get all commis
        commis = self.list_commis(limit=1000)  # Reasonable upper bound

        # Restrict search scope if commis_ids provided
        if commis_ids:
            allowed_ids = set(commis_ids)
            commis = [w for w in commis if w.get("commis_id") in allowed_ids]

        for commis in commis:
            commis_id = commis["commis_id"]
            commis_dir = self._get_commis_dir(commis_id)

            # Get matching files
            matching_files = list(commis_dir.glob(file_glob))
            if not matching_files:
                continue

            # Search each file
            for file_path in matching_files:
                try:
                    with open(file_path, "r") as f:
                        for line_num, line in enumerate(f, start=1):
                            if compiled_pattern.search(line):
                                matches.append(
                                    {
                                        "commis_id": commis_id,
                                        "file": file_path.name,
                                        "line": line_num,
                                        "content": line.strip(),
                                        "metadata": commis,
                                    }
                                )
                except Exception as e:
                    logger.debug(f"Failed to search {file_path}: {e}")
                    continue

        return matches

    def _update_index_entry(self, commis_id: str, updates: dict[str, Any]) -> None:
        """Update specific fields on an existing index entry.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        updates
            Dictionary of fields to update (merged into existing entry)
        """
        with self._index_lock():
            index = self._read_index_unlocked()

            for entry in index:
                if entry.get("commis_id") == commis_id:
                    entry.update(updates)
                    break

            self._write_index_unlocked(index)

    def update_summary(self, commis_id: str, summary: str, summary_meta: dict[str, Any]) -> None:
        """Update commis metadata with extracted summary.

        Called after commis completes. Safe to fail - summary is derived data.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        summary
            Compressed summary text (typically ~150 chars)
        summary_meta
            Metadata about summary generation (version, model, timestamp)
        """
        commis_dir = self._get_commis_dir(commis_id)
        metadata_path = commis_dir / "metadata.json"

        try:
            # Read current metadata
            with open(metadata_path, "r") as f:
                metadata = json.load(f)

            # Add summary fields
            metadata["summary"] = summary
            metadata["summary_meta"] = summary_meta

            # Write updated metadata
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

            # Update index with summary for efficient listing
            self._update_index_entry(commis_id, {"summary": summary})

            logger.debug(f"Updated summary for commis: {commis_id}")

        except Exception as e:
            # Summary update failure is non-fatal - log and continue
            logger.warning(f"Failed to update summary for commis {commis_id}: {e}")

    def save_metric(self, commis_id: str, metric: dict[str, Any]) -> None:
        """Append a metric event to metrics.jsonl.

        This is the lowest-level append operation. For structured metrics
        logging, consider using MetricsCollector context manager instead.

        Parameters
        ----------
        commis_id
            Unique commis identifier
        metric
            Metric dict (event, timestamps, duration, etc.)
        """
        commis_dir = self._get_commis_dir(commis_id)
        metrics_path = commis_dir / "metrics.jsonl"

        # Append metric as JSON line
        with open(metrics_path, "a") as f:
            f.write(json.dumps(metric) + "\n")


__all__ = ["CommisArtifactStore"]
