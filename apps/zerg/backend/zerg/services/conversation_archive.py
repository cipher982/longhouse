"""Filesystem archive helpers for conversation artifacts."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from zerg.config import get_settings

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str | None, *, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback
    safe = _SAFE_NAME_RE.sub("-", raw).strip("-._")
    return safe or fallback


class ConversationArchiveStore:
    """Persist raw conversation artifacts to disk."""

    def __init__(self, base_path: str | None = None):
        if base_path:
            self.base_path = Path(base_path)
        else:
            self.base_path = get_settings().data_dir / "conversations"
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _conversation_dir(self, *, owner_id: int, conversation_id: int, create: bool) -> Path:
        path = self.base_path / str(owner_id) / str(conversation_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def save_email_raw(
        self,
        *,
        owner_id: int,
        conversation_id: int,
        external_message_id: str | None,
        raw_bytes: bytes,
        extension: str = "eml",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        conversation_dir = self._conversation_dir(
            owner_id=owner_id,
            conversation_id=conversation_id,
            create=True,
        )
        raw_dir = conversation_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        artifact_name = _safe_name(
            external_message_id,
            fallback=uuid.uuid4().hex,
        )
        suffix = extension.lstrip(".") or "eml"
        content_path = raw_dir / f"{artifact_name}.{suffix}"
        content_path.write_bytes(raw_bytes)

        if metadata:
            meta_path = raw_dir / f"{artifact_name}.json"
            meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

        return str(content_path.relative_to(self.base_path))

    def resolve_path(self, relpath: str) -> Path:
        return self.base_path / relpath
