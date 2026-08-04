from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/qa/launch-reliability-measurements.py"
SPEC = importlib.util.spec_from_file_location("launch_reliability_measurements", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HEALTH_CASES = tuple(MODULE.HEALTH_EXPECTATIONS)


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


def _dogfood_series(path: Path, *, duplicate_ids: bool = False, recovery: object = None) -> None:
    episodes = []
    for index, observed_at in enumerate(
        ("2026-08-04T12:00:00Z", "2026-08-04T12:30:00Z", "2026-08-04T13:00:00Z")
    ):
        episodes.append(
            {
                "episode_id": "same" if duplicate_ids else f"episode-{index}",
                "observed_at": observed_at,
                "recovery_duration_seconds": index + 1 if recovery is None else recovery,
                "expected": {
                    "health_state": "broken",
                    "red_eligible": True,
                    "producer_freshness": "stale",
                    "action": "inspect_local_health",
                },
                "observed": {
                    "health_state": "broken",
                    "producer_freshness": "stale",
                    "suggested_action_ids": ["inspect_local_health"],
                },
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "launch_reliability_dogfood_series",
                "generated_at": "2026-08-04T13:00:30Z",
                "provenance": {
                    "generator": "test-fixture",
                    "git_sha": "0" * 40,
                    "repository_dirty": False,
                },
                "episodes": episodes,
            }
        )
    )


def _health_artifact(results: list[dict[str, object]]) -> dict[str, object]:
    overrides = {}
    normalized_results = []
    for index, result in enumerate(results):
        normalized = dict(result)
        normalized["case"] = normalized.get("case") or HEALTH_CASES[index]
        case = normalized["case"]
        contract = MODULE.HEALTH_EXPECTATIONS[case]
        expected = dict(normalized["expected"])
        for field, value in contract.items():
            expected.setdefault(field, value)
        normalized["expected"] = expected
        observed = dict(normalized["observed"])
        observed["collected_at"] = observed.get("collected_at") or "2026-08-04T13:00:00Z"
        normalized["observed"] = observed
        overrides[case] = normalized
    for case, contract in MODULE.HEALTH_EXPECTATIONS.items():
        if case in overrides:
            normalized_results.append(overrides[case])
            continue
        normalized_results.append(
            {
                "case": case,
                "expected": dict(contract),
                "observed": {
                    "collected_at": "2026-08-04T13:00:00Z",
                    "health_state": contract["state"],
                    "suggested_action_ids": [] if contract["action"] == "none" else [contract["action"]],
                },
            }
        )
    return {
        "artifact_kind": "installed_native_health_fault_matrix",
        "schema_version": 1,
        "generated_at": "2026-08-04T13:00:30Z",
        "verdict": "green",
        "implementation": {"sha256": ("abcdef" * 10) + "abcd", "returncode": 0},
        "scenarios": list(HEALTH_CASES),
        "results": normalized_results,
    }


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


