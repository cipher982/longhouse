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
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


MATRIX_FILENAME = "installed-managed-launch-fault-matrix.json"
EXPECTED_PROVIDERS = frozenset({"claude", "codex", "cursor", "opencode"})


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def report_provenance() -> dict[str, Any]:
    """Bind the report to the exact reporter and repository state that made it."""

    path = Path(__file__).resolve()
    repository = path.parents[2]
    relative_path = path.relative_to(repository)
    status_lines = _git(repository, "status", "--porcelain", "--untracked-files=all").splitlines()
    file_status = _git(repository, "status", "--porcelain", "--untracked-files=all", "--", str(relative_path))
    return {
        "argv": list(sys.argv),
        "cwd": str(Path.cwd()),
        "git_sha": _git(repository, "rev-parse", "HEAD"),
        "harness_file_dirty": bool(file_status),
        "path": str(path),
        "repository": str(repository),
        "repository_dirty": bool(status_lines),
        "sha256": _sha256(path),
    }


def discover_matrix_artifacts(inputs: Iterable[Path]) -> list[Path]:
    """Resolve files/directories and return unique matrix artifacts in order."""

    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = raw.expanduser().resolve()
        if not path.exists():
            raise ValueError(f"matrix input does not exist: {path}")
        if path.is_file():
            candidates = [path]
        else:
            candidates = []
            direct = path / MATRIX_FILENAME
            if direct.is_file():
                candidates.append(direct)
            candidates.extend(sorted(path.glob(f"*/{MATRIX_FILENAME}")))
        if not candidates:
            raise ValueError(f"matrix input contains no {MATRIX_FILENAME}: {path}")
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _is_full_run(artifact: dict[str, Any]) -> bool:
    provider_counts = Counter(
        str(entry.get("provider") or "") for entry in artifact.get("providers") or [] if isinstance(entry, dict)
    )
    return (
        len(artifact.get("providers") or []) == 8
        and provider_counts == {provider: 2 for provider in EXPECTED_PROVIDERS}
        and artifact.get("artifact_kind") == "installed_managed_launch_fault_matrix"
    )


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


def _startup_failure_counts(artifact: dict[str, Any]) -> dict[str, int]:
    counts = {"provider_owned": 0, "harness_precondition": 0, "unknown": 0}
    for failure in artifact.get("provider_startup_failures") or []:
        if not isinstance(failure, dict) or not failure.get("provider"):
            continue
        qualification = failure.get("qualification")
        if qualification == "provider_owned_start_failure":
            counts["provider_owned"] += 1
        elif qualification == "harness_precondition_unmet":
            counts["harness_precondition"] += 1
        else:
            counts["unknown"] += 1
    return counts


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


def _excluded_run_reason(artifact: dict[str, Any]) -> str | None:
    if not _is_full_run(artifact):
        return "not_exact_four_provider_eight_launch_run"
    measurements = artifact.get("measurements") or {}
    harness = artifact.get("harness") or {}
    if measurements.get("recovery_duration_seconds") is None or measurements.get("run_duration_seconds") is None:
        return "missing_timing_measurements"
    if harness.get("repository_dirty") is None or harness.get("harness_file_dirty") is None:
        return "missing_clean_harness_provenance"
    if harness.get("repository_dirty") is not False or harness.get("harness_file_dirty") is not False:
        return "dirty_harness_or_repository"
    return None


def _input_reference(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha256(path)}


def _successful_recovery(artifact: dict[str, Any]) -> bool:
    before = artifact.get("retry_intents_before_recovery")
    after = artifact.get("retry_intents_after_recovery")
    cleanup = _cleanup_statuses(artifact)
    return bool(
        _measured_run(artifact)
        and isinstance(before, int)
        and isinstance(after, int)
        and before > 0
        and after == 0
        and len(cleanup) == len(artifact.get("providers") or [])
        and all(status == "pass" for status in cleanup)
    )


def _not_observed(reason: str) -> dict[str, str]:
    return {"status": "not_observed", "reason": reason}


