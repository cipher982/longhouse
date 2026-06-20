#!/usr/bin/env python3
"""Smoke tests for provider release-proof Make entrypoints."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_make(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_provider_release_proof_make_requires_provider() -> None:
    result = _run_make(["provider-release-proof"])

    assert result.returncode == 2
    assert "PROVIDER is required" in result.stderr


def test_provider_release_proof_status_make_requires_provider_and_scenario() -> None:
    missing_provider = _run_make(["provider-release-proof-status"])

    assert missing_provider.returncode == 2
    assert "PROVIDER is required" in missing_provider.stderr

    missing_scenario = _run_make(["provider-release-proof-status", "PROVIDER=opencode"])

    assert missing_scenario.returncode == 2
    assert "SCENARIO_ID is required" in missing_scenario.stderr


def test_provider_release_proof_old_new_make_requires_old_and_new() -> None:
    missing_old = _run_make(["provider-release-proof-old-new"])

    assert missing_old.returncode == 2
    assert "OLD is required" in missing_old.stderr

    missing_new = _run_make(["provider-release-proof-old-new", "OLD=/tmp/old.json"])

    assert missing_new.returncode == 2
    assert "NEW is required" in missing_new.stderr


def test_provider_release_proof_staged_old_new_make_requires_provider_and_bins() -> (
    None
):
    missing_provider = _run_make(["provider-release-proof-staged-old-new"])

    assert missing_provider.returncode == 2
    assert "PROVIDER is required" in missing_provider.stderr

    missing_old = _run_make(
        ["provider-release-proof-staged-old-new", "PROVIDER=opencode"]
    )

    assert missing_old.returncode == 2
    assert "OLD_PROVIDER_BIN is required" in missing_old.stderr

    missing_new = _run_make(
        [
            "provider-release-proof-staged-old-new",
            "PROVIDER=opencode",
            "OLD_PROVIDER_BIN=/tmp/old-opencode",
        ]
    )

    assert missing_new.returncode == 2
    assert "NEW_PROVIDER_BIN is required" in missing_new.stderr


def test_provider_release_proof_status_all_make_reports_inventory_missing_baseline() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        coverage = root / "coverage.json"
        artifact = root / "status-all.json"
        coverage.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "providers": ["opencode"],
                    "surfaces": [],
                    "rows": [],
                    "accepted_release_proof_scenarios": [
                        {
                            "provider": "opencode",
                            "scenario_id": "opencode-release-proof-v1",
                            "provider_version": "opencode 1.16.2",
                            "baseline_scope": "no_token_server_api_control_shape",
                            "baseline_boundary": "live_no_token",
                            "promoted_to_sauron": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = _run_make(
            [
                "provider-release-proof-status-all",
                f"COVERAGE={coverage}",
                f"BASELINE_ROOT={root / 'baselines'}",
                f"ARTIFACT={artifact}",
            ]
        )

        assert result.returncode == 2, result.stderr
        payload = _read_json(artifact)
        assert payload["artifact_kind"] == "provider_release_proof_baseline_status_all"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "accepted_baseline_inventory_incomplete"
        assert payload["statuses"][0]["failure_code"] == "baseline_missing"


def test_provider_release_proof_maturity_make_emits_rollup() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        coverage = root / "coverage.json"
        artifact = root / "maturity.json"
        coverage.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "providers": ["opencode"],
                    "surfaces": ["binary identity", "send input"],
                    "rows": [
                        {
                            "provider": "opencode",
                            "surface": "binary identity",
                            "covered": "yes",
                            "runs_in_ci": True,
                            "runs_in_sauron_release_watch": True,
                            "accepted_baseline": "none",
                            "baseline_scenarios": [],
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
                    ],
                    "accepted_release_proof_scenarios": [],
                }
            ),
            encoding="utf-8",
        )

        result = _run_make(
            [
                "provider-release-proof-maturity",
                f"COVERAGE={coverage}",
                f"BASELINE_ROOT={root / 'baselines'}",
                f"ARTIFACT={artifact}",
            ]
        )

        assert result.returncode == 0, result.stderr
        payload = _read_json(artifact)
        assert payload["artifact_kind"] == "provider_release_proof_maturity_rollup"
        assert payload["overall"]["weighted_percent"] == 75.0
        assert payload["provider_rollups"]["opencode"]["weighted_percent"] == 75.0
        assert payload["accepted_baselines"]["status"] == "checked"


def test_provider_release_proof_universal_smoke_make_emits_all_provider_artifact() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        artifact = root / "universal-smoke.json"
        evidence_root = root / "evidence"

        result = _run_make(
            [
                "provider-release-proof-universal-smoke",
                f"ARTIFACT={artifact}",
                f"EVIDENCE_ROOT={evidence_root}",
                "UNIVERSAL_SCENARIO=adapter_conformance action_matrix control_surface full_action_suite",
            ]
        )

        assert result.returncode == 0, result.stderr
        payload = _read_json(artifact)
        assert payload["artifact_kind"] == "provider_release_proof_universal_smoke"
        assert payload["verdict"] == "yellow"
        assert payload["providers"] == ["claude", "codex", "opencode", "antigravity"]
        assert payload["scenarios"] == [
            "adapter_conformance",
            "action_matrix",
            "control_surface",
            "full_action_suite",
        ]
        assert payload["result_count"] == 16
        assert Path(payload["universal_harness_artifact"]).is_file()
        assert Path(payload["provider_support_matrix_path"]).is_file()
        assert Path(payload["provider_execution_coverage_matrix_path"]).is_file()
        assert Path(payload["maturity_rollup_path"]).is_file()
        assert payload["maturity_rollup"]["status"] == "pass"
        assert (
            payload["maturity_rollup"]["universal_harness"][
                "execution_coverage_pass_percent"
            ]
            > 0.0
        )
        support_matrix = payload["provider_support_matrix"]
        assert support_matrix["action_count"] > 20
        assert support_matrix["missing_provider_actions"] == []
        execution_matrix = payload["provider_execution_coverage_matrix"]
        assert (
            execution_matrix["artifact_kind"]
            == "universal_agent_harness_provider_execution_coverage_matrix"
        )
        assert execution_matrix["action_count"] == support_matrix["action_count"]
        assert execution_matrix["missing_provider_actions"] == []


def test_provider_release_proof_universal_smoke_default_runs_managed_session_e2e() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        artifact = root / "universal-smoke.json"
        evidence_root = root / "evidence"

        result = _run_make(
            [
                "provider-release-proof-universal-smoke",
                f"ARTIFACT={artifact}",
                f"EVIDENCE_ROOT={evidence_root}",
            ]
        )

        assert result.returncode == 0, result.stderr
        payload = _read_json(artifact)
        assert payload["verdict"] == "yellow"
        assert "managed_session_e2e" in payload["scenarios"]
        assert "old_new_release_diff" in payload["scenarios"]
        assert set(payload["synthetic_old_proof_paths"]) == set(payload["providers"])
        assert set(payload["synthetic_new_proof_paths"]) == set(payload["providers"])
        assert payload["result_count"] == len(payload["providers"]) * len(
            payload["scenarios"]
        )

        universal = _read_json(Path(payload["universal_harness_artifact"]))
        old_new_results = {
            row["provider"]: row
            for row in universal["results"]
            if row["scenario"] == "old_new_release_diff"
        }
        assert set(old_new_results) == set(payload["providers"])
        for provider, row in old_new_results.items():
            assert row["status"] == "pass"
            assert row["data"]["provider_release_proof_old_new_verdict"] == "green"
            assert row["data"]["old_proof_uri"] == str(
                Path(payload["synthetic_old_proof_paths"][provider]).resolve()
            )

        managed_results = {
            row["provider"]: row
            for row in universal["results"]
            if row["scenario"] == "managed_session_e2e"
        }
        assert set(managed_results) == {
            "claude",
            "codex",
            "opencode",
            "antigravity",
        }
        assert managed_results["claude"]["status"] == "pass"
        assert managed_results["opencode"]["status"] == "pass"
        assert managed_results["antigravity"]["status"] == "pass"
        assert managed_results["codex"]["status"] == "unsupported_gap"
        assert (
            managed_results["codex"]["failure_code"]
            == "codex_managed_bridge_credentials_missing"
        )
        execution_rows = {
            row["action_id"]: row
            for row in payload["provider_execution_coverage_matrix"]["actions"]
        }
        assert execution_rows["old_new_release_diff"]["coverage_status_counts"] == {
            "pass": len(payload["providers"])
        }


def test_provider_release_proof_make_rejects_yellow_acceptance_and_keeps_diff_yellow() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "proof.json"
        evidence = root / "evidence"
        baseline_root = root / "baselines"
        acceptance = root / "acceptance.json"
        diff = root / "diff.json"
        status = root / "status.json"

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=codex",
                "PROVIDER_VERSION=codex-test-0",
                "CODEX_API_URL=http://longhouse.test",
                "CODEX_AGENTS_TOKEN=secret-token",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["artifact_kind"] == "provider_release_proof"
        assert proof_payload["provider"] == "codex"
        assert proof_payload["provider_version"] == "codex-test-0"
        assert proof_payload["verdict"] == "yellow"
        assert proof_payload["failure_code"] == "insufficient_coverage"
        assert Path(proof_payload["artifacts"]["source_artifact"]).exists()

        accept_result = _run_make(
            [
                "provider-release-proof-accept",
                f"PROOF={proof}",
                f"BASELINE_ROOT={baseline_root}",
                f"ARTIFACT={acceptance}",
            ]
        )

        assert accept_result.returncode == 2, accept_result.stderr
        accept_payload = _read_json(acceptance)
        assert (
            accept_payload["artifact_kind"]
            == "provider_release_proof_baseline_acceptance"
        )
        assert accept_payload["verdict"] == "red"
        assert accept_payload["failure_code"] == "baseline_acceptance_rejected"
        assert accept_payload["accepted_path"] is None

        diff_result = _run_make(
            [
                "provider-release-proof-diff",
                f"CANDIDATE={proof}",
                f"BASELINE_ROOT={baseline_root}",
                f"ARTIFACT={diff}",
            ]
        )

        assert diff_result.returncode == 0, diff_result.stderr
        diff_payload = _read_json(diff)
        assert diff_payload["artifact_kind"] == "provider_release_proof_diff"
        assert diff_payload["verdict"] == "yellow"
        assert diff_payload["failure_code"] == "insufficient_coverage"
        assert diff_payload["diff"]["status"] == "not_compared"

        status_result = _run_make(
            [
                "provider-release-proof-status",
                "PROVIDER=codex",
                "SCENARIO_ID=codex-release-proof-v1",
                f"BASELINE_ROOT={baseline_root}",
                f"ARTIFACT={status}",
            ]
        )

        assert status_result.returncode == 0, status_result.stderr
        status_payload = _read_json(status)
        assert (
            status_payload["artifact_kind"] == "provider_release_proof_baseline_status"
        )
        assert status_payload["accepted"] is False
        assert status_payload["failure_code"] == "baseline_missing"


def test_provider_release_proof_make_passes_scenario_id_override() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "proof.json"
        evidence = root / "evidence"

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=codex",
                "PROVIDER_VERSION=codex-test-0",
                "SOURCE_REVIEW_STATUS=pass",
                "SCENARIO_ID=codex-custom-proof-v1",
                "PREFLIGHT_ONLY=1",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["scenario_id"] == "codex-custom-proof-v1"
        assert proof_payload["scenario_profile"] == "default"


def test_provider_release_proof_make_runs_preflight_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "preflight.json"
        evidence = root / "evidence"

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=codex",
                "PROVIDER_VERSION=codex-test-0",
                "CODEX_RUN_MANAGED_LIVE_SEND=1",
                "PREFLIGHT_ONLY=1",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["artifact_kind"] == "provider_release_proof_preflight"
        assert (
            proof_payload["scenario_id"] == "codex-managed-live-send-release-proof-v1"
        )
        assert (
            proof_payload["failure_code"]
            == "provider_release_proof_prerequisites_missing"
        )
        assert not (evidence / "raw").exists()


def test_provider_release_proof_make_forwards_codex_live_interrupt_profile() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "preflight.json"
        evidence = root / "evidence"

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=codex",
                "PROVIDER_VERSION=codex-test-0",
                "CODEX_RUN_MANAGED_LIVE_INTERRUPT=1",
                "CODEX_LIVE_INTERRUPT_TIMEOUT_SECS=7",
                "PREFLIGHT_ONLY=1",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["artifact_kind"] == "provider_release_proof_preflight"
        assert (
            proof_payload["scenario_id"]
            == "codex-managed-live-interrupt-release-proof-v1"
        )
        assert proof_payload["scenario_profile"] == "managed-live-interrupt"
        assert (
            proof_payload["failure_code"]
            == "provider_release_proof_prerequisites_missing"
        )
        assert not (evidence / "raw").exists()


def test_provider_release_proof_make_forwards_claude_real_print_profile() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "preflight.json"
        evidence = root / "evidence"
        fake_provider = root / "fake-claude"
        fake_provider.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_provider.chmod(0o755)

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=claude",
                f"PROVIDER_BIN={fake_provider}",
                "PROVIDER_VERSION=Claude Code 2.1.test",
                "CLAUDE_RUN_REAL_PRINT=1",
                "CLAUDE_PRINT_TIMEOUT_SECS=5",
                "PREFLIGHT_ONLY=1",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["artifact_kind"] == "provider_release_proof_preflight"
        assert proof_payload["scenario_id"] == "claude-real-print-release-proof-v1"
        assert proof_payload["scenario_profile"] == "real-print"
        assert proof_payload["verdict"] == "green"
        assert not (evidence / "raw").exists()


def main() -> int:
    tests = [
        test_provider_release_proof_make_requires_provider,
        test_provider_release_proof_status_make_requires_provider_and_scenario,
        test_provider_release_proof_old_new_make_requires_old_and_new,
        test_provider_release_proof_staged_old_new_make_requires_provider_and_bins,
        test_provider_release_proof_status_all_make_reports_inventory_missing_baseline,
        test_provider_release_proof_maturity_make_emits_rollup,
        test_provider_release_proof_universal_smoke_make_emits_all_provider_artifact,
        test_provider_release_proof_universal_smoke_default_runs_managed_session_e2e,
        test_provider_release_proof_make_rejects_yellow_acceptance_and_keeps_diff_yellow,
        test_provider_release_proof_make_passes_scenario_id_override,
        test_provider_release_proof_make_runs_preflight_only,
        test_provider_release_proof_make_forwards_codex_live_interrupt_profile,
        test_provider_release_proof_make_forwards_claude_real_print_profile,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
