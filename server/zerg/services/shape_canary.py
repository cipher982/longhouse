"""Live canary for transcript record shapes the catalog has not classified.

Provider updates add record types without telling anyone. The census
(`scripts/qa/transcript_census.py`) fingerprints a record as a shape such as
`system/turn_duration` and checks it against `schemas/transcript_shapes/`, but
only runs over fixtures and nightly pins. This module runs the same check on
every JSONL envelope the host ingests, so a shape that David's real usage
produces and the catalog does not know shows up within the hour.

Only shape names, provider and CLI version are kept. Record bodies never leave
the parse worker. The table lives in this process and is empty after a restart;
the next envelope that carries the shape fills it again.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections import OrderedDict
from datetime import UTC
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Providers whose byte_offset envelopes carry one JSON object per record.
JSONL_PROVIDERS = frozenset({"claude", "codex", "pi", "omp"})

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# Source checkout, runtime image (docker/runtime.dockerfile), pip wheel.
_CENSUS_CANDIDATES = (
    _ROOT / "scripts" / "qa" / "transcript_census.py",
    Path("/schemas/transcript_census.py"),
    _PACKAGE_ROOT / "_config" / "transcript_census.py",
)
_CATALOG_CANDIDATES = (
    _ROOT / "schemas" / "transcript_shapes",
    Path("/schemas/transcript_shapes"),
    _PACKAGE_ROOT / "_config" / "transcript_shapes",
)

MAX_SHAPES = 200
MAX_SHAPE_CHARS = 160
MAX_TRACKED_SESSIONS = 2048


@cache
def _census() -> Any | None:
    """The census module, or None when this install does not ship it."""
    path = next((p for p in _CENSUS_CANDIDATES if p.is_file()), None)
    if path is None:
        logger.warning("shape canary disabled: transcript_census.py not found")
        return None
    spec = importlib.util.spec_from_file_location("_lh_transcript_census", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@cache
def _catalog(provider: str) -> dict[str, Any] | None:
    import json

    directory = next((p for p in _CATALOG_CANDIDATES if p.is_dir()), None)
    if directory is None or not (directory / f"{provider}.json").is_file():
        return None
    return json.loads((directory / f"{provider}.json").read_text(encoding="utf-8"))


def available() -> bool:
    """Whether this process can classify shapes at all."""
    return _census() is not None and all(_catalog(p) is not None for p in sorted(JSONL_PROVIDERS))


def observe_records(provider: str, range_kind: str, records: Any) -> dict[str, Any] | None:
    """Classify one envelope's records. Runs in the parse worker.

    Returns `{"version": last CLI version seen, "unclassified": {shape: lines}}`,
    or None when the envelope is not a JSONL transcript or the canary is off.
    """
    if provider not in JSONL_PROVIDERS or range_kind != "byte_offset":
        return None
    census = _census()
    catalog = _catalog(provider)
    if census is None or catalog is None:
        return None
    import json

    shaper = census.SHAPERS[provider]
    version: str | None = None
    unclassified: dict[str, int] = {}
    for record in records:
        try:
            entry = json.loads(record.data)
        except ValueError:
            entry = None
        if isinstance(entry, dict):
            shape, _keys, seen_version, _entrypoint = shaper(entry)
            version = seen_version or version
        else:
            shape = "<unparseable>"
        if shape in unclassified:
            unclassified[shape] += 1
            continue
        row = census.catalog_row(catalog, shape)
        status = str((row or {}).get("classification") or "").strip()
        if status and status != "unclassified":
            continue
        unclassified[shape] = 1
    return {"version": version, "unclassified": unclassified}


class ShapeCanary:
    """Per (provider, version, shape) counts of unclassified records."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._versions: OrderedDict[str, str] = OrderedDict()
        self.overflowed = 0

    def record(self, provider: str, session_id: str, observation: dict[str, Any] | None) -> None:
        if not observation:
            return
        version = observation.get("version")
        if version:
            self._versions[session_id] = version
            self._versions.move_to_end(session_id)
            while len(self._versions) > MAX_TRACKED_SESSIONS:
                self._versions.popitem(last=False)
        version = version or self._versions.get(session_id) or "unknown"
        now = datetime.now(UTC)
        for shape, lines in observation.get("unclassified", {}).items():
            key = (provider, version, shape[:MAX_SHAPE_CHARS])
            row = self._rows.get(key)
            if row is None:
                if len(self._rows) >= MAX_SHAPES:
                    self.overflowed += lines
                    continue
                row = self._rows[key] = {"count": 0, "first_seen_at": now}
            row["count"] += lines
            row["last_seen_at"] = now

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"provider": provider, "cli_version": version, "shape": shape, **row}
            for (provider, version, shape), row in sorted(self._rows.items())
        ]


canary = ShapeCanary()
