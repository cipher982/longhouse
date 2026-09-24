"""Bounded render-object hydration for reference-only search rows."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zerg.services.raw_object_workers import storage_v2_root
from zerg.storage_v2.render_objects import DecodedRenderObject
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

    def hydrate(self, rows: list[dict[str, Any]], *, byte_budget: int) -> HydrationResult:
        hydrated: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            decoded = self._read(str(row["source_object_id"]))
            record = decoded.spec.records[int(row["record_ordinal"])]
            values = {"content_text": record.content_text, "tool_output_text": record.tool_output_text, "tool_name": record.tool_name}
            size = sum(len(value.encode()) for value in values.values() if isinstance(value, str))
            # A caller still needs a typed, inspectable result for one oversized
            # record; downstream response caps decide whether it can be emitted.
            if hydrated and used + size > byte_budget:
                return HydrationResult(rows=hydrated, complete=False)
            hydrated.append({**row, **values})
            used += size
        return HydrationResult(rows=hydrated, complete=True)

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
