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


def test_provider_release_proof_make_accept_and_diff() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        proof = root / "proof.json"
        evidence = root / "evidence"
        baseline_root = root / "baselines"
        acceptance = root / "acceptance.json"
        diff = root / "diff.json"

        proof_result = _run_make(
            [
                "provider-release-proof",
                "PROVIDER=gemini",
                "PROVIDER_VERSION=gemini-test-0",
                f"ARTIFACT={proof}",
                f"EVIDENCE_ROOT={evidence}",
            ]
        )

        assert proof_result.returncode == 0, proof_result.stderr
        proof_payload = _read_json(proof)
        assert proof_payload["artifact_kind"] == "provider_release_proof"
        assert proof_payload["provider"] == "gemini"
        assert proof_payload["provider_version"] == "gemini-test-0"
        assert proof_payload["verdict"] == "yellow"
        assert proof_payload["failure_code"] == "provider_release_proof_not_implemented"
        assert Path(proof_payload["artifacts"]["source_artifact"]).exists()

        accept_result = _run_make(
            [
                "provider-release-proof-accept",
                f"PROOF={proof}",
                f"BASELINE_ROOT={baseline_root}",
                f"ARTIFACT={acceptance}",
            ]
        )

        assert accept_result.returncode == 0, accept_result.stderr
        accept_payload = _read_json(acceptance)
        assert accept_payload["artifact_kind"] == "provider_release_proof_baseline_acceptance"
        assert Path(accept_payload["accepted_path"]).exists()

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
        assert diff_payload["verdict"] == "green"
        assert diff_payload["diff"]["status"] == "match"


def main() -> int:
    tests = [
        test_provider_release_proof_make_requires_provider,
        test_provider_release_proof_make_accept_and_diff,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
