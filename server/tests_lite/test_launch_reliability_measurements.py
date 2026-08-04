from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/qa/launch-reliability-measurements.py"
SPEC = importlib.util.spec_from_file_location("launch_reliability_measurements", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _matrix(
    *,
    path: Path,
    verdict: str = "yellow",
    auth_gap: bool = True,
    provider_failure: bool = False,
    retry_after: int = 0,
) -> None:
    providers = []
    for provider in ("claude", "codex", "opencode", "cursor"):
        providers.extend(
            [
                {
                    "provider": provider,
                    "detached_cleanup": {"status": "pass"},
                },
                {
                    "provider": provider,
                    "detached_cleanup": {"status": "pass"},
                },
            ]
        )
    auth = {provider: {"status": "ready"} for provider in ("claude", "codex", "opencode", "cursor")}
    if auth_gap:
        auth["cursor"] = {"status": "missing"}
    startup_failures = []
    if auth_gap:
        startup_failures.append({"provider": "cursor", "qualification": "harness_precondition_unmet"})
    if provider_failure:
        startup_failures.append({"provider": "claude", "qualification": "provider_owned_start_failure"})
    payload = {
        "artifact_kind": "installed_managed_launch_fault_matrix",
        "generated_at": "2026-08-04T16:00:00Z",
        "verdict": verdict,
        "providers": providers,
        "provider_auth": auth,
        "provider_startup_failures": startup_failures,
        "retry_intents_before_recovery": 8,
        "retry_intents_after_recovery": retry_after,
        "measurements": {"recovery_duration_seconds": 5.0, "run_duration_seconds": 100.0},
        "harness": {"repository_dirty": False, "harness_file_dirty": False, "repository_git_sha": "harness-sha"},
        "implementation": {"longhouse": {"source_git_sha": "implementation-sha"}},
        "recovery_qualification": "mixed_provider_degraded_start_with_harness_precondition_gap",
    }
    path.write_text(json.dumps(payload))


def test_report_separates_recovery_from_setup_and_provider_failures(tmp_path: Path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _matrix(path=first)
    _matrix(path=second, auth_gap=False, provider_failure=True)

    report = MODULE.build_report([first, second])

    matrix = report["matrix"]
    assert matrix["full_run_count"] == 2
    assert matrix["measured_clean_run_count"] == 2
    assert matrix["successful_recovery_count"] == 2
    assert matrix["retry_drain_rate"] == 1.0
    assert matrix["retry_drain_sample_count"] == 2
    assert matrix["retry_drain_denominator"] == "measured_clean_runs"
    assert matrix["cleanup_pass_rate"] == 1.0
    assert matrix["cleanup_pass_count"] == 16
    assert matrix["cleanup_scope_count"] == 16
    assert matrix["cleanup_rate_denominator"] == "measured_cleanup_scopes"
    attribution = matrix["startup_failure_attribution"]
    assert attribution["scope"] == "measured_clean_runs"
    assert len(attribution["auth_precondition_runs"]) == 1
    assert len(attribution["provider_owned_failure_runs"]) == 1
    assert attribution["totals"] == {"harness_precondition": 1, "provider_owned": 1, "unknown": 0}
    assert matrix["history"][0]["startup_failures"]["harness_precondition"] == 1
    assert matrix["history"][1]["startup_failures"]["provider_owned"] == 1
    assert report["measures"]["false_red_rate"]["status"] == "not_observed"


def test_report_marks_live_provider_delivery_separately(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix)
    harness = tmp_path / "harness.json"
    harness.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "status": "blocked",
                        "data": {
                            "operation_evidence": {
                                "pause_request_detect": {"level": "hermetic", "status": "pass"},
                                "live_answer_delivery": {
                                    "level": "live_token_required",
                                    "status": "blocked",
                                    "failure_code": "answer_pause_provider_delivery_unproven",
                                },
                            }
                        },
                    }
                ]
            }
        )
    )

    report = MODULE.build_report([matrix], [harness])

    assert report["provider_scenarios"]["result_status_counts"] == {"blocked": 1}
    assert report["provider_scenarios"]["operation_status_counts"] == {"blocked": 1, "pass": 1}
    assert report["provider_scenarios"]["blocked_operations"][0]["operation"] == "live_answer_delivery"
    assert report["provider_scenarios"]["evidence_level_counts"] == {"hermetic": 1, "live_token_required": 1}


