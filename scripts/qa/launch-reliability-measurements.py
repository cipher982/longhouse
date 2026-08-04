#!/usr/bin/env python3
"""Summarize retained launch-reliability evidence without inventing metrics.

The installed managed-launch matrix is deliberately allowed to be yellow. This
report keeps provider setup gaps, provider-owned failures, and successful
recovery separate, and marks measures that the supplied artifacts cannot prove
as ``not_observed``. It is a report over existing evidence, not a new incident
registry.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


MATRIX_FILENAME = "installed-managed-launch-fault-matrix.json"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def discover_matrix_artifacts(inputs: Iterable[Path]) -> list[Path]:
    """Resolve files/directories and return unique matrix artifacts in order."""

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = raw.expanduser().resolve()
        candidates = [path] if path.is_file() else sorted(path.glob(f"*/{MATRIX_FILENAME}"))
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _is_full_run(artifact: dict[str, Any]) -> bool:
    return len(artifact.get("providers") or []) >= 8 and artifact.get("artifact_kind") == "installed_managed_launch_fault_matrix"


def _cleanup_statuses(artifact: dict[str, Any]) -> list[str]:
    statuses: list[str] = []
    for provider in artifact.get("providers") or []:
        cleanup = provider.get("detached_cleanup")
        if isinstance(cleanup, dict):
            statuses.append(str(cleanup.get("status") or "unknown"))
    return statuses


def _auth_gaps(artifact: dict[str, Any]) -> list[str]:
    return sorted(
        provider
        for provider, auth in (artifact.get("provider_auth") or {}).items()
        if isinstance(auth, dict) and auth.get("status") != "ready"
    )


def _provider_owned_failure_count(artifact: dict[str, Any]) -> int:
    return sum(
        1
        for failure in artifact.get("provider_startup_failures") or []
        if isinstance(failure, dict) and failure.get("provider")
    )


def _measured_run(artifact: dict[str, Any]) -> bool:
    measurements = artifact.get("measurements") or {}
    return bool(
        _is_full_run(artifact)
        and isinstance(measurements, dict)
        and measurements.get("recovery_duration_seconds") is not None
        and measurements.get("run_duration_seconds") is not None
        and artifact.get("harness", {}).get("repository_dirty") is False
        and artifact.get("harness", {}).get("harness_file_dirty") is False
    )


def _successful_recovery(artifact: dict[str, Any]) -> bool:
    before = artifact.get("retry_intents_before_recovery")
    after = artifact.get("retry_intents_after_recovery")
    cleanup = _cleanup_statuses(artifact)
    return bool(
        _measured_run(artifact)
        and isinstance(before, int)
        and isinstance(after, int)
        and before >= 0
        and after == 0
        and len(cleanup) == len(artifact.get("providers") or [])
        and all(status == "pass" for status in cleanup)
    )


def _not_observed(reason: str) -> dict[str, str]:
    return {"status": "not_observed", "reason": reason}


def build_report(matrix_paths: Iterable[Path], harness_paths: Iterable[Path] = ()) -> dict[str, Any]:
    matrix_artifacts: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    for path in matrix_paths:
        try:
            matrix_artifacts.append((path, _json(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})

    full = [(path, artifact) for path, artifact in matrix_artifacts if _is_full_run(artifact)]
    measured = [(path, artifact) for path, artifact in full if _measured_run(artifact)]
    successful = [(path, artifact) for path, artifact in measured if _successful_recovery(artifact)]
    recovery_seconds = [float(artifact["measurements"]["recovery_duration_seconds"]) for _, artifact in measured]
    run_seconds = [float(artifact["measurements"]["run_duration_seconds"]) for _, artifact in measured]
    verdicts = Counter(str(artifact.get("verdict") or "unknown") for _, artifact in matrix_artifacts)
    auth_gap_runs = [
        {"path": str(path), "providers": _auth_gaps(artifact)}
        for path, artifact in measured
        if _auth_gaps(artifact)
    ]
    provider_failures = [
        {"path": str(path), "count": _provider_owned_failure_count(artifact)}
        for path, artifact in measured
        if _provider_owned_failure_count(artifact)
    ]

    history = []
    for path, artifact in measured:
        cleanup = _cleanup_statuses(artifact)
        measurements = artifact.get("measurements") or {}
        history.append(
            {
                "path": str(path),
                "generated_at": artifact.get("generated_at"),
                "verdict": artifact.get("verdict"),
                "recovery_qualification": artifact.get("recovery_qualification"),
                "retry_intents": {
                    "before": artifact.get("retry_intents_before_recovery"),
                    "after": artifact.get("retry_intents_after_recovery"),
                },
                "cleanup": {"pass": cleanup.count("pass"), "total": len(cleanup)},
                "auth_gaps": _auth_gaps(artifact),
                "provider_owned_startup_failures": _provider_owned_failure_count(artifact),
                "recovery_duration_seconds": measurements.get("recovery_duration_seconds"),
                "run_duration_seconds": measurements.get("run_duration_seconds"),
                "harness_repository_git_sha": (artifact.get("harness") or {}).get("repository_git_sha"),
                "implementation_source_git_sha": ((artifact.get("implementation") or {}).get("longhouse") or {}).get("source_git_sha"),
            }
        )

    harness_results: Counter[str] = Counter()
    harness_levels: Counter[str] = Counter()
    harness_inputs: list[str] = []
    for path in harness_paths:
        try:
            artifact = _json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
            continue
        harness_inputs.append(str(path))
        for result in artifact.get("results") or []:
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "unknown")
            harness_results[status] += 1
            for evidence in (result.get("data") or {}).get("operation_evidence", {}).values():
                if isinstance(evidence, dict):
                    level = str(evidence.get("level") or "unknown")
                    harness_levels[level] += 1

    report = {
        "schema_version": 1,
        "artifact_kind": "launch_reliability_measurements",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "inputs": {
            "matrix_artifacts": [str(path) for path, _ in matrix_artifacts],
            "provider_harness_artifacts": harness_inputs,
            "invalid_artifacts": invalid,
        },
        "matrix": {
            "artifact_count": len(matrix_artifacts),
            "full_run_count": len(full),
            "measured_clean_run_count": len(measured),
            "successful_recovery_count": len(successful),
            "verdict_counts": dict(sorted(verdicts.items())),
            "retry_drain_rate": (len(successful) / len(measured)) if measured else None,
            "cleanup_pass_rate": (
                sum(_cleanup_statuses(artifact).count("pass") for _, artifact in measured)
                / sum(len(_cleanup_statuses(artifact)) for _, artifact in measured)
                if measured and sum(len(_cleanup_statuses(artifact)) for _, artifact in measured)
                else None
            ),
            "auth_precondition_runs": auth_gap_runs,
            "provider_owned_failure_runs": provider_failures,
            "recovery_duration_seconds": {
                "count": len(recovery_seconds),
                "min": min(recovery_seconds) if recovery_seconds else None,
                "median": statistics.median(recovery_seconds) if recovery_seconds else None,
                "max": max(recovery_seconds) if recovery_seconds else None,
            },
            "run_duration_seconds": {
                "count": len(run_seconds),
                "min": min(run_seconds) if run_seconds else None,
                "median": statistics.median(run_seconds) if run_seconds else None,
                "max": max(run_seconds) if run_seconds else None,
            },
            "history": history,
        },
        "provider_scenarios": {
            "result_status_counts": dict(sorted(harness_results.items())),
            "evidence_level_counts": dict(sorted(harness_levels.items())),
        },
        "measures": {
            "automatic_recovery_time": (
                {"status": "observed", "source": "matrix.measurements"} if recovery_seconds else _not_observed("no measured clean matrix run")
            ),
            "false_red_rate": _not_observed("retained matrix artifacts do not include a user-action/data-risk ground-truth label"),
            "hidden_failure_rate": _not_observed("no longitudinal producer-freshness truth series was supplied"),
            "unresolved_event_bearing_issue_age": _not_observed("no event-bearing issue lifecycle series was supplied"),
            "action_coverage": _not_observed("local action tests are separate evidence and were not passed as a coverage artifact"),
            "duplicate_replayed_discarded_evidence": _not_observed("matrix artifacts do not carry end-to-end evidence conservation counters"),
        },
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-root", action="append", type=Path, required=True, help="Matrix artifact file or directory; repeatable.")
    parser.add_argument("--provider-harness-artifact", action="append", type=Path, default=[], help="Universal provider harness JSON; repeatable.")
    parser.add_argument("--output", type=Path, help="Write the report JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(discover_matrix_artifacts(args.matrix_root), args.provider_harness_artifact)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
        print(args.output)
    else:
        print(encoded, end="")
    return 0 if not report["inputs"]["invalid_artifacts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
