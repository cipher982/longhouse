#!/usr/bin/env python3
"""Tests for provider release-proof maturity rollups."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "qa" / "provider-release-proof-maturity.py"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_maturity(
    root: Path,
    *,
    coverage: Path,
    baseline_root: Path | None = None,
    universal_artifact: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "maturity.json"
    args = [
        sys.executable,
        str(SCRIPT),
        "--coverage",
        str(coverage),
        "--artifact",
        str(artifact),
        "--json",
    ]
    if baseline_root is not None:
        args.extend(["--baseline-root", str(baseline_root)])
    if universal_artifact is not None:
        args.extend(["--universal-artifact", str(universal_artifact)])
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, _read_json(artifact)


def _write_coverage(path: Path) -> None:
    payload = {
        "schema_version": 2,
        "providers": ["opencode", "claude"],
        "surfaces": ["binary identity", "send input", "live-token behavior"],
        "accepted_release_proof_scenarios": [
            {
                "provider": "opencode",
                "scenario_id": "opencode-release-proof-v1",
                "provider_version": "opencode 1.16.2",
                "baseline_scope": "no_token_server_api_control_shape",
                "baseline_boundary": "live_no_token",
                "promoted_to_sauron": True,
            },
            {
                "provider": "claude",
                "scenario_id": "claude-release-proof-v1",
                "provider_version": "Claude Code 2.1.181",
                "baseline_scope": "no_token_contract_shape",
                "baseline_boundary": "live_no_token",
                "promoted_to_sauron": False,
            },
        ],
        "rows": [
            {
                "provider": "opencode",
                "surface": "binary identity",
                "covered": "yes",
                "runs_in_ci": True,
                "runs_in_sauron_release_watch": True,
                "accepted_baseline": "release_proof",
                "baseline_scenarios": ["opencode-release-proof-v1"],
                "failure_actionable": True,
            },
            {
                "provider": "opencode",
                "surface": "send input",
                "covered": "partial",
                "runs_in_ci": True,
                "runs_in_sauron_release_watch": False,
                "accepted_baseline": "none",
                "baseline_scenarios": [],
                "failure_actionable": True,
            },
            {
                "provider": "opencode",
                "surface": "live-token behavior",
                "covered": "no",
                "runs_in_ci": False,
                "runs_in_sauron_release_watch": False,
                "accepted_baseline": "none",
                "baseline_scenarios": [],
                "failure_actionable": False,
            },
            {
                "provider": "claude",
                "surface": "binary identity",
                "covered": "partial",
                "runs_in_ci": True,
                "runs_in_sauron_release_watch": True,
                "accepted_baseline": "release_proof",
                "baseline_scenarios": ["claude-release-proof-v1"],
                "failure_actionable": True,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_maturity_rollup_scores_static_coverage_without_baselines() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        coverage = root / "coverage.json"
        _write_coverage(coverage)

        result, payload = _run_maturity(root, coverage=coverage)

        assert result.returncode == 0, result.stderr
        assert payload["artifact_kind"] == "provider_release_proof_maturity_rollup"
        assert payload["overall"]["total"] == 4
        assert payload["overall"]["yes"] == 1
        assert payload["overall"]["partial"] == 2
        assert payload["overall"]["no"] == 1
        assert payload["overall"]["weighted_percent"] == 50.0
        assert payload["overall"]["composite_inputs"] == ["coverage_weighted_percent"]
        assert payload["accepted_baselines"]["status"] == "not_checked"
        assert payload["release_baseline_rows"]["status"] == "not_checked"
        assert payload["provider_rollups"]["opencode"]["weighted_percent"] == 50.0


def test_maturity_rollup_checks_accepted_baseline_store() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        coverage = root / "coverage.json"
        baseline_root = root / "baselines"
        accepted = (
            baseline_root / "opencode" / "opencode-release-proof-v1" / "accepted.json"
        )
        accepted.parent.mkdir(parents=True)
        accepted.write_text(
            json.dumps(
                {
                    "artifact_kind": "provider_release_proof",
                    "provider": "opencode",
                    "scenario_id": "opencode-release-proof-v1",
                    "provider_version": "opencode 1.16.2",
                    "verdict": "green",
                }
            ),
            encoding="utf-8",
        )
        _write_coverage(coverage)

        result, payload = _run_maturity(
            root, coverage=coverage, baseline_root=baseline_root
        )

        assert result.returncode == 0, result.stderr
        assert payload["accepted_baselines"]["status"] == "checked"
        assert payload["accepted_baselines"]["scenario_count"] == 2
        assert payload["accepted_baselines"]["green"] == 1
        assert payload["accepted_baselines"]["missing"] == 1
        assert payload["accepted_baselines"]["green_percent"] == 50.0
        assert payload["release_baseline_rows"]["green"] == 1
        assert payload["release_baseline_rows"]["missing_or_not_green"] == 1
        assert payload["overall"]["composite_inputs"] == [
            "coverage_weighted_percent",
            "accepted_baseline_green_percent",
        ]


def test_maturity_rollup_summarizes_universal_action_matrix() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        coverage = root / "coverage.json"
        universal = root / "universal.json"
        _write_coverage(coverage)
        universal.write_text(
            json.dumps(
                {
                    "artifact_kind": "universal_agent_harness_run",
                    "verdict": "yellow",
                    "results": [
                        {
                            "provider": "opencode",
                            "scenario": "action_matrix",
                            "status": "blocked",
                            "data": {
                                "action_count": 4,
                                "status_counts": {
                                    "pass": 2,
                                    "blocked": 1,
                                    "unsupported_gap": 1,
                                },
                            },
                        },
                        {
                            "provider": "opencode",
                            "scenario": "control_surface",
                            "status": "blocked",
                        },
                    ],
                    "provider_execution_coverage_matrix": {
                        "artifact_kind": "universal_agent_harness_provider_execution_coverage_matrix",
                        "providers": ["opencode", "claude"],
                        "actions": [
                            {
                                "action_id": "send_message",
                                "providers": {
                                    "opencode": {
                                        "coverage_kind": "executable_scenario",
                                        "coverage_status": "pass",
                                    },
                                    "claude": {
                                        "coverage_kind": "executable_scenario",
                                        "coverage_status": "blocked",
                                    },
                                },
                            },
                            {
                                "action_id": "tool_call_result",
                                "providers": {
                                    "opencode": {
                                        "coverage_kind": "matrix_contract",
                                        "coverage_status": "pass",
                                    },
                                    "claude": {
                                        "coverage_kind": "matrix_contract",
                                        "coverage_status": "unsupported_gap",
                                    },
                                },
                            },
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

        result, payload = _run_maturity(
            root, coverage=coverage, universal_artifact=universal
        )

        assert result.returncode == 0, result.stderr
        assert payload["universal_harness"]["status"] == "checked"
        assert payload["universal_harness"]["scenario_status_counts"] == {"blocked": 2}
        assert payload["universal_harness"]["action_matrix_pass_percent"] == 50.0
        assert payload["universal_harness"]["providers"]["opencode"][
            "action_matrix"
        ] == {
            "action_count": 4,
            "pass_percent": 50.0,
            "status_counts": {"blocked": 1, "pass": 2, "unsupported_gap": 1},
        }
        assert payload["universal_harness"]["execution_coverage_pass_percent"] == 50.0
        assert payload["universal_harness"]["executable_scenario_percent"] == 50.0
        assert payload["universal_harness"]["matrix_contract_percent"] == 50.0
        assert payload["universal_harness"]["providers"]["opencode"][
            "execution_coverage"
        ] == {
            "action_count": 2,
            "coverage_kind_counts": {
                "executable_scenario": 1,
                "matrix_contract": 1,
            },
            "coverage_status_counts": {"pass": 2},
            "executable_scenario_percent": 50.0,
            "matrix_contract_percent": 50.0,
            "pass_percent": 100.0,
        }
        assert (
            payload["universal_harness"]["providers"]["claude"]["execution_coverage"][
                "pass_percent"
            ]
            == 0.0
        )
        assert payload["overall"]["composite_inputs"] == [
            "coverage_weighted_percent",
            "action_matrix_pass_percent",
        ]


def main() -> int:
    tests = [
        test_maturity_rollup_scores_static_coverage_without_baselines,
        test_maturity_rollup_checks_accepted_baseline_store,
        test_maturity_rollup_summarizes_universal_action_matrix,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