def test_report_does_not_observe_failed_recovery(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False, retry_after=3)

    report = MODULE.build_report([matrix])

    assert report["matrix"]["successful_recovery_count"] == 0
    assert report["measures"]["automatic_recovery_time"]["status"] == "not_observed"


def test_missing_matrix_input_fails_closed(tmp_path: Path):
    missing = tmp_path / "missing"

    try:
        MODULE.discover_matrix_artifacts([missing])
    except ValueError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("missing matrix input must fail closed")


def test_malformed_provider_harness_marks_report_invalid(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False)
    harness = tmp_path / "harness.json"
    harness.write_text(json.dumps({"artifact_kind": "universal_agent_harness_run"}))

    report = MODULE.build_report([matrix], [harness])

    assert report["report_status"] == "invalid"
    assert report["inputs"]["invalid_artifacts"] == [
        {"path": str(harness), "error": "provider harness artifact has no results list"}
    ]


def test_dirty_harness_is_not_a_measured_clean_run(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False)
    payload = json.loads(matrix.read_text())
    payload["harness"]["repository_dirty"] = True
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([matrix])

    assert report["matrix"]["measured_clean_run_count"] == 0
    assert report["measures"]["automatic_recovery_time"]["status"] == "not_observed"


def test_report_has_self_provenance():
    provenance = MODULE.build_report([])["provenance"]

    assert provenance["git_sha"]
    assert provenance["sha256"]
    assert provenance["path"].endswith("scripts/qa/launch-reliability-measurements.py")
    assert isinstance(provenance["argv"], list)


def test_full_run_requires_exact_two_launches_per_provider(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False)
    payload = json.loads(matrix.read_text())
    payload["providers"] = payload["providers"][:-1]
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([matrix])

    assert report["matrix"]["full_run_count"] == 0
    assert report["matrix"]["excluded_full_runs"] == []


def test_health_fault_matrix_measures_controlled_false_red_and_action_coverage(tmp_path: Path):
    matrix = tmp_path / "health.json"
    matrix.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {"health_state": "broken", "suggested_action_ids": ["inspect_local_health"]},
                    },
                    {
                        "expected": {"state": "degraded", "action": "inspect_storage_source"},
                        "observed": {"health_state": "healthy", "suggested_action_ids": []},
                    },
                    {
                        "expected": {"state": "healthy", "action": "none"},
                        "observed": {"health_state": "broken", "suggested_action_ids": []},
                    },
                ]
            }
        )
    )

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["health_fault_matrix"]["case_count"] == 3
    assert report["measures"]["false_red_rate"] == {
        "status": "observed",
        "scope": "installed_health_fault_matrix",
        "basis": "fault_matrix_expected_state",
        "numerator": 1,
        "denominator": 2,
        "numerator_definition": "observed_broken_cases_expected_not_broken",
        "denominator_definition": "observed_broken_cases",
        "rate": 0.5,
        "source": [{"path": str(matrix), "sha256": MODULE._sha256(matrix)}],
    }
    assert report["measures"]["hidden_failure_rate"]["numerator"] == 1
    assert report["measures"]["hidden_failure_rate"]["denominator"] == 2
    assert report["measures"]["hidden_failure_rate"]["denominator_definition"] == "expected_broken_or_degraded_cases"
    assert report["measures"]["action_coverage"]["numerator"] == 1
    assert report["measures"]["action_coverage"]["denominator"] == 2
    assert report["measures"]["action_coverage"]["denominator_definition"] == "cases_with_expected_action"
