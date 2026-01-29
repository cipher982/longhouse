"""Parser for Claude Code native JSONL session format.

Claude Code stores sessions at ~/.claude/projects/{encoded_cwd}/{sessionId}.jsonl

Each line is a JSON object with a "type" field:
- "user": User message
- "assistant": Assistant message (may contain text and/or tool_use)
- "summary": Session summary (metadata only)
- "file-history-snapshot": File tracking (metadata only)
- "progress": Subagent progress updates (metadata only)

This parser extracts the meaningful events (user messages, assistant text,
tool calls) and converts them to a normalized format for the agents schema.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class ParsedEvent:
    """A normalized event extracted from Claude Code JSONL."""

    uuid: str
    session_id: str
    timestamp: datetime
    role: str  # 'user' | 'assistant' | 'tool'
    content_text: str | None = None
    tool_name: str | None = None
    tool_input_json: dict | None = None
    tool_output_text: str | None = None
    source_offset: int = 0  # byte offset in file
    raw_type: str = ""  # original type (user, assistant, summary, etc.)
    raw_line: str = ""  # original JSONL line for lossless archiving

    def to_event_ingest(self, source_path: str) -> dict:
        """Convert to EventIngest format for the API."""
        return {
            "role": self.role,
            "content_text": self.content_text,
            "tool_name": self.tool_name,
            "tool_input_json": self.tool_input_json,
            "tool_output_text": self.tool_output_text,
            "timestamp": self.timestamp.isoformat(),
            "source_path": source_path,
            "source_offset": self.source_offset,
            "raw_json": self.raw_line if self.raw_line else None,
        }


@dataclass
class ParsedSession:
    """Metadata extracted from a Claude Code session file."""

    session_id: str
    cwd: str | None = None
    git_branch: str | None = None
    project: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    version: str | None = None  # Claude Code version
    events: list[ParsedEvent] = field(default_factory=list)


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        # Handle both with and without timezone
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _extract_user_content(message: dict) -> str | None:
    """Extract text content from a user message."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Concatenate text parts
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    # Tool result in user message (response to assistant tool call)
                    result = item.get("content", "")
                    if isinstance(result, str):
                        parts.append(result)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else None
    return None


def _extract_assistant_events(
    obj: dict,
    session_id: str,
    offset: int,
    raw_line: str = "",
) -> Iterator[ParsedEvent]:
    """Extract events from an assistant message.

    Assistant messages can contain:
    - text: Regular response text
    - tool_use: Tool calls (need to be paired with results from user messages)
    - thinking: Internal reasoning (skip for now)
    """
    message = obj.get("message", {})
    content = message.get("content", [])
    timestamp = _parse_timestamp(obj.get("timestamp"))
    msg_uuid = obj.get("uuid", str(uuid.uuid4()))

    if not timestamp:
        timestamp = datetime.now(timezone.utc)

    if not isinstance(content, list):
        return

    for idx, item in enumerate(content):
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")

        if item_type == "text":
            text = item.get("text", "")
            if text.strip():
                yield ParsedEvent(
                    uuid=f"{msg_uuid}-text-{idx}",
                    session_id=session_id,
                    timestamp=timestamp,
                    role="assistant",
                    content_text=text,
                    source_offset=offset,
                    raw_type="assistant",
                    raw_line=raw_line,
                )

        elif item_type == "tool_use":
            tool_name = item.get("name", "")
            tool_input = item.get("input", {})
            tool_id = item.get("id", "")

            yield ParsedEvent(
                uuid=f"{msg_uuid}-tool-{tool_id or idx}",
                session_id=session_id,
                timestamp=timestamp,
                role="assistant",
                tool_name=tool_name,
                tool_input_json=tool_input if isinstance(tool_input, dict) else None,
                source_offset=offset,
                raw_type="assistant",
                raw_line=raw_line,
            )


