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
    payload = {
        "artifact_kind": "installed_managed_launch_fault_matrix",
        "generated_at": "2026-08-04T16:00:00Z",
        "verdict": verdict,
        "providers": providers,
        "provider_auth": auth,
        "provider_startup_failures": (
            [{"provider": "claude", "qualification": "provider_owned_start_failure"}] if provider_failure else []
        ),
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
    assert matrix["cleanup_pass_rate"] == 1.0
    assert len(matrix["auth_precondition_runs"]) == 1
    assert len(matrix["provider_owned_failure_runs"]) == 1
    assert matrix["history"][0]["startup_failures"]["harness_precondition"] == 0
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
                        "status": "pass",
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

    assert report["provider_scenarios"]["result_status_counts"] == {"pass": 1}
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
