"""Claude Code session provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import ParsedSession
from zerg.services.shipper.parser import extract_session_metadata
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.providers import registry


class ClaudeProvider:
    """Provider for Claude Code sessions (~/.claude/projects/)."""

    name = "claude"

    def __init__(self, config_dir: Path | None = None) -> None:
        if config_dir is None:
            env_dir = os.getenv("CLAUDE_CONFIG_DIR")
            config_dir = Path(env_dir) if env_dir else Path.home() / ".claude"
        self.config_dir = config_dir

    @property
    def projects_dir(self) -> Path:
        return self.config_dir / "projects"

    def discover_files(self) -> list[Path]:
        if not self.projects_dir.exists():
            return []
        files = list(self.projects_dir.glob("**/*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files

    def parse_file(self, path: Path, offset: int = 0) -> Iterator[ParsedEvent]:
        yield from parse_session_file(path, offset=offset)

    def extract_metadata(self, path: Path) -> ParsedSession:
        return extract_session_metadata(path)


# Auto-register
registry.register(ClaudeProvider())
