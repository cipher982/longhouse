"""Shared release-proof rollup helpers."""

from __future__ import annotations

from typing import Any

PASS_STATUS = "pass"

PASSED_GAP_KIND = "passed"
EXPECTED_LIMIT_GAP_KINDS = {"not_applicable", "provider_contract_unsupported"}
PROOF_GAP_KINDS = {
    "missing_credentials",
    "missing_coverage",
    "missing_live_canary",
    "no_token_safety_gate",
}
REGRESSION_OR_UNKNOWN_GAP_KINDS = {
    "flaky",
    "unexpected_failure",
    "unknown_gap",
    "xfail_with_expiry",
}


def pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def new_execution_bucket() -> dict[str, Any]:
    return {
        "cell_count": 0,
        "pass": 0,
        "coverage_status_counts": {},
        "coverage_kind_counts": {},
        "coverage_gap_kind_counts": {},
    }


def record_execution_cell(
    bucket: dict[str, Any],
    *,
    coverage_status: str,
    coverage_kind: str,
    coverage_gap_kind: str = "unknown_gap",
) -> None:
    bucket["cell_count"] += 1
    if coverage_status == PASS_STATUS:
        bucket["pass"] += 1
    increment(bucket["coverage_status_counts"], coverage_status)
    increment(bucket["coverage_kind_counts"], coverage_kind)
    increment(bucket["coverage_gap_kind_counts"], coverage_gap_kind)


def finalize_execution_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    cell_count = int(bucket["cell_count"] or 0)
    executable_count = int(
        bucket["coverage_kind_counts"].get("executable_scenario") or 0
    )
    matrix_contract_count = int(
        bucket["coverage_kind_counts"].get("matrix_contract") or 0
    )
    return {
        "cell_count": cell_count,
        "coverage_gap_kind_counts": dict(bucket["coverage_gap_kind_counts"]),
        "coverage_kind_counts": dict(bucket["coverage_kind_counts"]),
        "coverage_status_counts": dict(bucket["coverage_status_counts"]),
        "executable_scenario_percent": pct(executable_count, cell_count),
        "matrix_contract_percent": pct(matrix_contract_count, cell_count),
        "pass": int(bucket["pass"] or 0),
        "pass_percent": pct(float(bucket["pass"]), float(cell_count)),
    }


def coverage_actionability(gap_kind_counts: dict[str, int]) -> dict[str, Any]:
    counts = {str(key): int(value or 0) for key, value in gap_kind_counts.items()}
    cell_count = sum(counts.values())
    passed = counts.get(PASSED_GAP_KIND, 0)
    expected_provider_limits = sum(
        count for key, count in counts.items() if key in EXPECTED_LIMIT_GAP_KINDS
    )
    proof_gaps = sum(count for key, count in counts.items() if key in PROOF_GAP_KINDS)
    regression_or_unknown = sum(
        count for key, count in counts.items() if key in REGRESSION_OR_UNKNOWN_GAP_KINDS
    )
    categorized = (
        {PASSED_GAP_KIND}
        | EXPECTED_LIMIT_GAP_KINDS
        | PROOF_GAP_KINDS
        | REGRESSION_OR_UNKNOWN_GAP_KINDS
    )
    uncategorized = sum(
        count for key, count in counts.items() if key not in categorized
    )
    attention_required = proof_gaps + regression_or_unknown + uncategorized

    if regression_or_unknown or uncategorized:
        status = "regression_or_unknown"
    elif proof_gaps:
        status = "needs_stronger_evidence"
    elif expected_provider_limits:
        status = "covered_with_expected_limits"
    else:
        status = "covered"

    return {
        "status": status,
        "cell_count": cell_count,
        "passed": passed,
        "pass_percent": pct(passed, cell_count),
        "expected_provider_limit_cells": expected_provider_limits,
        "proof_gap_cells": proof_gaps,
        "regression_or_unknown_cells": regression_or_unknown + uncategorized,
        "attention_required_cells": attention_required,
        "gap_kind_counts": counts,
    }
