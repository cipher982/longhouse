"""Evidence Compiler – deterministic assembly of commis evidence within token/byte budgets.

This module implements Phase 1 of the Mount → Reason → Prune system.
It assembles commis tool outputs (evidence) within strict budgets, applying
prioritization and truncation strategies.

Philosophy (from TRACE_FIRST_NORTH_STAR.md):
- Trace is truth (append-only execution record)
- Evidence mounting is deterministic (no LLM summarization)
- Prioritize failures first, then most recent outputs
- Use head+tail truncation to preserve context and final results

References:
- docs/specs/TRACE_FIRST_NORTH_STAR.md
- docs/specs/MOUNT_REASON_PRUNE_IMPLEMENTATION.md
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from zerg.crud.crud_commis_jobs import get_by_concierge_run
from zerg.services.commis_artifact_store import CommisArtifactStore

logger = logging.getLogger(__name__)


@dataclass
class ToolArtifact:
    """Metadata about a tool output artifact."""

    sequence: int  # Tool call sequence number
    filename: str  # e.g., "001_ssh_exec.txt"
    tool_name: str  # e.g., "ssh_exec"
    size_bytes: int  # Size of the artifact
    exit_code: int | None  # Exit code if available (from tool output JSON)
    failed: bool  # True if this tool call failed


class EvidenceCompiler:
    """Compile commis evidence within budgets using deterministic prioritization."""

    # Truncation constants
    HEAD_SIZE = 1024  # First 1KB always included
    TRUNCATION_MARKER_TEMPLATE = "\n[...truncated {truncated_bytes} bytes...]\n"

    def __init__(
        self,
        artifact_store: CommisArtifactStore | None = None,
        db: Session | None = None,
    ):
        """Initialize the evidence compiler.

        Parameters
        ----------
        artifact_store
            Artifact store for reading commis files. Creates default if None.
        db
            Database session for querying CommisJob metadata. Optional.
        """
        self.artifact_store = artifact_store or CommisArtifactStore()
        self.db = db

    def compile(
        self,
        course_id: int,
        owner_id: int,
        budget_bytes: int = 32000,
        db: Session | None = None,
    ) -> dict[int, str]:
        """Compile evidence for all commis in a concierge run within budget.

        Parameters
        ----------
        course_id
            Concierge run ID to compile evidence for
        owner_id
            User ID for security scoping
        budget_bytes
            Total budget across all commis (default: 32KB)
        db
            Database session override (uses self.db if not provided)

        Returns
        -------
        dict[int, str]
            Mapping of {job_id: expanded_evidence_string}
        """
        session = db or self.db
        if session is None:
            logger.warning(f"No database session available for evidence compilation (course_id={course_id})")
            return {}

        # Query all commis jobs for this concierge run
        jobs = get_by_concierge_run(session, concierge_course_id=course_id, owner_id=owner_id)

        if not jobs:
            logger.debug(f"No commis jobs found for course_id={course_id}, owner_id={owner_id}")
            return {}

        # Calculate per-commis budget
        per_commis_budget = budget_bytes // len(jobs) if jobs else budget_bytes

        # Compile evidence for each commis
        evidence_map = {}
        for job in jobs:
            if not job.commis_id:
                # Job hasn't started execution yet
                continue

            try:
                evidence = self._compile_commis_evidence(
                    job_id=job.id,
                    commis_id=job.commis_id,
                    owner_id=owner_id,
                    budget=per_commis_budget,
                )
                evidence_map[job.id] = evidence
            except FileNotFoundError:
                logger.warning(f"Commis artifacts not found for job {job.id} (commis_id={job.commis_id})")
                evidence_map[job.id] = f"[Evidence unavailable: commis artifacts not found for job {job.id}]"
            except Exception as e:
                logger.error(f"Failed to compile evidence for job {job.id}: {e}", exc_info=True)
                evidence_map[job.id] = f"[Evidence compilation failed: {e}]"

        return evidence_map

    def compile_for_job(
        self,
        *,
        job_id: int,
        commis_id: str,
        owner_id: int,
        budget_bytes: int = 32000,
    ) -> str:
        """Compile evidence for a single commis job within budget.

        Parameters
        ----------
        job_id
            Commis job ID
        commis_id
            Commis ID (filesystem directory name)
        owner_id
            User ID for security scoping
        budget_bytes
            Budget in bytes for this commis's evidence

        Returns
        -------
        str
            Formatted evidence string (with safe fallbacks on error)
        """
        try:
            return self._compile_commis_evidence(
                job_id=job_id,
                commis_id=commis_id,
                owner_id=owner_id,
                budget=budget_bytes,
            )
        except FileNotFoundError:
            logger.warning("Commis artifacts not found for job %s (commis_id=%s)", job_id, commis_id)
            return f"[Evidence unavailable: commis artifacts not found for job {job_id}]"
        except PermissionError:
            return f"[Access denied to commis {commis_id}]"
        except Exception as e:
            logger.error("Failed to compile evidence for job %s: %s", job_id, e, exc_info=True)
            return f"[Evidence compilation failed: {e}]"

    def _compile_commis_evidence(
        self,
        job_id: int,
        commis_id: str,
        owner_id: int,
        budget: int,
    ) -> str:
        """Compile evidence for a single commis within budget.

        Parameters
        ----------
        job_id
            Commis job ID
        commis_id
            Commis ID (filesystem directory name)
        owner_id
            User ID for security scoping
        budget
            Budget in bytes for this commis's evidence

        Returns
        -------
        str
            Formatted evidence string
        """
        # Verify access to commis metadata (security check)
        try:
            _ = self.artifact_store.get_commis_metadata(commis_id, owner_id=owner_id)
        except PermissionError:
            return f"[Access denied to commis {commis_id}]"

        # Discover and prioritize tool artifacts
        artifacts = self._discover_tool_artifacts(commis_id)

        if not artifacts:
            return f"--- Evidence for Commis {job_id} ({commis_id}) ---\nNo tool outputs found.\n--- End Evidence ---"

        # Prioritize artifacts
        prioritized = self._prioritize_artifacts(artifacts)

        # Build evidence output within budget
        lines = [
            f"--- Evidence for Commis {job_id} ({commis_id}) ---",
            f"Budget: {budget}B | Files: {len(artifacts)}",
            "",
        ]

        remaining_budget = budget - sum(len(line.encode("utf-8")) for line in lines)

        for artifact in prioritized:
            if remaining_budget <= 0:
                break

            artifact_section = self._format_artifact(commis_id, artifact, remaining_budget)
            artifact_bytes = len(artifact_section.encode("utf-8"))

            lines.append(artifact_section)
            remaining_budget -= artifact_bytes

        lines.extend(["", "--- End Evidence ---"])

        return "\n".join(lines)

    def _discover_tool_artifacts(self, commis_id: str) -> list[ToolArtifact]:
        """Discover all tool call artifacts for a commis.

        Parameters
        ----------
        commis_id
            Commis ID (filesystem directory name)

        Returns
        -------
        list[ToolArtifact]
            List of discovered artifacts with metadata
        """
        commis_dir = self.artifact_store._get_commis_dir(commis_id)
        tool_calls_dir = commis_dir / "tool_calls"

        if not tool_calls_dir.exists():
            return []

        artifacts = []

        for filepath in sorted(tool_calls_dir.glob("*.txt")):
            # Parse filename: "001_ssh_exec.txt" -> sequence=1, tool_name="ssh_exec"
            filename = filepath.name
            try:
                seq_str, tool_name_ext = filename.split("_", 1)
                sequence = int(seq_str)
                tool_name = tool_name_ext.replace(".txt", "")
            except ValueError:
                logger.warning(f"Skipping malformed artifact filename: {filename}")
                continue

            # Get file size
            size_bytes = filepath.stat().st_size

            # Try to extract exit_code from tool output (best effort)
            exit_code, failed = self._extract_exit_code(filepath)

            artifacts.append(
                ToolArtifact(
                    sequence=sequence,
                    filename=filename,
                    tool_name=tool_name,
                    size_bytes=size_bytes,
                    exit_code=exit_code,
                    failed=failed,
                )
            )

        return artifacts

    def _extract_exit_code(self, filepath: Path) -> tuple[int | None, bool]:
        """Extract exit code from tool output (best effort).

        Tool outputs are JSON envelopes with structure:
        {"ok": True/False, "data": {...}, "error": ...}

        For ssh_exec, data contains: {"exit_code": N, "stdout": ..., "stderr": ...}

        Parameters
        ----------
        filepath
            Path to tool output file

        Returns
        -------
        tuple[int | None, bool]
            (exit_code, failed) - exit_code is None if not extractable
        """
        try:
            content = filepath.read_text()
            data = json.loads(content)

            # Check if this is an error envelope
            if not data.get("ok", True):
                return (None, True)

            # Try to extract exit_code from data
            tool_data = data.get("data", {})
            if isinstance(tool_data, dict):
                exit_code = tool_data.get("exit_code")
                if exit_code is not None:
                    # Non-zero exit code means command failed
                    return (exit_code, exit_code != 0)

            return (None, False)

        except (json.JSONDecodeError, OSError):
            # Can't parse - assume not failed
            return (None, False)

    def _prioritize_artifacts(self, artifacts: list[ToolArtifact]) -> list[ToolArtifact]:
        """Prioritize artifacts for evidence mounting.

        Priority order:
        1. Failed tool outputs (exit_code != 0 or error envelope)
        2. Most recent tool outputs (higher sequence numbers)
        3. Larger outputs (more likely to contain detail)

        Parameters
        ----------
        artifacts
            List of artifacts to prioritize

        Returns
        -------
        list[ToolArtifact]
            Sorted list (highest priority first)
        """
        return sorted(
            artifacts,
            key=lambda a: (
                not a.failed,  # False (failed) sorts before True (success)
                -a.sequence,  # Higher sequence first
                -a.size_bytes,  # Larger files first
            ),
        )

    def _format_artifact(self, commis_id: str, artifact: ToolArtifact, budget: int) -> str:
        """Format a single artifact for evidence output, with truncation if needed.

        Parameters
        ----------
        commis_id
            Commis ID (for reading file)
        artifact
            Artifact metadata
        budget
            Remaining budget in bytes

        Returns
        -------
        str
            Formatted artifact section
        """
        # Read artifact content
        try:
            content = self.artifact_store.read_commis_file(commis_id, f"tool_calls/{artifact.filename}")
        except FileNotFoundError:
            return f"[MISSING] {artifact.filename} (file not found)"

        # Build header
        status_tag = "[FAILED] " if artifact.failed else ""
        exit_info = f", exit={artifact.exit_code}" if artifact.exit_code is not None else ""
        header = f"{status_tag}tool_calls/{artifact.filename} ({artifact.size_bytes}B{exit_info}):"

        # Check if truncation is needed
        content_bytes = len(content.encode("utf-8"))
        header_bytes = len(header.encode("utf-8"))
        available_budget = budget - header_bytes - 10  # Reserve space for newlines

        if content_bytes <= available_budget:
            # No truncation needed
            return f"{header}\n{content}\n"

        # Apply head+tail truncation
        truncated_content = self._truncate_with_head_tail(content, available_budget)
        return f"{header}\n{truncated_content}\n"

    def _truncate_with_head_tail(self, content: str, budget: int) -> str:
        """Truncate content using head+tail strategy with marker.

        Parameters
        ----------
        content
            Content to truncate
        budget
            Budget in bytes for truncated output

        Returns
        -------
        str
            Truncated content with marker
        """
        content_bytes = content.encode("utf-8")
        total_bytes = len(content_bytes)

        if total_bytes <= budget:
            return content

        # Reserve space for truncation marker (approximate)
        marker_template = self.TRUNCATION_MARKER_TEMPLATE.format(truncated_bytes=999999)
        marker_bytes = len(marker_template.encode("utf-8"))

        # Calculate head and tail sizes
        available = budget - marker_bytes
        if available < self.HEAD_SIZE * 2:
            # Budget too small for head+tail, just return truncated head
            head_bytes = content_bytes[:budget]
            return head_bytes.decode("utf-8", errors="replace") + "..."

        head_size = min(self.HEAD_SIZE, available // 2)
        tail_size = available - head_size

        # Extract head and tail
        head_bytes = content_bytes[:head_size]
        tail_bytes = content_bytes[-tail_size:]

        head = head_bytes.decode("utf-8", errors="replace")
        tail = tail_bytes.decode("utf-8", errors="replace")

        truncated_bytes = total_bytes - head_size - tail_size
        marker = self.TRUNCATION_MARKER_TEMPLATE.format(truncated_bytes=truncated_bytes)

        return f"{head}{marker}{tail}"


__all__ = ["EvidenceCompiler", "ToolArtifact"]