def _health_measurements(paths: Iterable[Path], invalid: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    references: list[dict[str, str]] = []
    total = 0
    broken_cases = 0
    false_red_cases = 0
    eligible_failure_cases = 0
    hidden_failure_cases = 0
    action_total = 0
    action_pass = 0
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        try:
            artifact = _json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
            continue
        results = artifact.get("results")
        if not isinstance(results, list):
            invalid.append({"path": str(path), "error": "health artifact has no results list"})
            continue
        references.append(_input_reference(path))
        for result in results:
            if not isinstance(result, dict):
                continue
            expected = result.get("expected") or {}
            observed = result.get("observed") or {}
            expected_state = str(expected.get("state") or "unknown")
            observed_state = str(observed.get("health_state") or "unknown")
            total += 1
            if observed_state == "broken":
                broken_cases += 1
                if expected_state != "broken":
                    false_red_cases += 1
            if expected_state in {"broken", "degraded"}:
                eligible_failure_cases += 1
                if observed_state == "healthy":
                    hidden_failure_cases += 1
            expected_action = str(expected.get("action") or "none")
            if expected_action != "none":
                action_total += 1
                suggested = {str(value) for value in observed.get("suggested_action_ids") or []}
                if expected_action in suggested:
                    action_pass += 1

    scope = "installed_health_fault_matrix"
    measurements = {
        "false_red_rate": {
            "status": "observed" if total else "not_observed",
            "scope": scope,
            "basis": "fault_matrix_expected_state",
            "numerator": false_red_cases,
            "denominator": broken_cases,
            "numerator_definition": "observed_broken_cases_expected_not_broken",
            "denominator_definition": "observed_broken_cases",
            "rate": (false_red_cases / broken_cases) if broken_cases else None,
            "source": references,
        },
        "hidden_failure_rate": {
            "status": "observed" if total else "not_observed",
            "scope": scope,
            "numerator": hidden_failure_cases,
            "denominator": eligible_failure_cases,
            "denominator_definition": "expected_broken_or_degraded_cases",
            "rate": (hidden_failure_cases / eligible_failure_cases) if eligible_failure_cases else None,
            "source": references,
        },
        "action_coverage": {
            "status": "observed" if action_total else "not_observed",
            "scope": scope,
            "numerator": action_pass,
            "denominator": action_total,
            "denominator_definition": "cases_with_expected_action",
            "rate": (action_pass / action_total) if action_total else None,
            "source": references,
        },
    }
    return references, {"case_count": total, "measurements": measurements}


def build_report(
    matrix_paths: Iterable[Path],
    harness_paths: Iterable[Path] = (),
    health_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    matrix_artifacts: list[tuple[Path, dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    for path in matrix_paths:
        try:
            matrix_artifacts.append((path, _json(path)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})

    full = [(path, artifact) for path, artifact in matrix_artifacts if _is_full_run(artifact)]
    measured = [(path, artifact) for path, artifact in full if _measured_run(artifact)]
    excluded = [
        {"path": str(path), "reason": reason}
        for path, artifact in full
        if (reason := _excluded_run_reason(artifact)) is not None
    ]
    successful = [(path, artifact) for path, artifact in measured if _successful_recovery(artifact)]
    cleanup_statuses = [
        status for _, artifact in measured for status in _cleanup_statuses(artifact)
    ]
    recovery_seconds = [float(artifact["measurements"]["recovery_duration_seconds"]) for _, artifact in successful]
    run_seconds = [float(artifact["measurements"]["run_duration_seconds"]) for _, artifact in successful]
    verdicts = Counter(str(artifact.get("verdict") or "unknown") for _, artifact in matrix_artifacts)
    auth_gap_runs = [
        {"path": str(path), "providers": _auth_gaps(artifact)}
        for path, artifact in measured
        if _auth_gaps(artifact)
    ]
    provider_failures = [
        {"path": str(path), "count": _startup_failure_counts(artifact)["provider_owned"]}
        for path, artifact in measured
        if _startup_failure_counts(artifact)["provider_owned"]
    ]
    startup_failure_totals = Counter()
    for _, artifact in measured:
        startup_failure_totals.update(_startup_failure_counts(artifact))

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
                "startup_failures": _startup_failure_counts(artifact),
                "recovery_duration_seconds": measurements.get("recovery_duration_seconds"),
                "run_duration_seconds": measurements.get("run_duration_seconds"),
                "harness_repository_git_sha": (artifact.get("harness") or {}).get("repository_git_sha"),
                "implementation_source_git_sha": ((artifact.get("implementation") or {}).get("longhouse") or {}).get("source_git_sha"),
            }
        )

    harness_results: Counter[str] = Counter()
    harness_levels: Counter[str] = Counter()
    harness_operation_statuses: Counter[str] = Counter()
    blocked_operations: list[dict[str, str]] = []
    harness_inputs: list[str] = []
    for path in harness_paths:
        try:
            artifact = _json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append({"path": str(path), "error": str(exc)})
            continue
        if not isinstance(artifact.get("results"), list):
            invalid.append({"path": str(path), "error": "provider harness artifact has no results list"})
            continue
        harness_inputs.append(str(path))
        for result in artifact.get("results") or []:
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "unknown")
            harness_results[status] += 1
            operation_evidence = (result.get("data") or {}).get("operation_evidence", {})
            for operation, evidence in operation_evidence.items():
                if isinstance(evidence, dict):
                    operation_status = str(evidence.get("status") or "unknown")
                    harness_operation_statuses[operation_status] += 1
                    if operation_status == "blocked":
                        blocked_operations.append(
                            {
                                "provider": str(result.get("provider") or "unknown"),
                                "scenario": str(result.get("scenario") or "unknown"),
                                "operation": str(operation),
                                "failure_code": str(evidence.get("failure_code") or "unknown"),
                            }
                        )
                    level = str(evidence.get("level") or "unknown")
                    harness_levels[level] += 1

    health_inputs, health_summary = _health_measurements(health_paths, invalid)

    report = {
        "schema_version": 1,
        "artifact_kind": "launch_reliability_measurements",
        "report_status": "invalid" if invalid else "ok",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provenance": report_provenance(),
        "inputs": {
            "matrix_artifacts": [_input_reference(path) for path, _ in matrix_artifacts],
            "provider_harness_artifacts": [_input_reference(Path(path)) for path in harness_inputs],
            "health_artifacts": health_inputs,
            "invalid_artifacts": invalid,
        },
        "matrix": {
            "artifact_count": len(matrix_artifacts),
            "full_run_count": len(full),
            "measured_clean_run_count": len(measured),
            "successful_recovery_count": len(successful),
            "verdict_counts": dict(sorted(verdicts.items())),
            "retry_drain_rate": (len(successful) / len(measured)) if measured else None,
            "retry_drain_sample_count": len(measured),
            "retry_drain_denominator": "measured_clean_runs",
            "cleanup_pass_rate": (
                cleanup_statuses.count("pass") / len(cleanup_statuses)
                if cleanup_statuses
                else None
            ),
            "cleanup_pass_count": cleanup_statuses.count("pass"),
            "cleanup_scope_count": len(cleanup_statuses),
            "cleanup_rate_denominator": "measured_cleanup_scopes",
            "startup_failure_attribution": {
                "scope": "measured_clean_runs",
                "auth_precondition_runs": auth_gap_runs,
                "provider_owned_failure_runs": provider_failures,
                "totals": dict(sorted(startup_failure_totals.items())),
            },
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
            "excluded_full_runs": excluded,
        },
        "provider_scenarios": {
            "result_status_counts": dict(sorted(harness_results.items())),
            "operation_status_counts": dict(sorted(harness_operation_statuses.items())),
            "blocked_operations": blocked_operations,
            "evidence_level_counts": dict(sorted(harness_levels.items())),
        },
        "health_fault_matrix": health_summary,
        "measures": {
            "automatic_recovery_time": (
                {
                    "status": "observed",
                    "scope": "measured_clean_runs",
                    "sample_count": len(successful),
                    "source": "successful matrix.measurements",
                }
                if successful
                else _not_observed("no measured matrix run with a positive retry queue converged to zero and complete cleanup")
            ),
            "false_red_rate": health_summary["measurements"]["false_red_rate"]
            if health_summary["measurements"]["false_red_rate"]["status"] == "observed"
            else _not_observed("retained artifacts do not include a user-action/data-risk ground-truth label"),
            "hidden_failure_rate": health_summary["measurements"]["hidden_failure_rate"]
            if health_summary["measurements"]["hidden_failure_rate"]["status"] == "observed"
            else _not_observed("no longitudinal producer-freshness truth series was supplied"),
            "unresolved_event_bearing_issue_age": _not_observed("no event-bearing issue lifecycle series was supplied"),
            "action_coverage": health_summary["measurements"]["action_coverage"]
            if health_summary["measurements"]["action_coverage"]["status"] == "observed"
            else _not_observed("local action tests are separate evidence and were not passed as a coverage artifact"),
            "duplicate_replayed_discarded_evidence": _not_observed("matrix artifacts do not carry end-to-end evidence conservation counters"),
        },
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix-root", action="append", type=Path, required=True, help="Matrix artifact file or directory; repeatable.")
    parser.add_argument("--provider-harness-artifact", action="append", type=Path, default=[], help="Universal provider harness JSON; repeatable.")
    parser.add_argument("--health-artifact", action="append", type=Path, default=[], help="Installed native health fault matrix JSON; repeatable.")
    parser.add_argument("--output", type=Path, help="Write the report JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(discover_matrix_artifacts(args.matrix_root), args.provider_harness_artifact, args.health_artifact)
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
