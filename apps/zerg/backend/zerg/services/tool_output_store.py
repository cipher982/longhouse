"""Tool Output Store – persistence for large supervisor tool outputs.

Stores large tool outputs on disk and returns lightweight markers for LLM context.
Each artifact is scoped to an owner_id for access control.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from zerg.config import get_settings

logger = logging.getLogger(__name__)


class ToolOutputStore:
    """Manages filesystem storage for supervisor tool outputs."""

    def __init__(self, base_path: str | None = None):
        if base_path:
            self.base_path = Path(base_path)
        else:
            self.base_path = get_settings().data_dir / "tool_outputs"

        self.base_path.mkdir(parents=True, exist_ok=True)

    def _owner_dir(self, owner_id: int, *, create: bool) -> Path:
        path = self.base_path / str(owner_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _validate_artifact_id(self, artifact_id: str) -> None:
        if not artifact_id or "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
            raise ValueError(f"Invalid artifact_id: {artifact_id}")

    def save_output(
        self,
        *,
        owner_id: int,
        tool_name: str,
        content: str,
        run_id: int | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        """Persist tool output and return artifact_id."""
        artifact_id = uuid.uuid4().hex
        owner_dir = self._owner_dir(owner_id, create=True)

        content_path = owner_dir / f"{artifact_id}.txt"
        meta_path = owner_dir / f"{artifact_id}.json"

        content_path.write_text(content, encoding="utf-8")

        metadata: dict[str, Any] = {
            "artifact_id": artifact_id,
            "owner_id": owner_id,
            "tool_name": tool_name,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(content.encode("utf-8")),
        }
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        logger.debug("Stored tool output %s for owner %s (%s bytes)", artifact_id, owner_id, metadata["size_bytes"])
        return artifact_id

    def read_output(self, *, owner_id: int, artifact_id: str) -> str:
        """Read tool output by artifact_id for an owner."""
        self._validate_artifact_id(artifact_id)
        owner_dir = self._owner_dir(owner_id, create=False)
        content_path = owner_dir / f"{artifact_id}.txt"

        if not content_path.exists():
            raise FileNotFoundError(f"Tool output not found for artifact_id={artifact_id}")

        return content_path.read_text(encoding="utf-8")

    def read_metadata(self, *, owner_id: int, artifact_id: str) -> dict[str, Any]:
        """Read metadata for an artifact (best-effort)."""
        self._validate_artifact_id(artifact_id)
        owner_dir = self._owner_dir(owner_id, create=False)
        meta_path = owner_dir / f"{artifact_id}.json"

        if not meta_path.exists():
            raise FileNotFoundError(f"Tool output metadata not found for artifact_id={artifact_id}")

        return json.loads(meta_path.read_text(encoding="utf-8"))


__all__ = ["ToolOutputStore"]
