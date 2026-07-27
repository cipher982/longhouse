from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from typing import Mapping

MEASUREMENT_SCHEMA_VERSION = 1
MEASUREMENT_FILENAME = "measurement.json"
_DISCRIMINATOR_KEYS = frozenset(
    {
        "action",
        "artifact_kind",
        "event_type",
        "failure_code",
        "kind",
        "method",
        "operation",
        "provider",
        "role",
        "scenario",
        "status",
        "tool_name",
        "type",
    }
)
_VOLATILE_METRIC_PARTS = ("duration", "elapsed", "latency", "memory", "rss", "timing")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: Any, *, key: str | None, package_roots: tuple[str, ...]) -> Any:
    normalized_key = (key or "").lower()
    if normalized_key.endswith("_at") or "timestamp" in normalized_key:
        return "$TIMESTAMP"
    if any(part in normalized_key for part in _VOLATILE_METRIC_PARTS):
        return "$VOLATILE_METRIC"
    if isinstance(value, Mapping):
        return {
            str(child_key): _canonical_value(child, key=str(child_key), package_roots=package_roots)
            for child_key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        return [_canonical_value(item, key=key, package_roots=package_roots) for item in value]
    if isinstance(value, str):
        normalized = value
        for root in package_roots:
            normalized = normalized.replace(root, "$PACKAGE_ROOT")
        return normalized
    return value


def canonical_digest_v1(value: Any, *, package_root: Path) -> str:
    """Measure content after removing package-root and clock noise.

    Provider IDs, transcript values, and canary markers deliberately remain.
    The digest is recorded for measurement only and cannot authorize skipping.
    """

    roots = tuple(dict.fromkeys((str(package_root), str(package_root.resolve()))))
    return _digest(_canonical_value(value, key=None, package_roots=roots))


def _structural_value(value: Any, *, key: str | None) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return {"string": value} if key in _DISCRIMINATOR_KEYS else "string"
    if isinstance(value, list):
        return {"array": [_structural_value(item, key=key) for item in value]}
    if isinstance(value, Mapping):
        return {
            str(child_key): _structural_value(child, key=str(child_key))
            for child_key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return type(value).__name__


def structural_fingerprint_v1(value: Any) -> str:
    """Measure value-normalized evidence shape while retaining discriminators."""

    return _digest(_structural_value(value, key=None))


def _load_measurement_inputs(package_root: Path) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for path in sorted(package_root.rglob("*"), key=lambda item: item.relative_to(package_root).as_posix()):
        if not path.is_file() or path.name == MEASUREMENT_FILENAME:
            continue
        relative = path.relative_to(package_root).as_posix()
        try:
            if path.suffix == ".json":
                inputs[relative] = json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix == ".jsonl":
                inputs[relative] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            inputs[relative] = {"measurement_error": type(exc).__name__}
    return inputs


def measure_evidence_package(package_root: Path) -> dict[str, Any]:
    inputs = _load_measurement_inputs(package_root)
    return {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "artifact_kind": "provider_evidence_hash_stability_measurement",
        "decision_role": "observation_only",
        "skip_authority": False,
        "input_files": sorted(inputs),
        "canonical_digest_v1": canonical_digest_v1(inputs, package_root=package_root),
        "structural_fingerprint_v1": structural_fingerprint_v1(inputs),
    }
