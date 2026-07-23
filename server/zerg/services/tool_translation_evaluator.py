"""Read-only accounting replay for provider tool-call translation.

The evaluator consumes a privacy-safe manifest of provider-shaped source records
and their canonical event projection. It never executes transcript content and
never emits raw payload values in its report.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES_PATH = REPO_ROOT / "config" / "tool-tiers.json"


class TranslationEvaluationError(ValueError):
    """The corpus or rules contract is invalid."""


def _shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return {"array": sorted({_shape_token(item) for item in value})}
    if isinstance(value, dict):
        return {str(key): _shape(item) for key, item in sorted(value.items())}
    return type(value).__name__


def _shape_token(value: Any) -> str:
    return json.dumps(_shape(value), sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    encoded = _shape_token(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _stable_identity(source_id: str, source_event_id: str, event_index: int) -> str:
    value = f"{source_id}\0{source_event_id}\0{event_index}".encode()
    return hashlib.sha256(value).hexdigest()[:24]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TranslationEvaluationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TranslationEvaluationError(f"expected JSON object in {path}")
    return value


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise TranslationEvaluationError(f"cannot read corpus source {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TranslationEvaluationError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise TranslationEvaluationError(f"expected object at {path}:{line_number}")
        records.append(value)
    return records


def _rule_lookup(rules: dict[str, Any]) -> set[str]:
    return {str(name).lower() for name in (rules.get("tools") or {})}


def _is_exact(tool_name: str, native: set[str]) -> bool:
    return tool_name.lower() in native or tool_name.startswith("mcp__")


def evaluate_manifest(manifest_path: Path, *, rules_path: Path = DEFAULT_RULES_PATH) -> dict[str, Any]:
    """Evaluate one manifest without mutating its sources or repository state."""

    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path)
    if manifest.get("version") != 1:
        raise TranslationEvaluationError("manifest version must be 1")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise TranslationEvaluationError("manifest must contain at least one source")

    rules = _load_json(rules_path.resolve())
    native_tools = _rule_lookup(rules)
    rules_hash = hashlib.sha256(rules_path.resolve().read_bytes()).hexdigest()[:16]

    totals: Counter[str] = Counter()
    by_provider: dict[str, Counter[str]] = defaultdict(Counter)
    schema_fingerprints: dict[str, Counter[str]] = defaultdict(Counter)
    unknowns: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    call_slots: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    result_slots: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    canonical_ids: set[str] = set()
    stable_identities: list[str] = []
    errors: list[str] = []
    consequence_slices: Counter[str] = Counter()

    for source in sources:
        if not isinstance(source, dict):
            raise TranslationEvaluationError("each manifest source must be an object")
        source_id = str(source.get("id") or "").strip()
        provider = str(source.get("provider") or "").strip().lower()
        wire_family = str(source.get("wire_family") or "").strip()
        relative_path = str(source.get("path") or "").strip()
        if not source_id or not provider or not wire_family or not relative_path:
            raise TranslationEvaluationError("source id, provider, wire_family, and path are required")
        records = _load_records((manifest_path.parent / relative_path).resolve())
        for record_index, record in enumerate(records):
            totals["source_events"] += 1
            by_provider[provider]["source_events"] += 1
            raw = record.get("raw")
            source_event_id = str(record.get("source_event_id") or "").strip()
            session_id = str(record.get("session_id") or "").strip()
            if raw is None or not source_event_id or not session_id:
                errors.append(f"{source_id}:{record_index}: missing raw trace, source_event_id, or session_id")
                totals["unattributed"] += 1
                continue
            schema_fingerprints[f"{provider}/{wire_family}"][_fingerprint(raw)] += 1
            canonical = record.get("canonical")
            if not isinstance(canonical, list) or not canonical:
                totals["lost"] += 1
                by_provider[provider]["lost"] += 1
                errors.append(f"{source_id}:{source_event_id}: source event has no canonical outcome")
                continue

            for event_index, event in enumerate(canonical):
                totals["canonical_events"] += 1
                by_provider[provider]["canonical_events"] += 1
                stable_identities.append(_stable_identity(source_id, source_event_id, event_index))
                if not isinstance(event, dict):
                    totals["unattributed"] += 1
                    errors.append(f"{source_id}:{source_event_id}:{event_index}: canonical event is not an object")
                    continue
                event_id = str(event.get("event_id") or "").strip()
                if not event_id:
                    totals["unattributed"] += 1
                    errors.append(f"{source_id}:{source_event_id}:{event_index}: missing event_id")
                elif event_id in canonical_ids:
                    totals["duplicated"] += 1
                    errors.append(f"duplicate canonical event_id: {event_id}")
                else:
                    canonical_ids.add(event_id)

                role = str(event.get("role") or "")
                tool_name = str(event.get("tool_name") or "").strip()
                call_id = str(event.get("tool_call_id") or "").strip()
                if role == "tool":
                    totals["results"] += 1
                    by_provider[provider]["results"] += 1
                    if event.get("failed") is True:
                        consequence_slices["failure"] += 1
                    if call_id:
                        result_slots[(provider, session_id, call_id)].append(event_id)
                    else:
                        totals["orphan_results"] += 1
                    continue
                if tool_name:
                    totals["outer_calls"] += 1
                    totals["logical_operations"] += 1
                    by_provider[provider]["outer_calls"] += 1
                    consequence = str(event.get("consequence") or "unknown")
                    consequence_slices[consequence] += 1
                    if call_id:
                        call_slots[(provider, session_id, call_id)].append(event_id)
                    else:
                        totals["orphan_calls"] += 1
                    if _is_exact(tool_name, native_tools):
                        totals["exact"] += 1
                        by_provider[provider]["exact"] += 1
                    else:
                        totals["unknown"] += 1
                        by_provider[provider]["unknown"] += 1
                        signature = _shape_token(event.get("tool_input_json"))
                        unknown = unknowns[(provider, tool_name, signature)]
                        unknown["count"] += 1
                        unknown["with_result_id"] += int(bool(call_id))
                else:
                    totals["other_events"] += 1
                    by_provider[provider]["other_events"] += 1

    for key in sorted(set(call_slots) | set(result_slots)):
        calls = call_slots.get(key, [])
        results = result_slots.get(key, [])
        if len(calls) > 1:
            totals["duplicate_call_ids"] += len(calls) - 1
            errors.append(f"duplicate call id for {key[0]}/{key[1]}: {key[2]}")
        if len(results) > 1:
            totals["duplicate_result_ids"] += len(results) - 1
            errors.append(f"multiple results for {key[0]}/{key[1]}: {key[2]}")
        if calls and results:
            totals["paired"] += 1
        elif calls:
            totals["orphan_calls"] += len(calls)
        else:
            totals["orphan_results"] += len(results)

    identity_digest = hashlib.sha256("\n".join(stable_identities).encode()).hexdigest()
    totals["visible_rows"] = totals["outer_calls"] + totals["other_events"] + totals["orphan_results"]
    for metric in (
        "source_events",
        "canonical_events",
        "outer_calls",
        "results",
        "logical_operations",
        "paired",
        "exact",
        "unknown",
        "lost",
        "duplicated",
        "unattributed",
        "orphan_calls",
        "orphan_results",
        "duplicate_call_ids",
        "duplicate_result_ids",
        "inferred_children",
        "blocked_recession",
        "visible_rows",
    ):
        totals[metric] += 0
    expected = manifest.get("expected") or {}
    if not isinstance(expected, dict):
        raise TranslationEvaluationError("manifest expected must be an object")
    for metric, value in expected.items():
        if totals[str(metric)] != int(value):
            errors.append(f"expected {metric}={value}, observed {totals[str(metric)]}")
    unknown_report = [
        {
            "provider": provider,
            "tool_name": tool_name,
            "input_shape": json.loads(signature),
            "count": counts["count"],
            "with_result_id": counts["with_result_id"],
        }
        for (provider, tool_name, signature), counts in sorted(unknowns.items(), key=lambda item: (-item[1]["count"], item[0]))
    ]
    return {
        "schema_version": 1,
        "manifest_id": str(manifest.get("id") or manifest_path.stem),
        "rules_fingerprint": rules_hash,
        "stable_identity_digest": identity_digest,
        "passed": not errors,
        "totals": dict(sorted(totals.items())),
        "providers": {provider: dict(sorted(counts.items())) for provider, counts in sorted(by_provider.items())},
        "schema_fingerprints": {family: dict(sorted(counts.items())) for family, counts in sorted(schema_fingerprints.items())},
        "consequence_slices": dict(sorted(consequence_slices.items())),
        "unknowns": unknown_report,
        "errors": errors,
    }
