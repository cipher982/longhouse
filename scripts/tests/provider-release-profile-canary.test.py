#!/usr/bin/env python3
"""Tests for the shared provider release profile canary wrapper."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "scripts/qa/provider-release-profile-canary.py"


def _write_exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_provider(path: Path, version: str) -> Path:
    return _write_exe(
        path,
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo \'{version}\'; exit 0; fi\nexit 0\n',
    )


def _run_canary(root: Path, args: list[str]) -> tuple[subprocess.CompletedProcess[str], dict]:
    artifact = root / "artifact.json"
    result = subprocess.run(
        [
            sys.executable,
            str(CANARY),
            "--repo-root",
            str(REPO_ROOT),
            "--artifact",
            str(artifact),
            "--evidence-root",
            str(root / "evidence"),
            "--json",
            *args,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    return result, payload


def test_each_managed_provider_emits_profile_artifact() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        for provider, binary_name in {
            "codex": "codex",
            "claude": "claude",
            "opencode": "opencode",
            "antigravity": "agy",
        }.items():
            binary = _fake_provider(root / "bin" / binary_name, f"{provider} 1.2.3")
            result, payload = _run_canary(
                root / provider,
                [
                    "--provider",
                    provider,
                    "--provider-bin",
                    str(binary),
                    "--source-review-status",
                    "pass",
                ],
            )

            assert result.returncode == 0, result.stderr + result.stdout
            assert payload["provider"] == provider
            assert payload["provider_version"] == f"{provider} 1.2.3"
            assert payload["verdict"] == "yellow"
            assert payload["failure_code"] == "insufficient_coverage"
            assert payload["canaries"]["contract_profile"]["status"] == "pass"
            assert payload["canaries"]["contract_profile"]["operation_evidence"]
            assert payload["canaries"]["binary_identity"]["status"] == "pass"
            assert payload["canaries"]["live_contract"]["status"] == "not_run"
            assert payload["operation_evidence"]["launch_local"]["status"] == "not_run"
            assert payload["operation_evidence"]["launch_local"]["level"] == "none"
            assert payload["operation_evidence"]["launch_local"]["failure_code"] == "insufficient_coverage"

            if provider == "claude":
                assert payload["operation_evidence"]["launch_remote"]["status"] == "pass"
                assert payload["operation_evidence"]["launch_remote"]["level"] == "source_review"
                assert payload["operation_evidence"]["launch_remote"]["canary"] == "source_review"
            if provider == "opencode":
                assert payload["operation_evidence"]["send_input"]["status"] == "not_run"
                assert payload["operation_evidence"]["send_input"]["canary"] == "opencode_server_live_contract"
                assert payload["operation_evidence"]["steer_active_turn"]["status"] == "unsupported"
            if provider == "antigravity":
                assert payload["operation_evidence"]["send_input"]["status"] == "not_run"
                assert payload["operation_evidence"]["send_input"]["canary"] == "antigravity_real_agy_send"
                assert payload["operation_evidence"]["launch_remote"]["status"] == "unsupported"


def test_profile_canary_can_use_release_version_without_local_binary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "claude",
                "--provider-version",
                "1.2.3",
                "--skip-binary-identity",
                "--source-review-status",
                "pass",
            ],
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert payload["provider_version"] == "1.2.3"
        assert payload["canaries"]["binary_identity"]["status"] == "not_run"
        assert payload["verdict"] == "yellow"
        assert payload["operation_evidence"]["launch_remote"]["status"] == "pass"
        assert payload["operation_evidence"]["steer_active_turn"]["status"] == "not_run"


def test_missing_provider_contract_is_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        result, payload = _run_canary(root, ["--provider", "unknown"])

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_contract_missing"


def test_missing_provider_binary_is_red() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "opencode",
                "--provider-bin",
                str(root / "missing-opencode"),
                "--source-review-status",
                "pass",
            ],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "provider_binary_not_found"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["failure_code"] == "provider_binary_not_found"


def test_source_review_failure_marks_supported_operations_failed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        binary = _fake_provider(root / "bin" / "claude", "2.1.153")
        result, payload = _run_canary(
            root,
            [
                "--provider",
                "claude",
                "--provider-bin",
                str(binary),
                "--source-review-status",
                "fail",
                "--source-review-note",
                "Claude channel entrypoint changed",
            ],
        )

        assert result.returncode == 1
        assert payload["verdict"] == "red"
        assert payload["failure_code"] == "source_review_failed"
        assert payload["operation_evidence"]["send_input"]["status"] == "fail"
        assert payload["operation_evidence"]["send_input"]["failure_code"] == "source_review_failed"
        assert payload["operation_evidence"]["launch_remote"]["status"] == "fail"


def main() -> int:
    tests = [
        test_each_managed_provider_emits_profile_artifact,
        test_profile_canary_can_use_release_version_without_local_binary,
        test_missing_provider_contract_is_red,
        test_missing_provider_binary_is_red,
        test_source_review_failure_marks_supported_operations_failed,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
