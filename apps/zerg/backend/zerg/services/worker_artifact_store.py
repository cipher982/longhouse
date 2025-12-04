"""Worker Artifact Store – filesystem persistence for disposable worker agents.

This service manages the filesystem structure for worker artifacts, enabling
workers to persist all outputs (tool calls, messages, results) to disk for
later retrieval by supervisor agents.

INVARIANTS:
- result.txt is canonical. Never delete or auto-truncate.
- metadata.json contains derived views (summaries, extracted fields).
- Derived data MUST be recomputable from canonical artifacts.
- System decisions (status) never depend on LLM-generated summaries.

Directory structure:
    /data/swarmlet/workers/
    ├── index.json                    # Master index of all workers
    └── {worker_id}/                  # e.g., "2024-12-03T14-32-00_disk-check"
        ├── metadata.json             # Status, timestamps, task, config
        ├── result.txt                # Final natural language result
        ├── thread.jsonl              # Full conversation (messages)
        └── tool_calls/               # Raw tool outputs
            ├── 001_ssh_exec.txt
            ├── 002_http_request.json
            └── ...

The worker_id format is: "{timestamp}_{slug}" e.g., "2024-12-03T14-32-00_disk-check"
where the slug is derived from the task description (first 30 chars, kebab-case).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class WorkerArtifactStore:
    """Manages filesystem storage for worker agent artifacts."""

    def __init__(self, base_path: str | None = None):
        """Initialize the artifact store.

        Parameters
        ----------
        base_path
            Root directory for worker artifacts. If None, reads from
            SWARMLET_DATA_PATH environment variable, defaulting to
            "/data/swarmlet/workers" in production.
        """
        if base_path is None:
            base_path = os.getenv("SWARMLET_DATA_PATH", "/data/swarmlet/workers")

        self.base_path = Path(base_path)
        self.index_path = self.base_path / "index.json"

        # Ensure base directory exists
        self.base_path.mkdir(parents=True, exist_ok=True)

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

    def _generate_worker_id(self, task: str) -> str:
        """Generate a unique worker ID from timestamp and task.

        Parameters
        ----------
        task
            Task description

        Returns
        -------
        str
            Worker ID in format: "{timestamp}_{slug}"
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        slug = self._slugify(task)
        return f"{timestamp}_{slug}"

    def _get_worker_dir(self, worker_id: str) -> Path:
        """Get the directory path for a worker.

        Parameters
        ----------
        worker_id
            Unique worker identifier

        Returns
        -------
        Path
            Directory path for the worker
        """
        return self.base_path / worker_id

    def _read_index(self) -> list[dict[str, Any]]:
        """Read the master index file.

        Returns
        -------
        list[dict]
            List of worker metadata entries
        """
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
            List of worker metadata entries
        """
        with open(self.index_path, "w") as f:
            json.dump(index, f, indent=2)

    def _update_index(self, worker_id: str, metadata: dict[str, Any]) -> None:
        """Update or insert a worker entry in the index.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        metadata
            Worker metadata to store
        """
        index = self._read_index()

        # Find existing entry or append new one
        for i, entry in enumerate(index):
            if entry.get("worker_id") == worker_id:
                index[i] = metadata
                break
        else:
            index.append(metadata)

        self._write_index(index)

    def create_worker(
        self,
        task: str,
        config: dict[str, Any] | None = None,
        owner_id: int | None = None,
    ) -> str:
        """Create a new worker directory structure.

        Parameters
        ----------
        task
            Task description for the worker
        config
            Optional configuration dict (e.g., model, tools, timeout)
        owner_id
            Optional ID of the user who owns this worker (for security filtering)

        Returns
        -------
        str
            Unique worker_id

        Raises
        ------
        ValueError
            If worker_id already exists (collision)
        """
        worker_id = self._generate_worker_id(task)
        worker_dir = self._get_worker_dir(worker_id)

        # Check for collision (shouldn't happen with timestamp)
        if worker_dir.exists():
            raise ValueError(f"Worker directory already exists: {worker_id}")

        # Create directory structure
        worker_dir.mkdir(parents=True, exist_ok=True)
        tool_calls_dir = worker_dir / "tool_calls"
        tool_calls_dir.mkdir(exist_ok=True)

        # Initialize config
        worker_config = config or {}
        # Store owner_id in config if provided
        if owner_id is not None:
            worker_config["owner_id"] = owner_id

        # Initialize metadata
        metadata = {
            "worker_id": worker_id,
            "task": task,
            "config": worker_config,
            "status": "created",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "error": None,
        }

        # Write metadata file
        metadata_path = worker_dir / "metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Update index
        self._update_index(worker_id, metadata)

        logger.info(f"Created worker directory: {worker_id}")
        return worker_id

    def save_tool_output(
        self, worker_id: str, tool_name: str, output: str, sequence: int
    ) -> str:
        """Save tool output to a file.

        Parameters
        ----------
        worker_id
            Unique worker identifier
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
        worker_dir = self._get_worker_dir(worker_id)
        tool_calls_dir = worker_dir / "tool_calls"

        # Generate filename
        filename = f"{sequence:03d}_{tool_name}.txt"
        filepath = tool_calls_dir / filename

        # Write output
        with open(filepath, "w") as f:
            f.write(output)

        logger.debug(f"Saved tool output: {worker_id}/{filename}")
        return f"tool_calls/{filename}"

    def save_message(self, worker_id: str, message: dict[str, Any]) -> None:
        """Append a message to the thread.jsonl file.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        message
            Message dict (role, content, etc.)
        """
        worker_dir = self._get_worker_dir(worker_id)
        thread_path = worker_dir / "thread.jsonl"

        # Append message as JSON line
        with open(thread_path, "a") as f:
            f.write(json.dumps(message) + "\n")

    def save_result(self, worker_id: str, result: str) -> None:
        """Save final result to result.txt.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        result
            Final natural language result from the agent
        """
        worker_dir = self._get_worker_dir(worker_id)
        result_path = worker_dir / "result.txt"

        with open(result_path, "w") as f:
            f.write(result)

        logger.info(f"Saved worker result: {worker_id}")

    def complete_worker(
        self, worker_id: str, status: str = "success", error: str | None = None
    ) -> None:
        """Mark worker as complete and update metadata.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        status
            Final status ("success", "failed", "timeout")
        error
            Optional error message if status is "failed"
        """
        worker_dir = self._get_worker_dir(worker_id)
        metadata_path = worker_dir / "metadata.json"

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
        self._update_index(worker_id, metadata)

        logger.info(f"Completed worker: {worker_id} (status={status})")

    def start_worker(self, worker_id: str) -> None:
        """Mark worker as started (updates metadata).

        Parameters
        ----------
        worker_id
            Unique worker identifier
        """
        worker_dir = self._get_worker_dir(worker_id)
        metadata_path = worker_dir / "metadata.json"

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
        self._update_index(worker_id, metadata)

        logger.info(f"Started worker: {worker_id}")

    def get_worker_metadata(self, worker_id: str, owner_id: int | None = None) -> dict[str, Any]:
        """Read worker metadata.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        owner_id
            Optional owner ID to enforce access control

        Returns
        -------
        dict
            Worker metadata

        Raises
        ------
        FileNotFoundError
            If worker does not exist
        PermissionError
            If worker belongs to a different owner
        """
        worker_dir = self._get_worker_dir(worker_id)
        metadata_path = worker_dir / "metadata.json"

        with open(metadata_path, "r") as f:
            metadata = json.load(f)

        # Check ownership if owner_id provided
        if owner_id is not None:
            worker_owner = metadata.get("config", {}).get("owner_id")
            # Only enforce if worker has an owner set
            if worker_owner is not None and worker_owner != owner_id:
                raise PermissionError(f"Access denied to worker {worker_id}")

        return metadata

    def get_worker_result(self, worker_id: str) -> str:
        """Read worker result.

        Parameters
        ----------
        worker_id
            Unique worker identifier

        Returns
        -------
        str
            Final result text

        Raises
        ------
        FileNotFoundError
            If result.txt does not exist
        """
        worker_dir = self._get_worker_dir(worker_id)
        result_path = worker_dir / "result.txt"

        with open(result_path, "r") as f:
            return f.read()

    def read_worker_file(self, worker_id: str, relative_path: str) -> str:
        """Read any file within a worker directory.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        relative_path
            Path relative to worker directory (e.g., "tool_calls/001_ssh_exec.txt")

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

        worker_dir = self._get_worker_dir(worker_id)
        file_path = worker_dir / relative_path

        # Ensure resolved path is still within worker directory
        if not file_path.resolve().is_relative_to(worker_dir.resolve()):
            raise ValueError("Path escapes worker directory")

        with open(file_path, "r") as f:
            return f.read()

    def list_workers(
        self,
        limit: int = 50,
        status: str | None = None,
        since: datetime | None = None,
        owner_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """List workers with optional filters.

        Parameters
        ----------
        limit
            Maximum number of workers to return
        status
            Filter by status ("success", "failed", "running", etc.)
        since
            Filter workers created after this timestamp
        owner_id
            Filter by owner ID (for security)

        Returns
        -------
        list[dict]
            List of worker metadata entries
        """
        index = self._read_index()

        # Apply filters
        filtered = index
        if owner_id is not None:
            # Filter by owner_id in config
            # Note: older workers might not have owner_id, they are effectively "public" or "orphan"
            # For strict security, we might want to exclude them, but for now we filter only if they have an ID
            # that doesn't match.
            filtered = [
                w
                for w in filtered
                if w.get("config", {}).get("owner_id") == owner_id
            ]

        if status:
            filtered = [w for w in filtered if w.get("status") == status]
        if since:
            since_iso = since.isoformat()
            filtered = [w for w in filtered if w.get("created_at", "") >= since_iso]

        # Sort by created_at descending (newest first)
        filtered.sort(key=lambda w: w.get("created_at", ""), reverse=True)

        # Apply limit
        return filtered[:limit]

    def search_workers(
        self, pattern: str, file_glob: str = "*.txt"
    ) -> list[dict[str, Any]]:
        """Search across worker artifacts using regex pattern.

        Parameters
        ----------
        pattern
            Regex pattern to search for
        file_glob
            File glob pattern (e.g., "*.txt", "tool_calls/*.txt")

        Returns
        -------
        list[dict]
            List of matches with context:
            [
                {
                    "worker_id": "...",
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

        # Get all workers
        workers = self.list_workers(limit=1000)  # Reasonable upper bound

        for worker in workers:
            worker_id = worker["worker_id"]
            worker_dir = self._get_worker_dir(worker_id)

            # Get matching files
            matching_files = list(worker_dir.glob(file_glob))
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
                                        "worker_id": worker_id,
                                        "file": file_path.name,
                                        "line": line_num,
                                        "content": line.strip(),
                                        "metadata": worker,
                                    }
                                )
                except Exception as e:
                    logger.debug(f"Failed to search {file_path}: {e}")
                    continue

        return matches

    def _update_index_entry(self, worker_id: str, updates: dict[str, Any]) -> None:
        """Update specific fields on an existing index entry.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        updates
            Dictionary of fields to update (merged into existing entry)
        """
        index = self._read_index()

        for entry in index:
            if entry.get("worker_id") == worker_id:
                entry.update(updates)
                break

        self._write_index(index)

    def update_summary(
        self, worker_id: str, summary: str, summary_meta: dict[str, Any]
    ) -> None:
        """Update worker metadata with extracted summary.

        Called after worker completes. Safe to fail - summary is derived data.

        Parameters
        ----------
        worker_id
            Unique worker identifier
        summary
            Compressed summary text (typically ~150 chars)
        summary_meta
            Metadata about summary generation (version, model, timestamp)
        """
        worker_dir = self._get_worker_dir(worker_id)
        metadata_path = worker_dir / "metadata.json"

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
            self._update_index_entry(worker_id, {"summary": summary})

            logger.debug(f"Updated summary for worker: {worker_id}")

        except Exception as e:
            # Summary update failure is non-fatal - log and continue
            logger.warning(f"Failed to update summary for worker {worker_id}: {e}")


__all__ = ["WorkerArtifactStore"]
