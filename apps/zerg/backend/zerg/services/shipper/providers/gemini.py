"""Gemini CLI session provider.

Parses JSON sessions from ~/.gemini/tmp/<hash>/chats/session-*.json

Gemini stores sessions as single JSON files with a messages array.
Each message has a type (user/gemini/info) and optional toolCalls.
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterator

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import ParsedSession
from zerg.services.shipper.providers import registry

logger = logging.getLogger(__name__)


def _parse_ts(ts: object) -> datetime | None:
    """Parse timestamp — handles ISO string and Unix epoch integer."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(ts, str):
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None
    return None


def _normalize_role(type_or_role: str) -> str:
    """Normalize Gemini type/role to standard roles."""
    lower = type_or_role.lower()
    if lower in ("user",):
        return "user"
    if lower in ("gemini", "assistant", "model"):
        return "assistant"
    if lower in ("info", "system"):
        return "system"
    if lower == "tool":
        return "tool"
    return "user"


class GeminiProvider:
    """Provider for Gemini CLI sessions (~/.gemini/tmp/)."""

    name = "gemini"

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or (Path.home() / ".gemini")

    @property
    def tmp_dir(self) -> Path:
        return self.config_dir / "tmp"

    def discover_files(self) -> list[Path]:
        """Find all Gemini session JSON files, newest first."""
        if not self.tmp_dir.exists():
            return []

        files: list[Path] = []

        # Walk all hash directories
        try:
            for hash_dir in self.tmp_dir.iterdir():
                if not hash_dir.is_dir():
                    continue
                # Validate it looks like a hex hash (32-64 chars)
                if not all(c in "0123456789abcdef" for c in hash_dir.name):
                    continue
                if len(hash_dir.name) < 32:
                    continue

                # Check chats/ subdirectory (standard path)
                chats_dir = hash_dir / "chats"
                if chats_dir.exists():
                    files.extend(chats_dir.glob("session-*.json"))

                # Fallback: session files directly in hash dir
                files.extend(hash_dir.glob("session-*.json"))
        except OSError as e:
            logger.warning("Error scanning Gemini tmp dir: %s", e)

        # Deduplicate (in case both patterns match same file)
        seen: set[Path] = set()
        unique: list[Path] = []
        for f in files:
            resolved = f.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(f)

        # Sort by mtime, newest first
        unique.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return unique

    def parse_file(self, path: Path, offset: int = 0) -> Iterator[ParsedEvent]:
        """Parse Gemini JSON session file.

        Note: offset is ignored for JSON files (always reads full file).
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to parse Gemini session %s: %s", path, e)
            return

        # Extract messages from various JSON shapes
        messages = self._extract_messages(data)
        session_id = self._extract_session_id(data, path)

        for msg in messages:
            yield from self._parse_message(msg, session_id)

    def _extract_messages(self, data: object) -> list[dict]:
        """Extract messages array from various Gemini JSON shapes."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "messages" in data and isinstance(data["messages"], list):
                return data["messages"]
            if "history" in data and isinstance(data["history"], list):
                return data["history"]
        return []

    def _extract_session_id(self, data: object, path: Path) -> str:
        """Extract session ID from data or derive from filename."""
        if isinstance(data, dict):
            sid = data.get("sessionId")
            if sid:
                return sid
        return path.stem  # session-2026-01-08T21-12-d3483e9f -> use as ID

    def _parse_message(self, msg: dict, session_id: str) -> Iterator[ParsedEvent]:
        """Parse a single Gemini message into events."""
        if not isinstance(msg, dict):
            return

        # Get role from type or role field
        msg_type = msg.get("type", msg.get("role", ""))
        if not msg_type:
            return

        role = _normalize_role(msg_type)
        timestamp = _parse_ts(msg.get("timestamp"))
        if not timestamp:
            timestamp = datetime.now(timezone.utc)

        msg_id = msg.get("id", str(uuid_mod.uuid4()))
        content = msg.get("content", "")

        # Skip info/system messages
        if role == "system":
            return

        # Emit main content event
        if content and isinstance(content, str) and content.strip():
            yield ParsedEvent(
                uuid=f"{session_id}-{msg_id}",
                session_id=session_id,
                timestamp=timestamp,
                role=role,
                content_text=content,
                source_offset=0,
                raw_type=f"gemini-{msg_type}",
                raw_line=json.dumps(msg),
            )

        # Emit tool call events (embedded in gemini messages)
        tool_calls = msg.get("toolCalls", [])
        if not isinstance(tool_calls, list):
            return

        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue

            tc_id = tc.get("id", str(uuid_mod.uuid4()))
            tc_name = tc.get("name", "")
            tc_args = tc.get("args", {})
            tc_timestamp = _parse_ts(tc.get("timestamp")) or timestamp

            # Tool call event
            if tc_name:
                yield ParsedEvent(
                    uuid=f"{session_id}-call-{tc_id}",
                    session_id=session_id,
                    timestamp=tc_timestamp,
                    role="assistant",
                    tool_name=tc.get("displayName") or tc_name,
                    tool_input_json=(tc_args if isinstance(tc_args, dict) else None),
                    source_offset=0,
                    raw_type="gemini-tool_call",
                    raw_line=json.dumps(msg),
                )

            # Tool result event
            results = tc.get("result", [])
            if isinstance(results, list):
                for res in results:
                    if not isinstance(res, dict):
                        continue
                    func_resp = res.get("functionResponse", {})
                    response = func_resp.get("response", {})
                    output = response.get("output", "")
                    if output:
                        yield ParsedEvent(
                            uuid=f"{session_id}-result-{tc_id}",
                            session_id=session_id,
                            timestamp=tc_timestamp,
                            role="tool",
                            tool_output_text=(str(output) if not isinstance(output, str) else output),
                            source_offset=0,
                            raw_type="gemini-tool_result",
                            raw_line=json.dumps(msg),
                        )

    def extract_metadata(self, path: Path) -> ParsedSession:
        """Extract session metadata from Gemini JSON file."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read Gemini metadata from %s: %s", path, e)
            return ParsedSession(session_id=path.stem)

        session_id = self._extract_session_id(data, path)
        result = ParsedSession(session_id=session_id)

        if isinstance(data, dict):
            result.started_at = _parse_ts(data.get("startTime"))
            result.ended_at = _parse_ts(data.get("lastUpdated"))

            # Project hash is one-way — can't reverse to get project name.
            # Try to infer cwd from tool call args instead.
            project_hash = data.get("projectHash", "")
            if project_hash:
                cwd = self._infer_cwd_from_messages(self._extract_messages(data))
                if cwd:
                    result.cwd = cwd
                    result.project = Path(cwd).name

        # If no timestamps from top level, scan messages
        if not result.started_at or not result.ended_at:
            messages = self._extract_messages(data) if isinstance(data, dict) else data if isinstance(data, list) else []
            timestamps = []
            for msg in messages:
                ts = _parse_ts(msg.get("timestamp")) if isinstance(msg, dict) else None
                if ts:
                    timestamps.append(ts)
            if timestamps:
                if not result.started_at:
                    result.started_at = min(timestamps)
                if not result.ended_at:
                    result.ended_at = max(timestamps)

        return result

    @staticmethod
    def _infer_cwd_from_messages(messages: list[dict]) -> str | None:
        """Try to infer working directory from tool call arguments."""
        path_keys = (
            "cwd",
            "path",
            "file_path",
            "filePath",
            "directory",
            "root",
        )
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            for tc in msg.get("toolCalls", []):
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args", {})
                if not isinstance(args, dict):
                    continue
                for key in path_keys:
                    val = args.get(key)
                    if val and isinstance(val, str) and val.startswith("/"):
                        # Walk up to find a reasonable project root
                        p = Path(val)
                        for parent in [p, *list(p.parents)]:
                            if parent == Path("/"):
                                break
                            if (parent / ".git").exists():
                                return str(parent)
                        # No .git found — use parent if it looks like a file
                        if p.is_file() or not p.exists():
                            return str(p.parent)
                        return str(p)
        return None


# Auto-register
registry.register(GeminiProvider())
