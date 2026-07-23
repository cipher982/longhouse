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
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.tool_presentation import project_tool_presentation

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RULES_PATH = REPO_ROOT / "config" / "tool-tiers.json"
PROOF_PROFILES = frozenset({"hermetic", "staged_release", "privacy_safe_live_replay"})


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


def evaluate_manifest(
    manifest_path: Path,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
    profile: str = "hermetic",
) -> dict[str, Any]:
    """Evaluate one manifest without mutating its sources or repository state."""

    if profile not in PROOF_PROFILES:
        raise TranslationEvaluationError(f"unknown proof profile: {profile}")
    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path)
    if manifest.get("version") != 1:
        raise TranslationEvaluationError("manifest version must be 1")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise TranslationEvaluationError("manifest must contain at least one source")

    _load_json(rules_path.resolve())
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
        contract = contract_for_provider(provider)
        if contract is None:
            raise TranslationEvaluationError(f"{source_id}: provider has no managed contract: {provider}")
        if wire_family not in contract.wire_families:
            raise TranslationEvaluationError(f"{source_id}: wire family {wire_family!r} is not declared for {provider}")
        if contract.presentation_ruleset != "shared_tool_presentation":
            raise TranslationEvaluationError(f"{source_id}: unsupported presentation ruleset {contract.presentation_ruleset!r}")
        if profile not in contract.proof_profiles.values():
            raise TranslationEvaluationError(f"{source_id}: proof profile {profile!r} is not declared for {provider}")
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
                    by_provider[provider]["outer_calls"] += 1
                    consequence = str(event.get("consequence") or "unknown")
                    consequence_slices[consequence] += 1
                    if call_id:
                        call_slots[(provider, session_id, call_id)].append(event_id)
                    else:
                        totals["orphan_calls"] += 1
                    presentation = project_tool_presentation(
                        tool_name,
                        event.get("tool_input_json"),
                        provider=provider,
                        rules_path=rules_path.resolve(),
                    )
                    disposition = str((presentation or {}).get("disposition") or "unknown")
                    children = list((presentation or {}).get("children") or [])
                    totals[disposition] += 1
                    by_provider[provider][disposition] += 1
                    if children:
                        totals["logical_operations"] += len(children)
                        totals["inferred_children"] += len(children)
                        by_provider[provider]["inferred_children"] += len(children)
                        if not bool((presentation or {}).get("wrapper_recedes")):
                            totals["wrappers_retained"] += 1
                            by_provider[provider]["wrappers_retained"] += 1
                        for child in children:
                            child_disposition = str(child.get("disposition") or "unknown")
                            totals[f"child_{child_disposition}"] += 1
                            by_provider[provider][f"child_{child_disposition}"] += 1
                            if child_disposition == "unknown":
                                signature = _shape_token(child.get("tool_input_json"))
                                unknown = unknowns[(provider, str(child.get("tool_name") or "unknown"), signature)]
                                unknown["count"] += 1
                                unknown["with_result_id"] += 0
                    else:
                        totals["logical_operations"] += 1
                    if disposition == "unknown":
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
        "parsed",
        "generic",
        "unknown",
        "lost",
        "duplicated",
        "unattributed",
        "orphan_calls",
        "orphan_results",
        "duplicate_call_ids",
        "duplicate_result_ids",
        "inferred_children",
        "wrappers_retained",
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
    has_orphans = bool(totals["orphan_calls"] or totals["orphan_results"])
    transcript_verdict = "red" if errors else ("yellow" if totals["unknown"] or has_orphans else "green")
    return {
        "schema_version": 1,
        "profile": profile,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_id": str(manifest.get("id") or manifest_path.stem),
        "rules_fingerprint": rules_hash,
        "stable_identity_digest": identity_digest,
        "passed": not errors,
        "verdicts": {
            "control": {"verdict": "not_evaluated", "reason": "translation corpus has no live control scenario"},
            "transcript": {
                "verdict": transcript_verdict,
                "reasons": [
                    *(["conservation_or_attribution_failure"] if errors else []),
                    *(["unknown_shapes_present"] if totals["unknown"] else []),
                    *(["orphan_call_or_result"] if has_orphans else []),
                ],
            },
        },
        "totals": dict(sorted(totals.items())),
        "providers": {provider: dict(sorted(counts.items())) for provider, counts in sorted(by_provider.items())},
        "schema_fingerprints": {family: dict(sorted(counts.items())) for family, counts in sorted(schema_fingerprints.items())},
        "consequence_slices": dict(sorted(consequence_slices.items())),
        "unknowns": unknown_report,
        "reports": {
            "shape_unknown": {
                "schema_fingerprints": {family: dict(sorted(counts.items())) for family, counts in sorted(schema_fingerprints.items())},
                "unknowns": unknown_report,
            },
            "conservation": {
                key: totals[key]
                for key in (
                    "source_events",
                    "canonical_events",
                    "lost",
                    "duplicated",
                    "unattributed",
                    "paired",
                    "orphan_calls",
                    "orphan_results",
                    "duplicate_call_ids",
                    "duplicate_result_ids",
                )
            },
            "presentation": {
                "outer_calls": totals["outer_calls"],
                "logical_operations": totals["logical_operations"],
                "exact": totals["exact"],
                "parsed": totals["parsed"],
                "generic": totals["generic"],
                "unknown": totals["unknown"],
                "inferred_children": totals["inferred_children"],
                "wrappers_retained": totals["wrappers_retained"],
                "visible_rows": totals["visible_rows"],
            },
        },
        "factory_health": {
            "state": "synthetic",
            "reason": "hermetic_replay_not_discovery",
            "complete_window": False,
        },
        "errors": errors,
    }


