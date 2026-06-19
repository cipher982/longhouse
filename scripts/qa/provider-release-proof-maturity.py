#!/usr/bin/env python3
"""Compute auditable provider release-proof maturity rollups.

This command turns the static coverage inventory, accepted-baseline store, and
optional universal harness artifacts into machine-readable ratios. It does not
decide release safety; it reports what evidence exists.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_COVERAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "specs"
    / "provider-release-proof-coverage.json"
)
DEFAULT_OUTPUT_PATH = Path(".build/provider-release-proof-maturity.json")
COVERAGE_WEIGHTS = {"yes": 1.0, "partial": 0.5, "no": 0.0}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def _coverage_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "yes": sum(1 for row in rows if row.get("covered") == "yes"),
        "partial": sum(1 for row in rows if row.get("covered") == "partial"),
        "no": sum(1 for row in rows if row.get("covered") == "no"),
        "ci": sum(1 for row in rows if row.get("runs_in_ci") is True),
        "sauron": sum(
            1 for row in rows if row.get("runs_in_sauron_release_watch") is True
        ),
        "release_baseline_rows": sum(
            1 for row in rows if row.get("accepted_baseline") == "release_proof"
        ),
        "parser_baseline_rows": sum(
            1 for row in rows if row.get("accepted_baseline") == "parser_fixture"
        ),
        "actionable_rows": sum(
            1 for row in rows if row.get("failure_actionable") is True
        ),
    }


def _coverage_score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _coverage_counts(rows)
    weighted = sum(COVERAGE_WEIGHTS.get(str(row.get("covered")), 0.0) for row in rows)
    total = len(rows)
    return {
        **counts,
        "total": total,
        "weighted_points": weighted,
        "weighted_percent": _pct(weighted, total),
        "ci_percent": _pct(counts["ci"], total),
        "sauron_percent": _pct(counts["sauron"], total),
        "release_baseline_percent": _pct(counts["release_baseline_rows"], total),
        "actionable_percent": _pct(counts["actionable_rows"], total),
    }


def _baseline_path(baseline_root: Path, provider: str, scenario_id: str) -> Path:
    return baseline_root / provider / scenario_id / "accepted.json"


def _baseline_statuses(
    accepted_scenarios: list[dict[str, Any]],
    *,
    baseline_root: Path | None,
) -> tuple[dict[str, Any], dict[tuple[str, str], str]]:
    if baseline_root is None:
        return {
            "status": "not_checked",
            "baseline_root": None,
            "scenario_count": len(accepted_scenarios),
            "green": 0,
            "missing": 0,
            "red_or_yellow": 0,
            "unreadable": 0,
            "green_percent": None,
            "scenarios": [],
        }, {}

    statuses: list[dict[str, Any]] = []
    status_by_key: dict[tuple[str, str], str] = {}
    for scenario in accepted_scenarios:
        provider = str(scenario.get("provider") or "")
        scenario_id = str(scenario.get("scenario_id") or "")
        path = _baseline_path(baseline_root, provider, scenario_id)
        status = "missing"
        provider_version = None
        failure_code = "baseline_missing"
        try:
            accepted = _read_json(path)
        except FileNotFoundError:
            accepted = None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            accepted = None
            status = "unreadable"
            failure_code = f"{type(exc).__name__}: {exc}"
        if accepted is not None:
            provider_version = accepted.get("provider_version")
            proof_verdict = str(accepted.get("verdict") or "").lower()
            if proof_verdict == "green":
                status = "green"
                failure_code = None
            else:
                status = "red_or_yellow"
                failure_code = str(
                    accepted.get("failure_code") or "accepted_baseline_not_green"
                )
        status_by_key[(provider, scenario_id)] = status
        statuses.append(
            {
                "provider": provider,
                "scenario_id": scenario_id,
                "expected_provider_version": scenario.get("provider_version"),
                "provider_version": provider_version,
                "promoted_to_sauron": bool(scenario.get("promoted_to_sauron")),
                "accepted_path": str(path),
                "status": status,
                "failure_code": failure_code,
            }
        )

    counts = {
        "green": sum(1 for row in statuses if row["status"] == "green"),
        "missing": sum(1 for row in statuses if row["status"] == "missing"),
        "red_or_yellow": sum(1 for row in statuses if row["status"] == "red_or_yellow"),
        "unreadable": sum(1 for row in statuses if row["status"] == "unreadable"),
    }
    return {
        "status": "checked",
        "baseline_root": str(baseline_root),
        "scenario_count": len(accepted_scenarios),
        **counts,
        "green_percent": _pct(counts["green"], len(accepted_scenarios)),
        "scenarios": statuses,
    }, status_by_key


def _release_baseline_row_status(
    rows: list[dict[str, Any]],
    *,
    status_by_key: dict[tuple[str, str], str],
    baseline_checked: bool,
) -> dict[str, Any]:
    release_rows = [
        row for row in rows if row.get("accepted_baseline") == "release_proof"
    ]
    if not baseline_checked:
        return {
            "status": "not_checked",
            "row_count": len(release_rows),
            "green": 0,
            "missing_or_not_green": 0,
            "green_percent": None,
        }
    green = 0
    missing_or_not_green = 0
    for row in release_rows:
        provider = str(row.get("provider") or "")
        scenario_ids = [str(item) for item in row.get("baseline_scenarios") or []]
        if scenario_ids and all(
            status_by_key.get((provider, scenario_id)) == "green"
            for scenario_id in scenario_ids
        ):
            green += 1
        else:
            missing_or_not_green += 1
    return {
        "status": "checked",
        "row_count": len(release_rows),
        "green": green,
        "missing_or_not_green": missing_or_not_green,
        "green_percent": _pct(green, len(release_rows)),
    }


def _action_matrix_rollup(universal_artifacts: list[Path]) -> dict[str, Any]:
    if not universal_artifacts:
        return {
            "status": "not_provided",
            "artifact_count": 0,
            "providers": {},
            "scenario_status_counts": {},
            "action_matrix_pass_percent": None,
        }

    providers: dict[str, dict[str, Any]] = {}
    scenario_status_counts: dict[str, int] = {}
    action_pass = 0
    action_total = 0
    execution_pass = 0
    execution_total = 0
    executable_scenario_total = 0
    matrix_contract_total = 0
    loaded_artifacts: list[str] = []
    errors: list[dict[str, str]] = []

    for path in universal_artifacts:
        try:
            artifact = _read_json(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        loaded_artifacts.append(str(path))
        for result in artifact.get("results") or []:
            if not isinstance(result, dict):
                continue
            provider = str(result.get("provider") or "unknown")
            scenario = str(result.get("scenario") or "unknown")
            status = str(result.get("status") or "unknown")
            scenario_status_counts[status] = scenario_status_counts.get(status, 0) + 1
            provider_entry = providers.setdefault(
                provider,
                {
                    "scenario_status_counts": {},
                    "action_matrix": None,
                    "execution_coverage": None,
                },
            )
            provider_entry["scenario_status_counts"][status] = (
                provider_entry["scenario_status_counts"].get(status, 0) + 1
            )
            if scenario != "action_matrix":
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            status_counts = (
                data.get("status_counts")
                if isinstance(data.get("status_counts"), dict)
                else {}
            )
            action_count = int(data.get("action_count") or 0)
            pass_count = int(status_counts.get("pass") or 0)
            action_pass += pass_count
            action_total += action_count
            provider_entry["action_matrix"] = {
                "action_count": action_count,
                "status_counts": dict(status_counts),
                "pass_percent": _pct(pass_count, action_count),
            }
        execution_matrix = artifact.get("provider_execution_coverage_matrix")
        if not isinstance(execution_matrix, dict):
            continue
        execution_actions = execution_matrix.get("actions")
        if not isinstance(execution_actions, list):
            continue
        provider_execution_totals: dict[str, dict[str, Any]] = {}
        for row in execution_actions:
            if not isinstance(row, dict):
                continue
            row_providers = row.get("providers")
            if not isinstance(row_providers, dict):
                continue
            for provider, cell in row_providers.items():
                if not isinstance(cell, dict):
                    continue
                coverage_status = str(cell.get("coverage_status") or "missing")
                coverage_kind = str(cell.get("coverage_kind") or "unknown")
                provider_key = str(provider)
                totals = provider_execution_totals.setdefault(
                    provider_key,
                    {
                        "action_count": 0,
                        "pass": 0,
                        "coverage_status_counts": {},
                        "coverage_kind_counts": {},
                    },
                )
                totals["action_count"] += 1
                totals["coverage_status_counts"][coverage_status] = (
                    totals["coverage_status_counts"].get(coverage_status, 0) + 1
                )
                totals["coverage_kind_counts"][coverage_kind] = (
                    totals["coverage_kind_counts"].get(coverage_kind, 0) + 1
                )
                execution_total += 1
                if coverage_status == "pass":
                    totals["pass"] += 1
                    execution_pass += 1
                if coverage_kind == "executable_scenario":
                    executable_scenario_total += 1
                elif coverage_kind == "matrix_contract":
                    matrix_contract_total += 1
        for provider, totals in provider_execution_totals.items():
            provider_entry = providers.setdefault(
                provider,
                {
                    "scenario_status_counts": {},
                    "action_matrix": None,
                    "execution_coverage": None,
                },
            )
            action_count = int(totals["action_count"] or 0)
            executable_count = int(
                totals["coverage_kind_counts"].get("executable_scenario") or 0
            )
            matrix_contract_count = int(
                totals["coverage_kind_counts"].get("matrix_contract") or 0
            )
            provider_entry["execution_coverage"] = {
                "action_count": action_count,
                "coverage_status_counts": dict(totals["coverage_status_counts"]),
                "coverage_kind_counts": dict(totals["coverage_kind_counts"]),
                "pass_percent": _pct(float(totals["pass"]), float(action_count)),
                "executable_scenario_percent": _pct(
                    float(executable_count),
                    float(action_count),
                ),
                "matrix_contract_percent": _pct(
                    float(matrix_contract_count),
                    float(action_count),
                ),
            }

    return {
        "status": "checked" if not errors else "partial",
        "artifact_count": len(universal_artifacts),
        "loaded_artifacts": loaded_artifacts,
        "errors": errors,
        "providers": providers,
        "scenario_status_counts": scenario_status_counts,
        "action_matrix_pass_percent": _pct(action_pass, action_total)
        if action_total
        else None,
        "execution_coverage_pass_percent": _pct(execution_pass, execution_total)
        if execution_total
        else None,
        "executable_scenario_percent": _pct(
            executable_scenario_total,
            execution_total,
        )
        if execution_total
        else None,
        "matrix_contract_percent": _pct(matrix_contract_total, execution_total)
        if execution_total
        else None,
    }


def compute_rollup(
    *,
    coverage_path: Path,
    baseline_root: Path | None,
    universal_artifacts: list[Path],
) -> dict[str, Any]:
    coverage = _read_json(coverage_path)
    providers = [str(provider) for provider in coverage.get("providers") or []]
    rows = [row for row in coverage.get("rows") or [] if isinstance(row, dict)]
    accepted_scenarios = [
        scenario
        for scenario in coverage.get("accepted_release_proof_scenarios") or []
        if isinstance(scenario, dict)
    ]
    provider_rollups = {
        provider: _coverage_score(
            [row for row in rows if row.get("provider") == provider]
        )
        for provider in providers
    }
    baseline_rollup, status_by_key = _baseline_statuses(
        accepted_scenarios, baseline_root=baseline_root
    )
    release_row_baselines = _release_baseline_row_status(
        rows,
        status_by_key=status_by_key,
        baseline_checked=baseline_root is not None,
    )
    action_rollup = _action_matrix_rollup(universal_artifacts)
    coverage_rollup = _coverage_score(rows)
    available_scores = [coverage_rollup["weighted_percent"]]
    if isinstance(baseline_rollup.get("green_percent"), (int, float)):
        available_scores.append(float(baseline_rollup["green_percent"]))
    if isinstance(action_rollup.get("action_matrix_pass_percent"), (int, float)):
        available_scores.append(float(action_rollup["action_matrix_pass_percent"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "provider_release_proof_maturity_rollup",
        "generated_at": _now_iso(),
        "coverage_path": str(coverage_path),
        "providers": providers,
        "surfaces": coverage.get("surfaces") or [],
        "overall": {
            **coverage_rollup,
            "available_score_count": len(available_scores),
            "composite_percent": round(
                sum(available_scores) / len(available_scores), 1
            ),
            "composite_inputs": [
                "coverage_weighted_percent",
                *(
                    ["accepted_baseline_green_percent"]
                    if isinstance(baseline_rollup.get("green_percent"), (int, float))
                    else []
                ),
                *(
                    ["action_matrix_pass_percent"]
                    if isinstance(
                        action_rollup.get("action_matrix_pass_percent"), (int, float)
                    )
                    else []
                ),
            ],
        },
        "provider_rollups": provider_rollups,
        "accepted_baselines": baseline_rollup,
        "release_baseline_rows": release_row_baselines,
        "universal_harness": action_rollup,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE_PATH)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--universal-artifact", type=Path, action="append", default=[])
    parser.add_argument("--artifact", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = compute_rollup(
        coverage_path=args.coverage,
        baseline_root=args.baseline_root,
        universal_artifacts=args.universal_artifact,
    )
    _write_json(args.artifact, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(str(args.artifact))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
