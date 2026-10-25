"""Bounded render-object hydration for reference-only search rows."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.services.raw_object_workers import storage_v2_root
from zerg.storage_v2.render_objects import DecodedRenderObject
from zerg.storage_v2.render_objects import RenderObjectCorruptError
from zerg.storage_v2.render_objects import read_render_object


@dataclass(frozen=True, slots=True)
class HydrationResult:
    rows: list[dict[str, Any]]
    complete: bool


class RenderHydrator:
    def __init__(self, *, root: Path | None = None, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.root = (root or storage_v2_root()).expanduser().resolve()
        self.max_bytes = max_bytes
        self._cache: OrderedDict[str, tuple[DecodedRenderObject, int]] = OrderedDict()
        self._cache_bytes = 0

    def hydrate(
        self,
        rows: list[dict[str, Any]],
        *,
        byte_budget: int,
        per_row_byte_cap: int | None = None,
    ) -> HydrationResult:
        hydrated: list[dict[str, Any]] = []
        used = 0
        complete = True
        for row in rows:
            try:
                decoded = self._read(str(row["source_object_id"]))
                record = decoded.spec.records[int(row["record_ordinal"])]
            except (RenderObjectCorruptError, OSError, IndexError, ValueError):
                hydrated.append(
                    {
                        **row,
                        "content_text": None,
                        "tool_input_text": None,
                        "tool_output_text": None,
                        "hydration_gap": "render_object_unavailable",
                    }
                )
                complete = False
                continue
            values = {
                "content_text": record.content_text,
                "tool_input_text": format_tool_input_text(record.tool_input_json),
                "tool_output_text": record.tool_output_text,
                "tool_name": record.tool_name,
            }
            if per_row_byte_cap is not None:
                values, full_bytes = _truncate_values(values, per_row_byte_cap)
                if full_bytes is not None:
                    values["content_text_full_bytes"] = full_bytes
            size = sum(
                len(value.encode())
                for key, value in values.items()
                if key in ("content_text", "tool_output_text") and isinstance(value, str)
            )
            if hydrated and used + size > byte_budget:
                return HydrationResult(rows=hydrated, complete=False)
            hydrated.append({**row, **values})
            used += size
        return HydrationResult(rows=hydrated, complete=complete)

    def _read(self, digest: str) -> DecodedRenderObject:
        cached = self._cache.pop(digest, None)
        if cached is not None:
            self._cache[digest] = cached
            return cached[0]
        decoded = read_render_object(self.root, f"render/v2/{digest[:2]}/{digest}.zst", expected_object_hash=digest)
        size = sum(
            len(value.encode())
            for record in decoded.spec.records
            for value in (record.content_text, record.tool_output_text)
            if isinstance(value, str)
        )
        self._cache[digest] = (decoded, size)
        self._cache_bytes += size
        while self._cache_bytes > self.max_bytes:
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= removed
        return decoded


def format_tool_input_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("command", "cmd", "query", "path", "url", "urls", "pattern"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, list):
                joined = " ".join(str(x).strip() for x in item if str(x).strip())
                if joined:
                    parts.append(joined)
        if parts:
            return "\n".join(parts)[:16_384]
    try:
        dumped = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return dumped[:16_384] if dumped else None
    except (TypeError, ValueError):
        return None


_format_tool_input_text = format_tool_input_text


def _truncate_values(values: dict[str, Any], limit: int) -> tuple[dict[str, Any], int | None]:
    """Cap one hydrated turn without splitting a UTF-8 character."""

    remaining = limit
    truncated = dict(values)
    content_full_bytes: int | None = None
    for key in ("content_text", "tool_output_text"):
        value = truncated[key]
        if not isinstance(value, str):
            continue
        encoded = value.encode()
        if len(encoded) > remaining:
            if key == "content_text":
                content_full_bytes = len(encoded)
            truncated[key] = encoded[:remaining].decode("utf-8", "ignore")
        remaining -= min(len(encoded), remaining)
    return truncated, content_full_bytes
