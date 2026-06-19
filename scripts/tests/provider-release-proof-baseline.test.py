#!/usr/bin/env python3
"""Tests for provider release-proof baseline acceptance and diffing."""

from __future__ import annotations

import json
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


def _write_proof(root: Path, name: str, *, status: str = "pass", version: str = "1.2.3") -> Path:
    proof_dir = root / name
    artifact_dir = proof_dir / "evidence"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_artifact = artifact_dir / "source.json"
    stdout = artifact_dir / "stdout.log"
    stderr = artifact_dir / "stderr.log"
    normalized_contract = artifact_dir / "normalized" / "contract.json"
    provider_contract = artifact_dir / "normalized" / "provider_contract.json"
    operation_evidence_artifact = artifact_dir / "normalized" / "operation_evidence.json"
    session_projection = artifact_dir / "normalized" / "session_projection.json"
    normalized = {
        "artifact_kind": "provider_release_proof",
        "provider": "opencode",
        "provider_version": f"opencode {version}",
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
        "provider": "opencode",
        "provider_version": f"opencode {version}",
        "contract_operations": {
            "send_input": {
                "level": "live_no_token",
                "source": "fake server_contract",
            }
        },
    }
    operation_evidence_payload = {
        "artifact_kind": "provider_release_proof_operation_evidence",
        "provider": "opencode",
        "provider_version": f"opencode {version}",
        "operation_evidence": normalized["operation_evidence"],
    }
    session_projection_payload = {
        "artifact_kind": "provider_release_proof_session_projection",
        "provider": "opencode",
        "provider_version": f"opencode {version}",
        "status": "captured",
        "projection": {
            "artifact_kind": "provider_live_session_projection",
            "provider": "opencode",
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
    source_artifact.write_text(json.dumps({"raw": True}), encoding="utf-8")
    stdout.write_text("stdout\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    normalized_contract.parent.mkdir(parents=True, exist_ok=True)
    normalized_contract.write_text(json.dumps(normalized), encoding="utf-8")
    provider_contract.write_text(json.dumps(provider_contract_payload), encoding="utf-8")
    operation_evidence_artifact.write_text(json.dumps(operation_evidence_payload), encoding="utf-8")
    session_projection.write_text(json.dumps(session_projection_payload), encoding="utf-8")
    proof = {
        "schema_version": 1,
        "artifact_kind": "provider_release_proof",
        "provider": "opencode",
        "provider_version": f"opencode {version}",
        "scenario_id": "opencode-release-proof-v1",
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
        },
    }
    proof_path = proof_dir / "proof.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    return proof_path


def _run(args: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = Path(args[args.index("--artifact") + 1]) if "--artifact" in args else None
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
            "provider_contract",
            "operation_evidence",
            "session_projection",
        } <= set(
            payload["archived_artifacts"]
        )


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
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        projection["projection"]["checks"]["prompt_async_no_reply_delivery"]["status"] = "fail"
        projection["projection"]["checks"]["prompt_async_no_reply_delivery"]["failure_code"] = (
            "opencode_prompt_async_delivery_not_observed"
        )
        projection["projection"]["operation_statuses"]["send_input"]["status"] = "fail"
        projection["projection"]["operation_statuses"]["send_input"]["failure_code"] = (
            "opencode_prompt_async_delivery_not_observed"
        )
        projection_path.write_text(json.dumps(projection), encoding="utf-8")
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        assert payload["failure_code"] == "provider_release_proof_comparable_artifacts_unavailable"
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


def test_diff_blocks_when_operation_evidence_artifact_is_malformed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        baseline_root = root / "baselines"
        accepted = _write_proof(root, "accepted")
        candidate = _write_proof(root, "candidate", version="1.2.4")
        candidate_payload = json.loads(candidate.read_text(encoding="utf-8"))
        operation_evidence_path = Path(candidate_payload["artifacts"]["operation_evidence"])
        operation_evidence = json.loads(operation_evidence_path.read_text(encoding="utf-8"))
        operation_evidence["operation_evidence"] = []
        operation_evidence_path.write_text(json.dumps(operation_evidence), encoding="utf-8")
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        assert payload["failure_code"] == "provider_release_proof_comparable_artifacts_unavailable"
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
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        _run(["accept", "--proof", str(accepted), "--baseline-root", str(baseline_root)])

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
        assert {"source_artifact", "stdout", "stderr"} <= set(payload["archived_artifacts"])
        assert payload["missing_archived_artifacts"] == []


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


def main() -> int:
    tests = [
        test_accept_archives_proof_and_artifacts,
        test_accept_refuses_non_green_proof,
        test_diff_against_accepted_baseline_matches,
        test_diff_against_accepted_baseline_detects_drift,
        test_diff_detects_stable_session_projection_drift,
        test_diff_blocks_when_comparable_artifacts_are_missing,
        test_diff_blocks_when_operation_evidence_artifact_is_malformed,
        test_diff_reports_missing_baseline_as_yellow,
        test_diff_reports_yellow_candidate_without_baseline_as_yellow,
        test_diff_reports_red_candidate_without_baseline_as_red,
        test_diff_does_not_promote_matching_yellow_candidate_to_green,
        test_diff_can_compare_explicit_old_and_new_proofs,
        test_status_reports_missing_baseline_as_yellow,
        test_status_reports_accepted_baseline_and_archived_artifacts,
        test_status_warns_when_archived_artifact_is_missing,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