def test_repeated_provider_harness_artifact_is_counted_once(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False)
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
                            }
                        },
                    }
                ]
            }
        )
    )
    harness_copy = tmp_path / "harness-copy.json"
    harness_copy.write_bytes(harness.read_bytes())

    report = MODULE.build_report([matrix], [harness, harness_copy, harness])

    assert report["provider_scenarios"]["result_status_counts"] == {"pass": 1}
    assert report["provider_scenarios"]["operation_status_counts"] == {"pass": 1}
    assert len(report["inputs"]["provider_harness_artifacts"]) == 1


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
            _health_artifact(
                [
                    {
                        "case": "missing_engine_status",
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {"health_state": "broken", "suggested_action_ids": ["inspect_local_health"]},
                    },
                    {
                        "case": "stale_projection",
                        "expected": {"state": "degraded", "action": "inspect_local_health"},
                        "observed": {"health_state": "healthy", "suggested_action_ids": ["inspect_local_health"]},
                    },
                    {
                        "case": "managed_launch_recovery_resolved",
                        "expected": {"state": "healthy", "action": "none"},
                        "observed": {"health_state": "broken", "suggested_action_ids": []},
                    },
                ]
            )
        )
    )

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["health_fault_matrix"]["case_count"] == 10
    assert report["measures"]["false_red_rate"] == {
        "status": "observed",
        "scope": "installed_health_fault_matrix",
        "basis": "fault_matrix_expected_state",
        "numerator": 1,
        "denominator": 7,
        "numerator_definition": "observed_broken_cases_expected_not_broken",
        "denominator_definition": "observed_broken_cases",
        "rate": 1 / 7,
        "source": [{"path": str(matrix), "sha256": MODULE._sha256(matrix)}],
    }
    assert report["measures"]["hidden_failure_rate"]["numerator"] == 1
    assert report["measures"]["hidden_failure_rate"]["denominator"] == 9
    assert report["measures"]["hidden_failure_rate"]["numerator_definition"] == "observed_healthy_expected_broken_or_degraded"
    assert report["measures"]["hidden_failure_rate"]["denominator_definition"] == "expected_broken_or_degraded_cases"
    assert report["measures"]["action_coverage"]["numerator"] == 9
    assert report["measures"]["action_coverage"]["denominator"] == 9
    assert report["measures"]["action_coverage"]["denominator_definition"] == "cases_with_a_non_none_expected_action"


def test_health_fault_matrix_marks_zero_observed_broken_cases_not_observed(tmp_path: Path):
    matrix = tmp_path / "health.json"
    payload = _health_artifact(
        [
            {
                "case": "managed_launch_recovery_resolved",
                "expected": {"state": "healthy", "action": "none"},
                "observed": {"health_state": "healthy", "suggested_action_ids": []},
            }
        ]
    )
    for result in payload["results"]:
        result["observed"]["health_state"] = "healthy"
        result["observed"]["suggested_action_ids"] = []
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["measures"]["false_red_rate"]["status"] == "not_observed"
    assert report["measures"]["false_red_rate"]["reason"] == "no observed broken cases in supplied health fault matrix"
    assert report["measures"]["hidden_failure_rate"]["status"] == "observed"
    assert report["measures"]["hidden_failure_rate"]["numerator"] == 9
    assert report["measures"]["hidden_failure_rate"]["denominator"] == 9
    assert report["measures"]["action_coverage"]["status"] == "observed"
    assert report["measures"]["action_coverage"]["numerator"] == 0
    assert report["measures"]["action_coverage"]["denominator"] == 9


