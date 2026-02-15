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

import logging
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterator

try:
    import orjson as _json_mod

    def _loads(s: str | bytes) -> dict:
        return _json_mod.loads(s)

    _JSONDecodeError: type[Exception] = _json_mod.JSONDecodeError

except ImportError:
    import json as _json_mod_std

    def _loads(s: str | bytes) -> dict:  # type: ignore[misc]
        return _json_mod_std.loads(s)

    _JSONDecodeError = _json_mod_std.JSONDecodeError  # type: ignore[assignment]

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

    first = True
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
                    raw_line=raw_line if first else "",
                )
                first = False

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
                raw_line=raw_line if first else "",
            )
            first = False


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

    first = True
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
                    raw_line=raw_line if first else "",
                )
                first = False


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
            f.seek(offset)

            while True:
                line_offset = f.tell()
                line = f.readline()

                if not line:
                    break

                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    continue

                try:
                    obj = _loads(line_text)
                except _JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON at offset {line_offset}: {e}")
                    continue

                event_type = obj.get("type", "")

                if event_type in ("summary", "file-history-snapshot", "progress"):
                    continue

                if event_type == "user":
                    message = obj.get("message", {})
                    content = message.get("content", [])

                    has_tool_result = False
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result":
                                has_tool_result = True
                                break

                    if has_tool_result:
                        yield from _extract_tool_results(obj, session_id, line_offset, raw_line=line_text)
                    else:
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


def parse_session_file_with_offset(path: Path, offset: int = 0) -> tuple[list[ParsedEvent], int]:
    """Parse session file and return events with last good byte offset.

    Returns:
        Tuple of (events, last_good_offset) where last_good_offset is the byte
        position after the last successfully parsed line.  A half-written line
        at EOF is excluded.
    """
    events, last_good_offset, _ = _parse_with_offset_tracking(path, offset)
    return events, last_good_offset


def parse_session_file_full(path: Path, offset: int = 0) -> tuple[list[ParsedEvent], int, ParsedSession]:
    """Parse session file, returning events + metadata in a single pass.

    Eliminates the redundant file re-read that extract_session_metadata()
    would perform. Use this instead of calling parse_session_file_with_offset()
    followed by extract_session_metadata().

    Returns:
        Tuple of (events, last_good_offset, metadata)
    """
    events, last_good_offset, meta = _parse_with_offset_tracking(path, offset, collect_metadata=True)
    # meta is always set when collect_metadata=True
    assert meta is not None
    return events, last_good_offset, meta


def _parse_with_offset_tracking(
    path: Path, offset: int = 0, *, collect_metadata: bool = False
) -> tuple[list[ParsedEvent], int, ParsedSession | None]:
    """Parse session file tracking the last good byte offset.

    When collect_metadata=True, extracts session metadata (cwd, branch,
    timestamps) during the same parse pass — eliminating the redundant
    file re-read that extract_session_metadata() would perform.
    """
    session_id = path.stem
    last_good_offset = offset
    events: list[ParsedEvent] = []

    # Metadata collection (only when requested)
    meta: ParsedSession | None = None
    if collect_metadata:
        meta = ParsedSession(session_id=session_id)
        _min_ts: datetime | None = None
        _max_ts: datetime | None = None

    try:
        with open(path, "rb") as f:
            f.seek(offset)

            while True:
                line_offset = f.tell()
                line = f.readline()

                if not line:
                    break

                after_line = f.tell()

                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    last_good_offset = after_line
                    continue

                try:
                    obj = _loads(line_text)
                except _JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON at offset {line_offset}: {e}")
                    continue

                last_good_offset = after_line

                # Collect metadata from every parsed line (cheap field lookups)
                if meta is not None:
                    if "cwd" in obj and not meta.cwd:
                        meta.cwd = obj["cwd"]
                    if "gitBranch" in obj and not meta.git_branch:
                        meta.git_branch = obj["gitBranch"]
                    if "version" in obj and not meta.version:
                        meta.version = obj["version"]
                    ts = _parse_timestamp(obj.get("timestamp"))
                    if ts:
                        if _min_ts is None or ts < _min_ts:
                            _min_ts = ts
                        if _max_ts is None or ts > _max_ts:
                            _max_ts = ts

                event_type = obj.get("type", "")

                if event_type in ("summary", "file-history-snapshot", "progress"):
                    continue

                if event_type == "user":
                    message = obj.get("message", {})
                    content = message.get("content", [])

                    has_tool_result = False
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "tool_result":
                                has_tool_result = True
                                break

                    if has_tool_result:
                        events.extend(_extract_tool_results(obj, session_id, line_offset, raw_line=line_text))
                    else:
                        text = _extract_user_content(message)
                        if text and text.strip():
                            timestamp = _parse_timestamp(obj.get("timestamp"))
                            if not timestamp:
                                timestamp = datetime.now(timezone.utc)

                            events.append(
                                ParsedEvent(
                                    uuid=obj.get("uuid", str(uuid.uuid4())),
                                    session_id=session_id,
                                    timestamp=timestamp,
                                    role="user",
                                    content_text=text,
                                    source_offset=line_offset,
                                    raw_type="user",
                                    raw_line=line_text,
                                )
                            )

                elif event_type == "assistant":
                    events.extend(_extract_assistant_events(obj, session_id, line_offset, raw_line=line_text))

    except Exception as e:
        logger.exception(f"Error parsing session file {path}: {e}")

    # Finalize metadata
    if meta is not None:
        if meta.cwd:
            meta.project = Path(meta.cwd).name
        meta.started_at = _min_ts
        meta.ended_at = _max_ts

    return events, last_good_offset, meta


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
                    obj = _loads(line)
                except _JSONDecodeError:
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