def _extract_tool_results(
    obj: dict,
    session_id: str,
    offset: int,
    raw_line: str = "",
) -> Iterator[ParsedEvent]:
    """Extract tool results from user messages (responses to tool_use).

    When Claude calls a tool, the result comes back in a user message
    with content containing tool_result items.
    """
    message = obj.get("message", {})
    content = message.get("content", [])
    timestamp = _parse_timestamp(obj.get("timestamp"))
    msg_uuid = obj.get("uuid", str(uuid.uuid4()))

    if not timestamp:
        timestamp = datetime.now(timezone.utc)

    if not isinstance(content, list):
        return

    for idx, item in enumerate(content):
        if not isinstance(item, dict):
            continue

        if item.get("type") == "tool_result":
            tool_use_id = item.get("tool_use_id", "")
            result_content = item.get("content", "")

            # Content can be string or list
            if isinstance(result_content, list):
                parts = []
                for part in result_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        parts.append(part)
                result_text = "\n".join(parts)
            else:
                result_text = str(result_content) if result_content else None

            if result_text:
                yield ParsedEvent(
                    uuid=f"{msg_uuid}-result-{tool_use_id or idx}",
                    session_id=session_id,
                    timestamp=timestamp,
                    role="tool",
                    tool_output_text=result_text,
                    source_offset=offset,
                    raw_type="tool_result",
                    raw_line=raw_line,
                )


def parse_session_file(path: Path, offset: int = 0) -> Iterator[ParsedEvent]:
    """Parse Claude Code JSONL file starting from byte offset.

    Args:
        path: Path to the session JSONL file
        offset: Byte offset to start reading from (for incremental sync)

    Yields:
        ParsedEvent objects for each meaningful event in the file

    The session_id is extracted from the filename (UUID.jsonl).
    """
    session_id = path.stem  # filename without .jsonl

    try:
        with open(path, "rb") as f:
            # Seek to offset
            f.seek(offset)

            while True:
                line_offset = f.tell()
                line = f.readline()

                if not line:
                    break

                # Skip empty lines
                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    continue

                try:
                    obj = json.loads(line_text)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON at offset {line_offset}: {e}")
                    continue

                event_type = obj.get("type", "")

                # Skip metadata-only types
                if event_type in ("summary", "file-history-snapshot", "progress"):
                    continue

                if event_type == "user":
                    # Check if this is a tool result response
                    message = obj.get("message", {})
                    content = message.get("content", [])

                    has_tool_result = False
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result":
                                has_tool_result = True
                                break

                    if has_tool_result:
                        # Extract tool results
                        yield from _extract_tool_results(obj, session_id, line_offset, raw_line=line_text)
                    else:
                        # Regular user message
                        text = _extract_user_content(message)
                        if text and text.strip():
                            timestamp = _parse_timestamp(obj.get("timestamp"))
                            if not timestamp:
                                timestamp = datetime.now(timezone.utc)

                            yield ParsedEvent(
                                uuid=obj.get("uuid", str(uuid.uuid4())),
                                session_id=session_id,
                                timestamp=timestamp,
                                role="user",
                                content_text=text,
                                source_offset=line_offset,
                                raw_type="user",
                                raw_line=line_text,
                            )

                elif event_type == "assistant":
                    yield from _extract_assistant_events(obj, session_id, line_offset, raw_line=line_text)

    except Exception as e:
        logger.exception(f"Error parsing session file {path}: {e}")


def extract_session_metadata(path: Path) -> ParsedSession:
    """Extract session metadata from a Claude Code JSONL file.

    Reads the file to extract:
    - session_id (from filename)
    - cwd (from first user/assistant message)
    - git_branch
    - project (derived from cwd)
    - started_at (earliest timestamp)
    - ended_at (latest timestamp)
    """
    session_id = path.stem
    result = ParsedSession(session_id=session_id)

    timestamps: list[datetime] = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Extract metadata from any message type that has it
                if "cwd" in obj and not result.cwd:
                    result.cwd = obj["cwd"]

                if "gitBranch" in obj and not result.git_branch:
                    result.git_branch = obj["gitBranch"]

                if "version" in obj and not result.version:
                    result.version = obj["version"]

                # Collect timestamps
                ts = _parse_timestamp(obj.get("timestamp"))
                if ts:
                    timestamps.append(ts)

        # Derive project from cwd
        if result.cwd:
            # Use last component of path as project name
            result.project = Path(result.cwd).name

        # Set time bounds
        if timestamps:
            result.started_at = min(timestamps)
            result.ended_at = max(timestamps)

    except Exception as e:
        logger.warning(f"Error extracting metadata from {path}: {e}")

    return result