def test_repeated_health_artifact_is_counted_once(tmp_path: Path):
    matrix = tmp_path / "health.json"
    matrix.write_text(
        json.dumps(
            _health_artifact(
                [
                    {
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {
                            "health_state": "broken",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                    }
                ]
            )
        )
    )
    matrix_copy = tmp_path / "health-copy.json"
    matrix_copy.write_bytes(matrix.read_bytes())

    report = MODULE.build_report([], health_paths=[matrix, matrix_copy, matrix])

    assert report["health_fault_matrix"]["case_count"] == 10
    assert report["measures"]["false_red_rate"]["denominator"] == 6


def test_reserialized_health_artifact_is_counted_once(tmp_path: Path):
    matrix = tmp_path / "health.json"
    matrix.write_text(
        json.dumps(
            _health_artifact(
                [
                    {
                        "case": "missing_engine_status",
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {
                            "health_state": "broken",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                    }
                ]
            )
        )
    )
    matrix_copy = tmp_path / "health-reformatted.json"
    reformatted = json.loads(matrix.read_text())
    reformatted["results"] = list(reversed(reformatted["results"]))
    reformatted["scenarios"] = list(reversed(reformatted["scenarios"]))
    matrix_copy.write_text(json.dumps(reformatted, indent=2))

    report = MODULE.build_report([], health_paths=[matrix, matrix_copy])

    assert report["health_fault_matrix"]["case_count"] == 10
    assert report["measures"]["false_red_rate"]["denominator"] == 6


def test_health_artifact_requires_every_declared_scenario(tmp_path: Path):
    matrix = tmp_path / "truncated-health.json"
    payload = _health_artifact(
        [
            {
                "case": "missing_engine_status",
                "expected": {"state": "broken", "action": "inspect_local_health"},
                "observed": {
                    "health_state": "broken",
                    "suggested_action_ids": ["inspect_local_health"],
                },
            },
            {
                "case": "stale_projection",
                "expected": {"state": "degraded", "action": "inspect_local_health"},
                "observed": {"health_state": "degraded", "suggested_action_ids": []},
            },
        ]
    )
    payload["results"] = payload["results"][:1]
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["report_status"] == "invalid"
    assert "cover every declared scenario" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["health_fault_matrix"]["case_count"] == 0


def test_health_artifact_rejects_subset_scenario_manifest(tmp_path: Path):
    matrix = tmp_path / "subset-health.json"
    payload = _health_artifact(
        [
            {
                "case": "missing_engine_status",
                "expected": {"state": "broken", "action": "inspect_local_health"},
                "observed": {
                    "health_state": "broken",
                    "suggested_action_ids": ["inspect_local_health"],
                },
            }
        ]
    )
    payload["scenarios"] = ["missing_engine_status"]
    payload["results"] = payload["results"][:1]
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["report_status"] == "invalid"
    assert "canonical fault matrix" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["health_fault_matrix"]["case_count"] == 0


def test_conflicting_health_artifact_identity_invalidates_the_batch(tmp_path: Path):
    first = tmp_path / "first-health.json"
    second = tmp_path / "conflicting-health.json"
    payload = _health_artifact(
        [
            {
                "case": "missing_engine_status",
                "expected": {"state": "broken", "action": "inspect_local_health"},
                "observed": {
                    "health_state": "broken",
                    "suggested_action_ids": ["inspect_local_health"],
                },
            }
        ]
    )
    first.write_text(json.dumps(payload))
    payload["results"][0]["observed"]["health_state"] = "healthy"
    second.write_text(json.dumps(payload))

    report = MODULE.build_report([], health_paths=[first, second])

    assert report["report_status"] == "invalid"
    assert "conflicts with an existing run identity" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["health_fault_matrix"]["case_count"] == 0


def test_stale_health_observation_invalidates_the_artifact(tmp_path: Path):
    matrix = tmp_path / "stale-health.json"
    payload = _health_artifact(
        [
            {
                "case": "missing_engine_status",
                "expected": {"state": "broken", "action": "inspect_local_health"},
                "observed": {
                    "health_state": "broken",
                    "suggested_action_ids": ["inspect_local_health"],
                    "collected_at": "2026-08-04T12:00:00Z",
                },
            }
        ]
    )
    matrix.write_text(json.dumps(payload))

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["report_status"] == "invalid"
    assert "older than 300 seconds" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["health_fault_matrix"]["case_count"] == 0


def test_malformed_health_label_invalidates_the_entire_artifact(tmp_path: Path):
    matrix = tmp_path / "health.json"
    matrix.write_text(
        json.dumps(
            _health_artifact(
                [
                    {
                        "expected": {"state": "not-a-health-state", "action": "none"},
                        "observed": {"health_state": "healthy", "suggested_action_ids": []},
                    },
                    {
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {
                            "health_state": "healthy",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                    },
                ]
            )
        )
    )

    report = MODULE.build_report([], health_paths=[matrix])

    assert report["report_status"] == "invalid"
    assert report["health_fault_matrix"]["case_count"] == 0
    assert report["measures"]["hidden_failure_rate"]["status"] == "not_observed"
    assert report["inputs"]["health_artifacts"] == []
    assert report["inputs"]["invalid_artifacts"] == [
        {"path": str(matrix), "error": "health result 0.expected.state is invalid"}
    ]


def test_invalid_health_sibling_scrubs_valid_health_measures(tmp_path: Path):
    valid = tmp_path / "valid-health.json"
    valid.write_text(
        json.dumps(
            _health_artifact(
                [
                    {
                        "case": "missing_engine_status",
                        "expected": {"state": "broken", "action": "inspect_local_health"},
                        "observed": {
                            "health_state": "broken",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                    }
                ]
            )
        )
    )
    invalid = tmp_path / "invalid-health.json"
    invalid.write_text(
        json.dumps(
            _health_artifact(
                [
                    {
                        "case": "missing_engine_status",
                        "expected": {"state": "invalid", "action": "none"},
                        "observed": {"health_state": "healthy", "suggested_action_ids": []},
                    }
                ]
            )
        )
    )

    report = MODULE.build_report([], health_paths=[valid, invalid])

    assert report["report_status"] == "invalid"
    assert report["health_fault_matrix"]["case_count"] == 0
    for metric in report["health_fault_matrix"]["measurements"].values():
        assert metric["status"] == "not_observed"
        assert metric["source"] == []
        assert metric["numerator"] is None
        assert metric["denominator"] is None
        assert metric["rate"] is None


def test_repeated_matrix_content_is_counted_once(tmp_path: Path):
    matrix = tmp_path / "matrix.json"
    _matrix(path=matrix, auth_gap=False)
    matrix_copy = tmp_path / "matrix-copy.json"
    matrix_copy.write_bytes(matrix.read_bytes())

    report = MODULE.build_report([matrix, matrix_copy])

    assert report["matrix"]["full_run_count"] == 1
    assert report["matrix"]["measured_clean_run_count"] == 1
    assert report["measures"]["automatic_recovery_time"]["sample_count"] == 1


def test_dogfood_series_supplies_longitudinal_measures(tmp_path: Path):
    series = tmp_path / "dogfood.json"
    series.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "launch_reliability_dogfood_series",
                "generated_at": "2026-08-04T13:00:30Z",
                "provenance": {
                    "generator": "test-fixture",
                    "git_sha": "0" * 40,
                    "repository_dirty": False,
                },
                "episodes": [
                    {
                        "episode_id": "episode-1",
                        "observed_at": "2026-08-04T12:00:10Z",
                        "recovery_duration_seconds": 10.0,
                        "expected": {
                            "health_state": "degraded",
                            "red_eligible": False,
                            "producer_freshness": "fresh",
                            "action": "inspect_storage_source",
                        },
                        "observed": {
                            "health_state": "degraded",
                            "producer_freshness": "fresh",
                            "suggested_action_ids": ["inspect_storage_source"],
                        },
                        "event_bearing_issue": {
                            "status": "resolved",
                            "opened_at": "2026-08-04T12:00:00Z",
                            "resolved_at": "2026-08-04T12:00:08Z",
                        },
                        "evidence_conservation": {
                            "source_events": 4,
                            "archived_events": 3,
                            "replayed_events": 1,
                            "duplicate_events": 1,
                            "discarded_events": 0,
                            "unresolved_events": 1,
                        },
                    },
                    {
                        "episode_id": "episode-2",
                        "observed_at": "2026-08-04T12:30:10Z",
                        "recovery_duration_seconds": 12.0,
                        "expected": {
                            "health_state": "broken",
                            "red_eligible": True,
                            "producer_freshness": "stale",
                            "action": "inspect_local_health",
                        },
                        "observed": {
                            "health_state": "broken",
                            "producer_freshness": "stale",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                        "event_bearing_issue": {
                            "status": "unresolved",
                            "opened_at": "2026-08-04T12:30:00Z",
                        },
                        "evidence_conservation": {
                            "source_events": 2,
                            "archived_events": 2,
                            "replayed_events": 0,
                            "duplicate_events": 0,
                            "discarded_events": 0,
                            "unresolved_events": 0,
                        },
                    },
                    {
                        "episode_id": "episode-3",
                        "observed_at": "2026-08-04T13:00:30Z",
                        "recovery_duration_seconds": 14.0,
                        "expected": {
                            "health_state": "broken",
                            "red_eligible": True,
                            "producer_freshness": "stale",
                            "action": "inspect_local_health",
                        },
                        "observed": {
                            "health_state": "broken",
                            "producer_freshness": "stale",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                        "event_bearing_issue": {
                            "status": "unresolved",
                            "opened_at": "2026-08-04T12:00:00Z",
                        },
                        "evidence_conservation": {
                            "source_events": 3,
                            "archived_events": 2,
                            "replayed_events": 1,
                            "duplicate_events": 1,
                            "discarded_events": 0,
                            "unresolved_events": 1,
                        },
                    },
                ],
            }
        )
    )

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "ok"
    assert report["dogfood_series"]["episode_count"] == 3
    assert report["dogfood_series"]["observation_window"]["status"] == "eligible"
    assert report["dogfood_series"]["automatic_recovery_time"]["sample_count"] == 3
    assert report["measures"]["false_red_rate"]["scope"] == "dogfood_episode_series"
    assert report["measures"]["false_red_rate"]["rate"] == 0.0
    assert report["measures"]["hidden_failure_rate"]["rate"] == 0.0
    assert report["measures"]["action_coverage"]["rate"] == 1.0
    assert report["measures"]["unresolved_event_bearing_issue_age"]["max_age_seconds"] == 3630.0
    conservation = report["measures"]["duplicate_replayed_discarded_evidence"]
    assert conservation["totals"] == {
        "archived_events": 7,
        "discarded_events": 0,
        "duplicate_events": 2,
        "replayed_events": 2,
        "source_events": 9,
        "unresolved_events": 2,
    }
    assert conservation["conservation_status"] == "complete"


def test_malformed_dogfood_series_fails_closed(tmp_path: Path):
    series = tmp_path / "dogfood.json"
    series.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "launch_reliability_dogfood_series",
                "generated_at": "2026-08-04T13:00:30Z",
                "provenance": {
                    "generator": "test-fixture",
                    "git_sha": "0" * 40,
                    "repository_dirty": False,
                },
                "episodes": [
                    {
                        "episode_id": "episode-1",
                        "observed_at": "2026-08-04T12:00:00Z",
                        "expected": {
                            "health_state": "broken",
                            "red_eligible": True,
                            "producer_freshness": "stale",
                            "action": "inspect_local_health",
                        },
                        "observed": {
                            "health_state": "broken",
                            "producer_freshness": "stale",
                            "suggested_action_ids": ["inspect_local_health"],
                        },
                        "evidence_conservation": {
                            "source_events": 1,
                            "archived_events": 2,
                            "replayed_events": 0,
                            "duplicate_events": 0,
                            "discarded_events": 0,
                            "unresolved_events": 0,
                        },
                    }
                ],
            }
        )
    )

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "invalid"
    assert "accounts for more events" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["dogfood_series"]["episode_count"] == 0


