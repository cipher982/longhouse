"""Shared release-proof rollup helpers."""

from __future__ import annotations

from typing import Any

PASS_STATUS = "pass"


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
    }


def record_execution_cell(
    bucket: dict[str, Any],
    *,
    coverage_status: str,
    coverage_kind: str,
) -> None:
    bucket["cell_count"] += 1
    if coverage_status == PASS_STATUS:
        bucket["pass"] += 1
    increment(bucket["coverage_status_counts"], coverage_status)
    increment(bucket["coverage_kind_counts"], coverage_kind)


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
        "coverage_kind_counts": dict(bucket["coverage_kind_counts"]),
        "coverage_status_counts": dict(bucket["coverage_status_counts"]),
        "executable_scenario_percent": pct(executable_count, cell_count),
        "matrix_contract_percent": pct(matrix_contract_count, cell_count),
        "pass": int(bucket["pass"] or 0),
        "pass_percent": pct(float(bucket["pass"]), float(cell_count)),
    }
