#!/usr/bin/env python3
"""Tests for provider release-proof baseline acceptance and diffing."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "scripts" / "qa" / "provider-release-proof-baseline.py"


def _verdict_for_status(status: str) -> str:
    if status == "pass":
        return "green"
    if status == "warn":
        return "yellow"
    return "red"


def _failure_for_status(status: str) -> str | None:
    if status == "pass":
        return None
    if status == "warn":
        return "fake_warning"
    return "fake_drift"


def _write_proof(
    root: Path,
    name: str,
    *,
    status: str = "pass",
    version: str = "1.2.3",
    provider: str = "opencode",
    scenario_id: str = "opencode-release-proof-v1",
) -> Path:
    proof_dir = root / name
    artifact_dir = proof_dir / "evidence"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_artifact = artifact_dir / "source.json"
    stdout = artifact_dir / "stdout.log"
    stderr = artifact_dir / "stderr.log"
    normalized_contract = artifact_dir / "normalized" / "contract.json"
    provider_contract = artifact_dir / "normalized" / "provider_contract.json"
    operation_evidence_artifact = (
        artifact_dir / "normalized" / "operation_evidence.json"
    )
    session_projection = artifact_dir / "normalized" / "session_projection.json"
    action_matrix = artifact_dir / "normalized" / "action_matrix.json"
    control_surface = artifact_dir / "normalized" / "control_surface.json"
    provider_execution_coverage_matrix = (
        artifact_dir / "normalized" / "provider_execution_coverage_matrix.json"
    )
    action_rows = [
        {
            "action_id": "send_message",
            "category": "control",
            "status": status,
            "support": True,
            "support_reason": "contract.send_input",
            "required_evidence": "hermetic",
            "evidence_level": "live_no_token",
            "proof_scope": "managed_provider_contract",
            "contract_operation": "send_input",
            "canary": "server_contract",
            "failure_code": _failure_for_status(status),
            "raw_artifacts": [f"/tmp/{name}/volatile-action-path.json"],
        },
        {
            "action_id": "old_new_release_diff",
            "category": "release_diff",
            "status": "blocked",
            "support": True,
            "support_reason": "provider_release_proof",
            "required_evidence": "live_no_token",
            "proof_scope": "release_diff_runner",
            "failure_code": "old_new_release_runner_missing",
        },
    ]
    normalized = {
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "verdict": _verdict_for_status(status),
        "failure_code": _failure_for_status(status),
        "canaries": {"server_contract": {"status": status}},
        "operation_evidence": {
            "send_input": {
                "status": status,
                "level": "live_no_token",
                "canary": "server_contract",
            }
        },
    }
    provider_contract_payload = {
        "artifact_kind": "provider_release_proof_provider_contract",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "contract_operations": {
            "send_input": {
                "level": "live_no_token",
                "source": "fake server_contract",
            }
        },
    }
    operation_evidence_payload = {
        "artifact_kind": "provider_release_proof_operation_evidence",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "operation_evidence": normalized["operation_evidence"],
    }
    session_projection_payload = {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "projection": {
            "artifact_kind": "provider_live_session_projection",
            "provider": provider,
            "status": "captured",
            "provider_session_id": f"volatile-{name}-{version}",
            "classification_sidecar_path": f"/tmp/{name}/sidecar.json",
            "checks": {
                "session_create": {
                    "status": "pass",
                    "provider_session_id": f"volatile-{name}-{version}",
                    "elapsed_ms": 7,
                },
                "prompt_async_no_reply_delivery": {
                    "status": status,
                    "message_marker_sha256": f"volatile-{name}",
                    "elapsed_ms": 11,
                },
            },
            "operation_statuses": {
                "send_input": {
                    "status": status,
                    "level": "live_no_token",
                    "canary": "server_contract",
                }
            },
        },
    }
    action_matrix_payload = {
        "artifact_kind": "provider_release_proof_action_matrix",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "action_matrix": {
            "artifact_kind": "provider_release_proof_action_matrix",
            "provider": provider,
            "action_count": len(action_rows),
            "action_ids": [row["action_id"] for row in action_rows],
            "status_counts": {"blocked": 1, status: 1},
            "action_matrix_path": f"/tmp/{name}/volatile-action-matrix.json",
            "raw_inputs_path": f"/tmp/{name}/volatile-action-inputs.json",
            "actions": action_rows,
        },
    }
    control_surface_payload = {
        "artifact_kind": "provider_release_proof_control_surface",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "control_surface": {
            "artifact_kind": "provider_release_proof_control_surface",
            "provider": provider,
            "action_count": 1,
            "action_ids": ["send_message"],
            "status_counts": {status: 1},
            "control_surface_path": f"/tmp/{name}/volatile-control-surface.json",
            "raw_inputs_path": f"/tmp/{name}/volatile-control-inputs.json",
            "actions": [action_rows[0]],
        },
    }
    execution_coverage_rows = [
        {
            "action_id": "send_message",
            "category": "control",
            "contract_operation": "send_input",
            "required_evidence": "hermetic",
            "coverage_kind": "executable_scenario",
            "coverage_status": status,
            "failure_code": _failure_for_status(status),
            "matrix_status": status,
            "matrix_support": True,
            "matrix_support_reason": "contract.send_input",
            "scenario_ids": ["managed_session_e2e"],
            "scenario_statuses": {"managed_session_e2e": status},
            "coverage_policy": "scenario_or_matrix",
        },
        {
            "action_id": "old_new_release_diff",
            "category": "release_diff",
            "required_evidence": "live_no_token",
            "coverage_kind": "matrix_contract",
            "coverage_status": "blocked",
            "failure_code": "old_new_release_runner_missing",
            "matrix_status": "blocked",
            "matrix_failure_code": "old_new_release_runner_missing",
            "matrix_support": True,
            "matrix_support_reason": "provider_release_proof",
            "coverage_policy": "matrix_only",
        },
    ]
    provider_execution_coverage_matrix_payload = {
        "artifact_kind": "provider_release_proof_provider_execution_coverage_matrix",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "status": "captured",
        "provider_execution_coverage_matrix": {
            "artifact_kind": "provider_release_proof_provider_execution_coverage_matrix",
            "provider": provider,
            "action_count": len(execution_coverage_rows),
            "coverage_status_counts": {"blocked": 1, status: 1},
            "coverage_kind_counts": {
                "executable_scenario": 1,
                "matrix_contract": 1,
            },
            "required_evidence_rollup": {
                "hermetic": {
                    "cell_count": 1,
                    "pass_count": 1 if status == "pass" else 0,
                    "pass_percent": 100.0 if status == "pass" else 0.0,
                    "coverage_status_counts": {status: 1},
                    "coverage_kind_counts": {"executable_scenario": 1},
                },
                "live_no_token": {
                    "cell_count": 1,
                    "pass_count": 0,
                    "pass_percent": 0.0,
                    "coverage_status_counts": {"blocked": 1},
                    "coverage_kind_counts": {"matrix_contract": 1},
                },
            },
            "execution_coverage_matrix_path": f"/tmp/{name}/volatile-execution-coverage.json",
            "actions": execution_coverage_rows,
        },
    }
    source_artifact.write_text(json.dumps({"raw": True}), encoding="utf-8")
    stdout.write_text("stdout\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    normalized_contract.parent.mkdir(parents=True, exist_ok=True)
    normalized_contract.write_text(json.dumps(normalized), encoding="utf-8")
    provider_contract.write_text(
        json.dumps(provider_contract_payload), encoding="utf-8"
    )
    operation_evidence_artifact.write_text(
        json.dumps(operation_evidence_payload), encoding="utf-8"
    )
    session_projection.write_text(
        json.dumps(session_projection_payload), encoding="utf-8"
    )
    action_matrix.write_text(json.dumps(action_matrix_payload), encoding="utf-8")
    control_surface.write_text(json.dumps(control_surface_payload), encoding="utf-8")
    provider_execution_coverage_matrix.write_text(
        json.dumps(provider_execution_coverage_matrix_payload), encoding="utf-8"
    )
    proof = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof",
        "provider": provider,
        "provider_version": f"{provider} {version}",
        "scenario_id": scenario_id,
        "scenario_version": 1,
        "verdict": _verdict_for_status(status),
        "failure_code": _failure_for_status(status),
        "normalized": normalized,
        "artifacts": {
            "source_artifact": str(source_artifact),
            "stdout": str(stdout),
            "stderr": str(stderr),
            "normalized_contract": str(normalized_contract),
            "provider_contract": str(provider_contract),
            "operation_evidence": str(operation_evidence_artifact),
            "session_projection": str(session_projection),
            "action_matrix": str(action_matrix),
            "control_surface": str(control_surface),
            "provider_execution_coverage_matrix": str(
                provider_execution_coverage_matrix
            ),
        },
    }
    proof_path = proof_dir / "proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    return proof_path


def _run(args: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = (
        Path(args[args.index("--artifact") + 1]) if "--artifact" in args else None
    )
    result = subprocess.run(
        [sys.executable, str(BASELINE), *args, "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    if artifact is not None:
        assert artifact.exists()
    return result, payload


def _write_coverage_inventory(root: Path, scenarios: list[dict]) -> Path:
    path = root / "provider-release-proof-coverage.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "providers": sorted({scenario["provider"] for scenario in scenarios}),
                "surfaces": [],
                "rows": [],
                "accepted_release_proof_scenarios": scenarios,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_accept_archives_proof_and_artifacts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof_path = _write_proof(root, "candidate")
        artifact = root / "acceptance.json"

        result, payload = _run(
            [
                "accept",
                "--proof",
                str(proof_path),
                "--baseline-root",
                str(root / "baselines"),
                "--artifact",
                str(artifact),
            ]
        )

        assert result.returncode == 0
        assert payload["artifact_kind"] == "provider_release_proof_baseline_acceptance"
        assert Path(payload["accepted_path"]).exists()
        assert Path(payload["version_path"]).exists()
        assert {
            "source_artifact",
            "stdout",
            "stderr",
            "normalized_contract",
            "provider_contract",
            "operation_evidence",
            "session_projection",
        } <= set(payload["archived_artifacts"])


def test_accept_refuses_non_green_proof() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof_path = _write_proof(root, "candidate", status="warn")
        artifact = root / "acceptance.json"

        result, payload = _run(
            [
                "accept",
                "--proof",
                str(proof_path),
                "--baseline-root",
                str(root / "baselines"),
                "--artifact",
                str(artifact),
            ]
        )

        assert result.returncode == 1
        assert payload["artifact_kind"] == "provider_release_proof_baseline_acceptance"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "baseline_acceptance_rejected"
        assert payload["accepted_path"] is None
        assert not (root / "baselines").exists()


def test_diff_against_accepted_baseline_matches() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["diff"]["status"] == "match"


def test_diff_against_accepted_baseline_detects_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", status="fail", version="1.2.4")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_drift"
        assert payload["diff"]["status"] == "different"


def test_diff_detects_stable_session_projection_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        projection_path = Path(candidate_payload["artifacts"]["session_projection"])
        projection = json.loads(projection_path.read_text(encoding="utf-8"))
        projection["projection"]["checks"]["prompt_async_no_reply_delivery"][
            "status"
        ] = "fail"
        projection["projection"]["checks"]["prompt_async_no_reply_delivery"][
            "failure_code"
        ] = "opencode_prompt_async_delivery_not_observed"
        projection["projection"]["operation_statuses"]["send_input"]["status"] = "fail"
        projection["projection"]["operation_statuses"]["send_input"]["failure_code"] = (
            "opencode_prompt_async_delivery_not_observed"
        )
        projection_path.write_text(json.dumps(projection), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_drift"
        assert payload["diff"]["status"] == "different"
        previous = payload["diff"]["changes"][0]["previous"]
        current = payload["diff"]["changes"][0]["current"]
        assert (
            previous["artifacts"]["session_projection"]["projection"]["checks"][
                "prompt_async_no_reply_delivery"
            ]["status"]
            == "pass"
        )
        assert (
            current["artifacts"]["session_projection"]["projection"]["checks"][
                "prompt_async_no_reply_delivery"
            ]["failure_code"]
            == "opencode_prompt_async_delivery_not_observed"
        )


def test_diff_detects_stable_action_matrix_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        action_matrix_path = Path(candidate_payload["artifacts"]["action_matrix"])
        action_matrix = json.loads(action_matrix_path.read_text(encoding="utf-8"))
        action_matrix["action_matrix"]["actions"][0]["status"] = "fail"
        action_matrix["action_matrix"]["actions"][0]["failure_code"] = (
            "send_message_contract_regressed"
        )
        action_matrix["action_matrix"]["status_counts"] = {"blocked": 1, "fail": 1}
        action_matrix_path.write_text(json.dumps(action_matrix), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_drift"
        assert payload["diff"]["status"] == "different"
        action_drift = payload["diff"]["action_drift"]
        assert action_drift["status"] == "different"
        assert action_drift["changed_action_count"] == 1
        assert action_drift["counts_by_artifact"] == {"action_matrix": 1}
        assert action_drift["counts_by_required_evidence"] == {"hermetic": 1}
        action_row = next(
            row for row in action_drift["actions"] if row["artifact"] == "action_matrix"
        )
        assert action_row["action_id"] == "send_message"
        assert action_row["category"] == "control"
        assert action_row["required_evidence"] == "hermetic"
        assert action_row["changed_fields"] == ["status", "failure_code"]
        assert action_row["previous"]["status"] == "pass"
        assert action_row["current"]["status"] == "fail"
        assert (
            action_row["current"]["failure_code"] == "send_message_contract_regressed"
        )
        previous = payload["diff"]["changes"][0]["previous"]
        current = payload["diff"]["changes"][0]["current"]
        assert (
            previous["artifacts"]["action_matrix"]["action_matrix"]["actions"][0][
                "status"
            ]
            == "pass"
        )
        assert current["artifacts"]["action_matrix"]["action_matrix"]["actions"][0][
            "failure_code"
        ] == ("send_message_contract_regressed")


def test_diff_summarizes_action_category_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        action_matrix_path = Path(candidate_payload["artifacts"]["action_matrix"])
        action_matrix = json.loads(action_matrix_path.read_text(encoding="utf-8"))
        action_matrix["action_matrix"]["actions"][0]["category"] = "observation"
        action_matrix_path.write_text(json.dumps(action_matrix), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        action_drift = payload["diff"]["action_drift"]
        assert action_drift["status"] == "different"
        assert action_drift["changed_action_count"] == 1
        assert action_drift["counts_by_category"] == {"observation": 1}
        assert action_drift["actions"][0]["changed_fields"] == ["category"]
        assert action_drift["actions"][0]["previous"]["category"] == "control"
        assert action_drift["actions"][0]["current"]["category"] == "observation"


def test_diff_summarizes_control_surface_action_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        control_surface_path = Path(candidate_payload["artifacts"]["control_surface"])
        control_surface = json.loads(control_surface_path.read_text(encoding="utf-8"))
        control_surface["control_surface"]["actions"][0]["status"] = "fail"
        control_surface["control_surface"]["actions"][0]["failure_code"] = (
            "control_surface_send_regressed"
        )
        control_surface["control_surface"]["status_counts"] = {"fail": 1}
        control_surface_path.write_text(json.dumps(control_surface), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        action_drift = payload["diff"]["action_drift"]
        assert action_drift["status"] == "different"
        assert action_drift["changed_action_count"] == 1
        assert action_drift["counts_by_artifact"] == {"control_surface": 1}
        assert action_drift["actions"][0]["artifact"] == "control_surface"
        assert action_drift["actions"][0]["changed_fields"] == [
            "status",
            "failure_code",
        ]


def test_diff_summarizes_provider_execution_coverage_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        execution_path = Path(
            candidate_payload["artifacts"]["provider_execution_coverage_matrix"]
        )
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "coverage_status"
        ] = "fail"
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "failure_code"
        ] = "send_message_execution_regressed"
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "scenario_statuses"
        ] = {"managed_session_e2e": "fail"}
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "scenario_failure_codes"
        ] = {"managed_session_e2e": "send_message_execution_regressed"}
        execution["provider_execution_coverage_matrix"]["coverage_status_counts"] = {
            "blocked": 1,
            "fail": 1,
        }
        execution_path.write_text(json.dumps(execution), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "provider_release_proof_drift"
        coverage_drift = payload["diff"]["execution_coverage_drift"]
        assert coverage_drift["status"] == "different"
        assert coverage_drift["changed_action_count"] == 1
        assert coverage_drift["counts_by_required_evidence"] == {"hermetic": 1}
        assert coverage_drift["counts_by_coverage_status"] == {"fail": 1}
        assert coverage_drift["counts_by_coverage_kind"] == {"executable_scenario": 1}
        coverage_row = coverage_drift["actions"][0]
        assert coverage_row["action_id"] == "send_message"
        assert coverage_row["changed_fields"] == [
            "coverage_status",
            "failure_code",
            "scenario_statuses",
            "scenario_failure_codes",
        ]
        assert coverage_row["previous"]["coverage_status"] == "pass"
        assert coverage_row["current"]["coverage_status"] == "fail"
        assert (
            coverage_row["current"]["failure_code"]
            == "send_message_execution_regressed"
        )


def test_diff_summarizes_provider_execution_rollup_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        execution_path = Path(
            candidate_payload["artifacts"]["provider_execution_coverage_matrix"]
        )
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["provider_execution_coverage_matrix"]["coverage_status_counts"] = {
            "blocked": 1,
            "pass": 99,
        }
        execution_path.write_text(json.dumps(execution), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "provider_release_proof_drift"
        coverage_drift = payload["diff"]["execution_coverage_drift"]
        assert coverage_drift["status"] == "different"
        assert coverage_drift["changed_action_count"] == 0
        assert coverage_drift["rollup_drift"]["changed_fields"] == [
            "coverage_status_counts"
        ]
        assert coverage_drift["rollup_drift"]["previous"]["coverage_status_counts"] == {
            "blocked": 1,
            "pass": 1,
        }
        assert coverage_drift["rollup_drift"]["current"]["coverage_status_counts"] == {
            "blocked": 1,
            "pass": 99,
        }


def test_diff_blocks_when_comparable_artifacts_are_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _, acceptance = _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        Path(acceptance["archived_artifacts"]["session_projection"]).unlink()
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        Path(candidate_payload["artifacts"]["session_projection"]).unlink()

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert (
            payload["failure_code"]
            == "provider_release_proof_comparable_artifacts_unavailable"
        )
        assert payload["diff"]["artifact_errors"] == [
            {
                "artifact": "session_projection",
                "failure_code": "comparable_artifact_missing",
                "message": "Comparable artifact session_projection is missing.",
                "path": acceptance["archived_artifacts"]["session_projection"],
                "side": "baseline",
            },
            {
                "artifact": "session_projection",
                "failure_code": "comparable_artifact_missing",
                "message": "Comparable artifact session_projection is missing.",
                "path": candidate_payload["artifacts"]["session_projection"],
                "side": "candidate",
            },
        ]


def test_diff_marks_action_drift_unavailable_when_action_artifact_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _, acceptance = _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        Path(acceptance["archived_artifacts"]["action_matrix"]).unlink()
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        Path(candidate_payload["artifacts"]["action_matrix"]).unlink()

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert (
            payload["failure_code"]
            == "provider_release_proof_comparable_artifacts_unavailable"
        )
        action_drift = payload["diff"]["action_drift"]
        assert action_drift["status"] == "unavailable"
        assert action_drift["changed_action_count"] == 0
        assert [
            (error["side"], error["artifact"], error["failure_code"])
            for error in action_drift["unavailable_artifacts"]
        ] == [
            ("baseline", "action_matrix", "comparable_artifact_missing"),
            ("candidate", "action_matrix", "comparable_artifact_missing"),
        ]


def test_diff_marks_execution_coverage_drift_unavailable_when_artifact_is_missing() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _, acceptance = _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        Path(
            acceptance["archived_artifacts"]["provider_execution_coverage_matrix"]
        ).unlink()
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        Path(
            candidate_payload["artifacts"]["provider_execution_coverage_matrix"]
        ).unlink()

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        coverage_drift = payload["diff"]["execution_coverage_drift"]
        assert coverage_drift["status"] == "unavailable"
        assert coverage_drift["changed_action_count"] == 0
        assert [
            (error["side"], error["artifact"], error["failure_code"])
            for error in coverage_drift["unavailable_artifacts"]
        ] == [
            (
                "baseline",
                "provider_execution_coverage_matrix",
                "comparable_artifact_unavailable",
            ),
            (
                "candidate",
                "provider_execution_coverage_matrix",
                "comparable_artifact_unavailable",
            ),
        ]


def test_diff_blocks_when_execution_coverage_missing_on_one_side() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        Path(
            candidate_payload["artifacts"]["provider_execution_coverage_matrix"]
        ).unlink()

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["failure_code"] == "provider_release_proof_drift"
        coverage_drift = payload["diff"]["execution_coverage_drift"]
        assert coverage_drift["status"] == "different"
        assert coverage_drift["changed_action_count"] == 0
        assert [
            (error["side"], error["artifact"], error["failure_code"])
            for error in coverage_drift["unavailable_artifacts"]
        ] == [
            (
                "candidate",
                "provider_execution_coverage_matrix",
                "comparable_artifact_unavailable",
            )
        ]


def test_diff_blocks_when_operation_evidence_artifact_is_malformed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        operation_evidence_path = Path(
            candidate_payload["artifacts"]["operation_evidence"]
        )
        operation_evidence = json.loads(
            operation_evidence_path.read_text(encoding="utf-8")
        )
        operation_evidence["operation_evidence"] = []
        operation_evidence_path.write_text(
            json.dumps(operation_evidence), encoding="utf-8"
        )
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert (
            payload["failure_code"]
            == "provider_release_proof_comparable_artifacts_unavailable"
        )
        assert {
            "artifact": "operation_evidence",
            "failure_code": "comparable_artifact_field_malformed",
            "field": "operation_evidence",
            "message": "Comparable artifact operation_evidence field operation_evidence must be an object.",
            "side": "candidate",
        } in payload["diff"]["artifact_errors"]


def test_diff_reports_missing_baseline_as_yellow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        candidate = _write_proof(root, "candidate")

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(root / "baselines"),
            ]
        )

        assert result.returncode == 0
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "baseline_missing"
        assert payload["diff"]["status"] == "not_compared"
        assert payload["diff"]["action_drift"]["status"] == "not_compared"
        assert payload["diff"]["execution_coverage_drift"]["status"] == "not_compared"


def test_diff_reports_yellow_candidate_without_baseline_as_yellow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        candidate = _write_proof(root, "candidate", status="warn")

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(root / "baselines"),
            ]
        )

        assert result.returncode == 0
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "fake_warning"
        assert payload["diff"]["status"] == "not_compared"


def test_diff_reports_red_candidate_without_baseline_as_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        candidate = _write_proof(root, "candidate", status="fail")

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(root / "baselines"),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "fake_drift"
        assert payload["diff"]["status"] == "not_compared"


def test_diff_does_not_promote_matching_yellow_candidate_to_green() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        candidate_payload["verdict"] = "yellow"
        candidate_payload["failure_code"] = "fake_warning"
        candidate.write_text(json.dumps(candidate_payload), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "fake_warning"
        assert payload["diff"]["status"] == "match"


def test_diff_blocks_drift_even_when_candidate_is_yellow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        candidate_payload["verdict"] = "yellow"
        candidate_payload["failure_code"] = "fake_warning"
        normalized_path = Path(candidate_payload["artifacts"]["normalized_contract"])
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        normalized["operation_evidence"]["send_input"]["status"] = "fail"
        normalized["operation_evidence"]["send_input"]["failure_code"] = "fake_drift"
        normalized_path.write_text(json.dumps(normalized), encoding="utf-8")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_drift"
        assert payload["diff"]["status"] == "different"


def test_diff_can_compare_explicit_old_and_new_proofs() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        old = _write_proof(root, "old")
        new = _write_proof(root, "new", version="1.2.4")

        result, payload = _run(["diff", "--base", str(old), "--candidate", str(new)])

        assert result.returncode == 0
        assert payload["verdict"] == "green"
        assert payload["baseline"]["provider_version"] == "opencode 1.2.3"
        assert payload["candidate"]["provider_version"] == "opencode 1.2.4"


def test_old_new_command_compares_explicit_proof_artifacts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        old = _write_proof(root, "old")
        new = _write_proof(root, "new", version="1.2.4")
        artifact = root / "old-new-diff.json"

        result, payload = _run(
            [
                "old-new",
                "--old",
                str(old),
                "--new",
                str(new),
                "--artifact",
                str(artifact),
            ]
        )

        assert result.returncode == 0
        assert payload["artifact_kind"] == "provider_release_proof_old_new_diff"
        assert payload["verdict"] == "green"
        assert payload["diff"]["status"] == "match"
        assert payload["old_proof_uri"] == str(old.resolve())
        assert payload["new_proof_uri"] == str(new.resolve())
        assert payload["staging"]["status"] == "explicit_proof_artifacts"


def test_old_new_command_blocks_on_action_row_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        old = _write_proof(root, "old")
        new = _write_proof(root, "new", version="1.2.4")
        new_payload = json.loads(new.read_text(encoding="utf-8"))
        action_matrix_path = Path(new_payload["artifacts"]["action_matrix"])
        action_matrix = json.loads(action_matrix_path.read_text(encoding="utf-8"))
        action_matrix["action_matrix"]["actions"][0]["status"] = "fail"
        action_matrix["action_matrix"]["actions"][0]["failure_code"] = (
            "send_message_contract_regressed"
        )
        action_matrix_path.write_text(json.dumps(action_matrix), encoding="utf-8")

        result, payload = _run(
            [
                "old-new",
                "--old",
                str(old),
                "--new",
                str(new),
            ]
        )

        assert result.returncode == 1
        assert payload["artifact_kind"] == "provider_release_proof_old_new_diff"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_drift"
        assert payload["diff"]["status"] == "different"
        assert payload["diff"]["action_drift"]["changed_action_count"] == 1
        assert payload["diff"]["action_drift"]["counts_by_required_evidence"] == {
            "hermetic": 1
        }


def test_old_new_command_blocks_on_execution_coverage_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        old = _write_proof(root, "old")
        new = _write_proof(root, "new", version="1.2.4")
        new_payload = json.loads(new.read_text(encoding="utf-8"))
        execution_path = Path(
            new_payload["artifacts"]["provider_execution_coverage_matrix"]
        )
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "coverage_status"
        ] = "fail"
        execution["provider_execution_coverage_matrix"]["actions"][0][
            "failure_code"
        ] = "send_message_execution_regressed"
        execution_path.write_text(json.dumps(execution), encoding="utf-8")

        result, payload = _run(
            [
                "old-new",
                "--old",
                str(old),
                "--new",
                str(new),
            ]
        )

        assert result.returncode == 1
        assert payload["artifact_kind"] == "provider_release_proof_old_new_diff"
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_release_proof_drift"
        assert payload["diff"]["status"] == "different"
        assert payload["diff"]["execution_coverage_drift"]["changed_action_count"] == 1
        assert payload["diff"]["execution_coverage_drift"][
            "counts_by_coverage_status"
        ] == {"fail": 1}


def test_status_reports_missing_baseline_as_yellow() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        result, payload = _run(
            [
                "status",
                "--provider",
                "opencode",
                "--scenario-id",
                "opencode-release-proof-v1",
                "--baseline-root",
                str(root / "baselines"),
            ]
        )

        assert result.returncode == 0
        assert payload["artifact_kind"] == "provider_release_proof_baseline_status"
        assert payload["accepted"] is False
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "baseline_missing"


def test_status_reports_accepted_baseline_and_archived_artifacts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        result, payload = _run(
            [
                "status",
                "--provider",
                "opencode",
                "--scenario-id",
                "opencode-release-proof-v1",
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["accepted"] is True
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["provider_version"] == "opencode 1.2.3"
        assert {"source_artifact", "stdout", "stderr"} <= set(
            payload["archived_artifacts"]
        )
        assert payload["missing_archived_artifacts"] == []


def test_relocated_baseline_store_resolves_archived_artifacts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        relocated_root = root / "relocated-baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )

        shutil.copytree(baseline_root, relocated_root)
        shutil.rmtree(baseline_root)

        result, payload = _run(
            [
                "status",
                "--provider",
                "opencode",
                "--scenario-id",
                "opencode-release-proof-v1",
                "--baseline-root",
                str(relocated_root),
            ]
        )

        assert result.returncode == 0
        assert payload["accepted"] is True
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["missing_archived_artifacts"] == []
        for path in payload["archived_artifacts"].values():
            assert str(path).startswith(str(relocated_root))

        diff_result, diff_payload = _run(
            [
                "diff",
                "--candidate",
                str(candidate),
                "--baseline-root",
                str(relocated_root),
            ]
        )

        assert diff_result.returncode == 0
        assert diff_payload["verdict"] == "green"
        assert diff_payload["diff"]["status"] == "match"


def test_status_warns_when_archived_artifact_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        _, acceptance = _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        Path(acceptance["archived_artifacts"]["stdout"]).unlink()

        result, payload = _run(
            [
                "status",
                "--provider",
                "opencode",
                "--scenario-id",
                "opencode-release-proof-v1",
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["accepted"] is True
        assert payload["verdict"] == "yellow"
        assert payload["failure_code"] == "baseline_artifacts_missing"
        assert payload["missing_archived_artifacts"] == ["stdout"]


def test_status_blocks_when_accepted_baseline_is_not_green() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        _, acceptance = _run(
            ["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)]
        )
        accepted_path = Path(acceptance["accepted_path"])
        accepted_payload = json.loads(accepted_path.read_text(encoding="utf-8"))
        accepted_payload["verdict"] = "yellow"
        accepted_payload["failure_code"] = "tampered"
        accepted_path.write_text(json.dumps(accepted_payload), encoding="utf-8")

        result, payload = _run(
            [
                "status",
                "--provider",
                "opencode",
                "--scenario-id",
                "opencode-release-proof-v1",
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["accepted"] is True
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "accepted_baseline_not_green"


def test_status_all_reports_inventory_green_when_every_accepted_scenario_is_green() -> (
    None
):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        opencode = _write_proof(root, "opencode")
        codex = _write_proof(
            root,
            "codex",
            provider="codex",
            scenario_id="codex-managed-live-send-release-proof-v1",
        )
        _run(
            ["accept", "--proof", str(opencode), "--baseline-root", str(baseline_root)]
        )
        _run(["accept", "--proof", str(codex), "--baseline-root", str(baseline_root)])
        coverage = _write_coverage_inventory(
            root,
            [
                {
                    "provider": "opencode",
                    "scenario_id": "opencode-release-proof-v1",
                    "provider_version": "opencode 1.2.3",
                    "baseline_scope": "no_token_server_api_control_shape",
                    "baseline_boundary": "live_no_token",
                    "promoted_to_sauron": True,
                },
                {
                    "provider": "codex",
                    "scenario_id": "codex-managed-live-send-release-proof-v1",
                    "provider_version": "codex 1.2.3",
                    "baseline_scope": "managed_runtime_live_send_marker",
                    "baseline_boundary": "managed_runtime_live_token_or_fake",
                    "promoted_to_sauron": True,
                },
            ],
        )

        result, payload = _run(
            [
                "status-all",
                "--coverage",
                str(coverage),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 0
        assert payload["artifact_kind"] == "provider_release_proof_baseline_status_all"
        assert payload["verdict"] == "green"
        assert payload["failure_code"] is None
        assert payload["scenario_count"] == 2
        assert payload["green_count"] == 2
        assert payload["non_green_count"] == 0
        assert [status["provider"] for status in payload["statuses"]] == [
            "opencode",
            "codex",
        ]
        assert all(status["accepted"] is True for status in payload["statuses"])


def test_status_all_blocks_when_inventory_claimed_baseline_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        coverage = _write_coverage_inventory(
            root,
            [
                {
                    "provider": "opencode",
                    "scenario_id": "opencode-release-proof-v1",
                    "provider_version": "opencode 1.2.3",
                    "baseline_scope": "no_token_server_api_control_shape",
                    "baseline_boundary": "live_no_token",
                    "promoted_to_sauron": True,
                }
            ],
        )

        result, payload = _run(
            [
                "status-all",
                "--coverage",
                str(coverage),
                "--baseline-root",
                str(baseline_root),
            ]
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "accepted_baseline_inventory_incomplete"
        assert payload["scenario_count"] == 1
        assert payload["green_count"] == 0
        assert payload["non_green_count"] == 1
        assert payload["statuses"][0]["accepted"] is False
        assert payload["statuses"][0]["failure_code"] == "baseline_missing"


def main() -> int:
    tests = [
        test_accept_archives_proof_and_artifacts,
        test_accept_refuses_non_green_proof,
        test_diff_against_accepted_baseline_matches,
        test_diff_against_accepted_baseline_detects_drift,
        test_diff_detects_stable_session_projection_drift,
        test_diff_detects_stable_action_matrix_drift,
        test_diff_summarizes_action_category_drift,
        test_diff_summarizes_control_surface_action_drift,
        test_diff_summarizes_provider_execution_coverage_drift,
        test_diff_summarizes_provider_execution_rollup_drift,
        test_diff_blocks_when_comparable_artifacts_are_missing,
        test_diff_marks_action_drift_unavailable_when_action_artifact_is_missing,
        test_diff_marks_execution_coverage_drift_unavailable_when_artifact_is_missing,
        test_diff_blocks_when_execution_coverage_missing_on_one_side,
        test_diff_blocks_when_operation_evidence_artifact_is_malformed,
        test_diff_reports_missing_baseline_as_yellow,
        test_diff_reports_yellow_candidate_without_baseline_as_yellow,
        test_diff_reports_red_candidate_without_baseline_as_red,
        test_diff_does_not_promote_matching_yellow_candidate_to_green,
        test_diff_blocks_drift_even_when_candidate_is_yellow,
        test_diff_can_compare_explicit_old_and_new_proofs,
        test_old_new_command_compares_explicit_proof_artifacts,
        test_old_new_command_blocks_on_action_row_drift,
        test_old_new_command_blocks_on_execution_coverage_drift,
        test_status_reports_missing_baseline_as_yellow,
        test_status_reports_accepted_baseline_and_archived_artifacts,
        test_relocated_baseline_store_resolves_archived_artifacts,
        test_status_warns_when_archived_artifact_is_missing,
        test_status_blocks_when_accepted_baseline_is_not_green,
        test_status_all_reports_inventory_green_when_every_accepted_scenario_is_green,
        test_status_all_blocks_when_inventory_claimed_baseline_is_missing,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