def test_duplicate_dogfood_path_is_counted_once(tmp_path: Path):
    series = tmp_path / "dogfood.json"
    _dogfood_series(series)

    report = MODULE.build_report([], dogfood_paths=[series, series])

    assert report["report_status"] == "ok"
    assert report["dogfood_series"]["episode_count"] == 3
    assert report["dogfood_series"]["input_status"] == "valid"
    assert report["measures"]["automatic_recovery_time"]["sample_count"] == 3


def test_duplicate_dogfood_episode_id_and_nonfinite_duration_fail_closed(tmp_path: Path):
    duplicate = tmp_path / "duplicate.json"
    _dogfood_series(duplicate, duplicate_ids=True)
    duplicate_report = MODULE.build_report([], dogfood_paths=[duplicate])
    assert duplicate_report["report_status"] == "invalid"
    assert "duplicate episode_id" in duplicate_report["inputs"]["invalid_artifacts"][0]["error"]
    assert duplicate_report["measures"]["automatic_recovery_time"]["status"] == "not_observed"

    nonfinite = tmp_path / "nonfinite.json"
    _dogfood_series(nonfinite, recovery=float("nan"))
    nonfinite_report = MODULE.build_report([], dogfood_paths=[nonfinite])
    assert nonfinite_report["report_status"] == "invalid"
    assert "must be non-negative" in nonfinite_report["inputs"]["invalid_artifacts"][0]["error"]