def evaluate_codex_archive(
    archive_root: Path,
    *,
    rules_path: Path = DEFAULT_RULES_PATH,
    max_files: int = 500,
) -> dict[str, Any]:
    """Privacy-safe structural replay over native Codex JSONL archives."""

    archive_root = archive_root.expanduser().resolve()
    if not archive_root.is_dir():
        raise TranslationEvaluationError(f"Codex archive root is not a directory: {archive_root}")
    discovered_files = sorted(archive_root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    files = discovered_files[:max_files]
    selection_truncated = len(discovered_files) > len(files)
    totals: Counter[str] = Counter()
    fingerprints: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    unknown_tool_names: Counter[str] = Counter()
    releases: Counter[str] = Counter()
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    results: Counter[tuple[str, str]] = Counter()
    errors: list[str] = []
    affected_sessions: set[str] = set()

    for path in files:
        totals["archive_files"] += 1
        session_ref = hashlib.sha256(str(path.relative_to(archive_root)).encode()).hexdigest()[:16]
        try:
            lines = path.open(encoding="utf-8")
        except OSError:
            totals["unreadable_files"] += 1
            continue
        with lines:
            for line_number, line in enumerate(lines, 1):
                if not any(token in line for token in ("custom_tool_call", "function_call", "session_meta")):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    totals["malformed_records"] += 1
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    totals["malformed_records"] += 1
                    continue
                if record.get("type") == "session_meta":
                    releases[str(payload.get("cli_version") or "unknown")] += 1
                    continue
                payload_type = payload.get("type")
                call_id = str(payload.get("call_id") or "")
                if payload_type in {"custom_tool_call_output", "function_call_output"}:
                    totals["results"] += 1
                    if call_id:
                        results[(session_ref, call_id)] += 1
                    else:
                        totals["orphan_results"] += 1
                    continue
                if payload_type not in {"custom_tool_call", "function_call"}:
                    continue
                totals["outer_calls"] += 1
                affected_sessions.add(session_ref)
                fingerprints[_fingerprint(payload)] += 1
                tool_name = str(payload.get("name") or "unknown")
                tool_names[tool_name] += 1
                if call_id:
                    call_key = (session_ref, call_id)
                    if call_key in calls:
                        totals["duplicate_call_ids"] += 1
                    calls[call_key] = {"session": session_ref, "line": line_number}
                else:
                    totals["orphan_calls"] += 1
                presentation = project_tool_presentation(
                    tool_name,
                    payload.get("input") if payload_type == "custom_tool_call" else payload.get("arguments"),
                    provider="codex",
                    rules_path=rules_path,
                )
                disposition = str((presentation or {}).get("disposition") or "unknown")
                totals[disposition] += 1
                if disposition == "unknown":
                    unknown_tool_names[tool_name] += 1
                children = list((presentation or {}).get("children") or [])
                totals["logical_operations"] += len(children) or 1
                if children:
                    totals["inferred_children"] += len(children)
                if (presentation or {}).get("wrapper_recedes"):
                    totals["wrappers_receded"] += 1
                elif children:
                    totals["wrappers_retained"] += 1

    for call_key in set(calls) | set(results):
        call_count = int(call_key in calls)
        result_count = results[call_key]
        if call_count and result_count == 1:
            totals["paired"] += 1
        elif call_count and not result_count:
            totals["orphan_calls"] += 1
        elif not call_count:
            totals["orphan_results"] += result_count
        elif result_count > 1:
            totals["duplicate_result_ids"] += result_count - 1

    for metric in (
        "archive_files",
        "outer_calls",
        "results",
        "paired",
        "exact",
        "parsed",
        "generic",
        "unknown",
        "logical_operations",
        "inferred_children",
        "wrappers_receded",
        "wrappers_retained",
        "orphan_calls",
        "orphan_results",
        "duplicate_call_ids",
        "duplicate_result_ids",
        "malformed_records",
        "unreadable_files",
    ):
        totals[metric] += 0
    red = bool(totals["duplicate_call_ids"] or totals["duplicate_result_ids"] or totals["malformed_records"] or totals["unreadable_files"])
    yellow = bool(totals["unknown"] or totals["orphan_calls"] or totals["orphan_results"])
    verdict = "red" if red else ("yellow" if yellow else "green")
    transcript_reasons = [
        *(["duplicate_or_malformed_evidence"] if red else []),
        *(["unknown_shapes_present"] if totals["unknown"] else []),
        *(["orphan_call_or_result"] if totals["orphan_calls"] or totals["orphan_results"] else []),
    ]
    return {
        "schema_version": 1,
        "profile": "privacy_safe_live_replay",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_id": f"codex-native-{hashlib.sha256(str(archive_root).encode()).hexdigest()[:12]}",
        "passed": not red,
        "verdicts": {
            "control": {"verdict": "not_evaluated", "reason": "archive replay has no control scenario"},
            "transcript": {"verdict": verdict, "reasons": transcript_reasons},
        },
        "totals": dict(sorted(totals.items())),
        "selection": {
            "strategy": "newest_file_window",
            "files_discovered": len(discovered_files),
            "files_scanned": len(files),
            "selection_truncated": selection_truncated,
        },
        "providers": {"codex": dict(sorted(totals.items()))},
        "reports": {
            "shape_unknown": {
                "schema_fingerprints": dict(sorted(fingerprints.items())),
                "tool_names": dict(tool_names.most_common()),
                "unknown_tool_names": dict(unknown_tool_names.most_common()),
                "provider_releases": dict(releases.most_common()),
                "affected_sessions": len(affected_sessions),
            },
            "conservation": {
                key: totals[key]
                for key in (
                    "outer_calls",
                    "results",
                    "paired",
                    "orphan_calls",
                    "orphan_results",
                    "duplicate_call_ids",
                    "duplicate_result_ids",
                    "malformed_records",
                    "unreadable_files",
                )
            },
            "presentation": {
                key: totals[key]
                for key in (
                    "outer_calls",
                    "logical_operations",
                    "exact",
                    "parsed",
                    "generic",
                    "unknown",
                    "inferred_children",
                    "wrappers_receded",
                    "wrappers_retained",
                )
            },
        },
        "factory_health": {
            "state": "unknown" if selection_truncated else "current",
            "reason": "selection_truncated" if selection_truncated else None,
            "complete_window": not selection_truncated,
        },
        "errors": errors,
    }


def write_evidence_package(report: dict[str, Any], output_root: Path) -> Path:
    """Write the shared immutable report layout without copying raw payloads."""

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{report['manifest_id']}"
    root = output_root.expanduser().resolve() / run_id
    root.mkdir(parents=True, exist_ok=False)
    (root / "candidate-patches").mkdir()
    (root / "fixture-candidates").mkdir()
    (root / "screenshots").mkdir()
    files = {
        "run.json": {
            "schema_version": report["schema_version"],
            "run_id": run_id,
            "manifest_id": report["manifest_id"],
            "profile": report.get("profile"),
            "evaluated_at": report.get("evaluated_at"),
        },
        "action-matrix.json": {
            "capture_and_conserve": report["reports"]["conservation"],
            "normalize_and_project": report["reports"]["shape_unknown"],
            "present_and_disclose": report["reports"]["presentation"],
            "control_surface": report["verdicts"]["control"],
        },
        "shape-inventory.json": report["reports"]["shape_unknown"],
        "conservation-report.json": report["reports"]["conservation"],
        "control-report.json": report["verdicts"]["control"],
        "presentation-report.json": report["reports"]["presentation"],
        "baseline-diff.json": {"status": "not_compared"},
        "verdict.json": {"verdicts": report["verdicts"], "factory_health": report["factory_health"]},
    }
    for name, payload in files.items():
        (root / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    unknown_candidates = report["reports"]["shape_unknown"].get("unknowns")
    if unknown_candidates is None:
        unknown_candidates = [
            {"tool_name": name, "count": count, "status": "review_required"}
            for name, count in report["reports"]["shape_unknown"].get("unknown_tool_names", {}).items()
        ]
    (root / "fixture-candidates" / "unknown-shapes.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "privacy": "structural_only",
                "candidates": unknown_candidates,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    latest_path = output_root.expanduser().resolve() / "latest.json"
    latest_temp = latest_path.with_name(f".{latest_path.name}.{uuid4().hex}.tmp")
    latest_temp.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "path": str(root),
                "evaluated_at": report.get("evaluated_at"),
                "profile": report.get("profile"),
                "providers": sorted(report.get("providers", {})),
                "totals": report.get("totals", {}),
                "verdicts": report["verdicts"],
                "factory_health": report.get("factory_health", {}),
            },
            indent=2,
        )
        + "\n"
    )
    latest_temp.replace(latest_path)
    return root
