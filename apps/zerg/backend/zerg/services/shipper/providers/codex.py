"""Codex CLI session provider.

Parses JSONL sessions from ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Codex stores sessions in JSONL format with types:
- session_meta: Session initialization metadata
- response_item: Messages (user/assistant/developer), function calls, tool outputs
- turn_context: Per-turn metadata (cwd, model)
- event_msg: Token usage events (skipped)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterator

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import ParsedSession
from zerg.services.shipper.providers import registry

logger = logging.getLogger(__name__)


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse ISO timestamp, handling Z suffix."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _extract_content_text(content: list) -> str | None:
    """Extract text from Codex content blocks.

    User messages use input_text, assistant messages use output_text.
    """
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype in ("input_text", "output_text"):
            text = block.get("text", "")
            if text.strip():
                parts.append(text)
    return "\n".join(parts) if parts else None


def _parse_tool_output(raw_output: str) -> str | None:
    """Parse double-JSON-encoded tool output.

    Codex encodes function_call_output as: JSON string -> {"output": "actual text"}
    """
    if not raw_output:
        return None
    try:
        parsed = json.loads(raw_output)
        if isinstance(parsed, dict):
            output = parsed.get("output", raw_output)
            return str(output) if not isinstance(output, str) else output
        return str(parsed)
    except (json.JSONDecodeError, TypeError):
        return raw_output


class CodexProvider:
    """Provider for Codex CLI sessions (~/.codex/sessions/)."""

    name = "codex"

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or (Path.home() / ".codex")

    @property
    def sessions_dir(self) -> Path:
        return self.config_dir / "sessions"

    def discover_files(self) -> list[Path]:
        """Find all JSONL session files, newest first."""
        if not self.sessions_dir.exists():
            return []
        files = list(self.sessions_dir.glob("**/*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files

    def parse_file(self, path: Path, offset: int = 0) -> Iterator[ParsedEvent]:
        """Parse Codex JSONL session file from byte offset."""
        # We need session_id from session_meta, which is typically line 1.
        # If offset > 0, we may have already read it. Use filename as fallback.
        session_id = self._extract_session_id_from_filename(path)

        try:
            with open(path, "rb") as f:
                # If offset is 0, try to grab session_id from first line
                if offset == 0:
                    first_line = f.readline()
                    if first_line:
                        try:
                            obj = json.loads(first_line.decode("utf-8", errors="replace"))
                            if obj.get("type") == "session_meta":
                                sid = obj.get("payload", {}).get("id")
                                if sid:
                                    session_id = sid
                        except json.JSONDecodeError:
                            pass
                        # Reset to process from beginning
                        f.seek(0)

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
                        obj = json.loads(line_text)
                    except json.JSONDecodeError:
                        logger.debug(
                            "Codex: bad JSON at offset %d in %s",
                            line_offset,
                            path.name,
                        )
                        continue

                    entry_type = obj.get("type", "")
                    timestamp = _parse_ts(obj.get("timestamp"))
                    if not timestamp:
                        timestamp = datetime.now(timezone.utc)

                    # Skip non-content types
                    if entry_type in ("session_meta", "turn_context", "event_msg"):
                        continue

                    if entry_type == "response_item":
                        yield from self._parse_response_item(
                            obj.get("payload", {}),
                            session_id=session_id,
                            timestamp=timestamp,
                            offset=line_offset,
                            raw_line=line_text,
                        )

        except Exception as e:
            logger.exception("Error parsing Codex session %s: %s", path, e)

    def _parse_response_item(
        self,
        payload: dict,
        session_id: str,
        timestamp: datetime,
        offset: int,
        raw_line: str,
    ) -> Iterator[ParsedEvent]:
        """Parse a single response_item payload."""
        ptype = payload.get("type", "")

        if ptype == "message":
            role = payload.get("role", "")
            content = payload.get("content", [])

            # Skip developer role (system instructions)
            if role == "developer":
                return

            if not isinstance(content, list):
                return

            text = _extract_content_text(content)
            if text:
                normalized_role = "assistant" if role == "assistant" else "user"
                yield ParsedEvent(
                    uuid=f"{session_id}-msg-{offset}",
                    session_id=session_id,
                    timestamp=timestamp,
                    role=normalized_role,
                    content_text=text,
                    source_offset=offset,
                    raw_type=f"codex-{role}",
                    raw_line=raw_line,
                )

        elif ptype == "function_call":
            name = payload.get("name", "")
            args_str = payload.get("arguments", "{}")
            call_id = payload.get("call_id", "")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {"raw": args_str}

            yield ParsedEvent(
                uuid=f"{session_id}-call-{call_id or offset}",
                session_id=session_id,
                timestamp=timestamp,
                role="assistant",
                tool_name=name,
                tool_input_json=args if isinstance(args, dict) else None,
                source_offset=offset,
                raw_type="codex-function_call",
                raw_line=raw_line,
            )

        elif ptype == "function_call_output":
            raw_output = payload.get("output", "")
            call_id = payload.get("call_id", "")
            output_text = _parse_tool_output(raw_output)

            if output_text:
                yield ParsedEvent(
                    uuid=f"{session_id}-result-{call_id or offset}",
                    session_id=session_id,
                    timestamp=timestamp,
                    role="tool",
                    tool_output_text=output_text,
                    source_offset=offset,
                    raw_type="codex-function_call_output",
                    raw_line=raw_line,
                )

        # Skip reasoning type silently
        elif ptype == "reasoning":
            return

    def extract_metadata(self, path: Path) -> ParsedSession:
        """Extract session metadata from Codex JSONL file."""
        session_id = self._extract_session_id_from_filename(path)
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

                    entry_type = obj.get("type", "")
                    ts = _parse_ts(obj.get("timestamp"))
                    if ts:
                        timestamps.append(ts)

                    if entry_type == "session_meta":
                        payload = obj.get("payload", {})
                        sid = payload.get("id")
                        if sid:
                            result.session_id = sid
                        if not result.cwd:
                            result.cwd = payload.get("cwd")
                        if not result.version:
                            result.version = payload.get("cli_version")
                        git = payload.get("git", {})
                        if git and not result.git_branch:
                            result.git_branch = git.get("branch")

                    elif entry_type == "turn_context":
                        payload = obj.get("payload", {})
                        if not result.cwd:
                            result.cwd = payload.get("cwd")

            if result.cwd:
                result.project = Path(result.cwd).name
            if timestamps:
                result.started_at = min(timestamps)
                result.ended_at = max(timestamps)

        except Exception as e:
            logger.warning("Error extracting Codex metadata from %s: %s", path, e)

        return result

    @staticmethod
    def _extract_session_id_from_filename(path: Path) -> str:
        """Extract UUID from Codex filename.

        Filename format: rollout-YYYY-MM-DDThh-mm-ss-UUID.jsonl
        e.g. rollout-2026-02-03T15-35-56-019c2538-0c3d-7f23-8743-18c6fbf5dd9c
        """
        stem = path.stem
        # Try to find UUID portion after the timestamp prefix
        # Split: rollout, YYYY, MM, DDThh, mm, ss, UUID...
        parts = stem.split("-", 7)
        if len(parts) >= 7:
            return parts[6]
        return stem


# Auto-register
registry.register(CodexProvider())