def test_discarded_evidence_is_visible_as_a_separate_conservation_state(tmp_path: Path):
    series = tmp_path / "discard.json"
    _dogfood_series(series)
    payload = json.loads(series.read_text())
    payload["episodes"][0]["evidence_conservation"] = {
        "source_events": 2,
        "archived_events": 1,
        "replayed_events": 0,
        "duplicate_events": 0,
        "discarded_events": 1,
        "unresolved_events": 0,
    }
    series.write_text(json.dumps(payload))

    report = MODULE.build_report([], dogfood_paths=[series])

    conservation = report["measures"]["duplicate_replayed_discarded_evidence"]
    assert conservation["conservation_status"] == "complete_with_discard"
    assert conservation["totals"]["discarded_events"] == 1


def test_future_issue_resolution_fails_closed(tmp_path: Path):
    series = tmp_path / "future-resolution.json"
    _dogfood_series(series)
    payload = json.loads(series.read_text())
    payload["episodes"][0]["event_bearing_issue"] = {
        "status": "resolved",
        "opened_at": "2026-08-04T11:59:00Z",
        "resolved_at": "2026-08-04T14:00:00Z",
    }
    series.write_text(json.dumps(payload))

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "invalid"
    assert "after observed_at" in report["inputs"]["invalid_artifacts"][0]["error"]


def test_episode_after_artifact_generation_fails_closed(tmp_path: Path):
    series = tmp_path / "future-episode.json"
    _dogfood_series(series)
    payload = json.loads(series.read_text())
    payload["generated_at"] = "2026-08-04T11:00:00Z"
    series.write_text(json.dumps(payload))

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "invalid"
    assert "after dogfood.generated_at" in report["inputs"]["invalid_artifacts"][0]["error"]


def test_truthless_dogfood_episode_fails_closed(tmp_path: Path):
    series = tmp_path / "truthless.json"
    _dogfood_series(series)
    payload = json.loads(series.read_text())
    for episode in payload["episodes"]:
        episode.pop("expected")
        episode.pop("observed")
    series.write_text(json.dumps(payload))

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "invalid"
    assert "must include expected and observed" in report["inputs"]["invalid_artifacts"][0]["error"]
    assert report["measures"]["false_red_rate"]["status"] == "not_observed"


def test_mixed_valid_and_invalid_dogfood_inputs_clear_numeric_claims(tmp_path: Path):
    valid = tmp_path / "valid.json"
    _dogfood_series(valid)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema_version": 1, "artifact_kind": "wrong"}))

    report = MODULE.build_report([], dogfood_paths=[valid, invalid])

    assert report["report_status"] == "invalid"
    assert report["dogfood_series"]["episode_count"] == 0
    assert report["dogfood_series"]["input_status"] == "invalid"
    assert report["measures"]["false_red_rate"]["status"] == "not_observed"
    assert report["dogfood_series"]["false_red_rate"]["rate"] is None
    assert report["dogfood_series"]["false_red_rate"]["denominator"] is None
    assert report["dogfood_series"]["false_red_rate"]["source"] == []


def test_duplicate_observation_timestamps_do_not_promote_series(tmp_path: Path):
    series = tmp_path / "same-time.json"
    _dogfood_series(series)
    payload = json.loads(series.read_text())
    payload["episodes"][1]["observed_at"] = payload["episodes"][0]["observed_at"]
    payload["episodes"][2]["observed_at"] = payload["episodes"][0]["observed_at"]
    series.write_text(json.dumps(payload))

    report = MODULE.build_report([], dogfood_paths=[series])

    assert report["report_status"] == "ok"
    assert report["dogfood_series"]["observation_window"]["distinct_observation_count"] == 1
    assert report["dogfood_series"]["observation_window"]["status"] == "insufficient"
    assert report["dogfood_series"]["false_red_rate"]["status"] == "not_observed"
    assert report["dogfood_series"]["false_red_rate"]["numerator"] is None
    assert report["dogfood_series"]["false_red_rate"]["denominator"] is None
    assert report["dogfood_series"]["false_red_rate"]["rate"] is None
    assert report["dogfood_series"]["automatic_recovery_time"]["sample_count"] is None
    assert report["dogfood_series"]["automatic_recovery_time"]["seconds"]["median"] is None
