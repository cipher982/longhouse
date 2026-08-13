from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from zerg.qa import antigravity_launch_hook_inbox
from zerg.qa.provider_semantic_qualification import SemanticAssertion
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass


def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\necho fake-agy\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def _fake_no_token_canary_artifact() -> dict:
    return {
        "provider": "antigravity",
        "provider_version": "1.1.5",
        "verdict": "pass",
        "canaries": {
            "binary_identity": {"status": "pass"},
            "command_shape": {"status": "pass"},
            "plugin_contract": {"status": "pass"},
            "global_hooks_contract": {"status": "pass"},
            "hook_inbox_claim_contract": {"status": "pass"},
        },
    }


def _install_fake_execute(monkeypatch: pytest.MonkeyPatch, *, hook_inbox_outcome: AssertionOutcome) -> None:
    def fake_execute(binary, evidence_root):  # noqa: ANN001 - mirrors the real signature
        assertions = (
            SemanticAssertion(
                "hook_inbox_contract_preserved",
                hook_inbox_outcome,
                EvidenceClass.LIVE_NO_TOKEN,
            ),
            SemanticAssertion(
                "real_print_injection_observed",
                AssertionOutcome.BLOCKED,
                EvidenceClass.LIVE_NO_TOKEN,
            ),
        )
        overall = "blocked" if hook_inbox_outcome != AssertionOutcome.SEMANTIC_FAIL else "fail"
        status_dict = {
            "status": overall,
            "no_token_canary": _fake_no_token_canary_artifact(),
            "real_print_canary": {"status": "blocked", "failure_code": "explicit_antigravity_qualification_authority_missing"},
        }
        return status_dict, assertions, ()

    monkeypatch.setattr(antigravity_launch_hook_inbox.antigravity_hook_qualification, "_execute", fake_execute)


def _run_args(tmp_path: Path, *, evidence_root: Path, provider_bin: Path, variant: str = "cell:antigravity:hook_inbox_contract_preserved:antigravity_hook_inbox"):
    return antigravity_launch_hook_inbox._parser().parse_args(
        [
            "--variant",
            variant,
            "--evidence-root",
            str(evidence_root),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
            "--provider-bin",
            str(provider_bin),
        ]
    )


def test_registration_matches_schema_declared_contract() -> None:
    registration = antigravity_launch_hook_inbox.REGISTRATION.to_dict()

    assert registration["producer_id"] == "antigravity.hook_inbox_launch.v1"
    assert registration["scenario_id"] == "antigravity_hook_inbox"
    assert registration["oracle_source"] == "server/zerg/qa/provider_release_semantic_oracles.py"
    assert registration["assertion_cells"] == [{"assertion_id": "hook_inbox_contract_preserved", "variant": None}]
    assert registration["providers"] == ["antigravity"]
    assert registration["modes"] == ["helm"]
    assert registration["evidence_classes"] == ["live_no_token"]
    assert registration["executable_module"] == "zerg.qa.antigravity_launch_hook_inbox"


def test_main_registration_flag_prints_registration(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = antigravity_launch_hook_inbox.main(["--registration"])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == antigravity_launch_hook_inbox.REGISTRATION.to_dict()


def test_hook_inbox_launch_pass_writes_admissible_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_execute(monkeypatch, hook_inbox_outcome=AssertionOutcome.PASS)
    binary = _fake_binary(tmp_path)
    evidence_root = tmp_path / "evidence"
    args = _run_args(tmp_path, evidence_root=evidence_root, provider_bin=binary)

    result = antigravity_launch_hook_inbox.run_hook_inbox_launch(args)

    assert result["status"] == "pass"
    assert result["provider"] == "antigravity"
    # Authored schema variant is None (no `variant:` declared for this
    # assertion) -- must not be the derived execution-variant CLI value.
    assert result["variant"] is None
    assert result["scenario_id"] == "antigravity_hook_inbox"
    assert result["scenario_revision"] == antigravity_launch_hook_inbox.REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_no_token"
    assert result["producer"]["producer_id"] == "antigravity.hook_inbox_launch.v1"
    assert result["assertions"] == {"hook_inbox_contract_preserved": True}
    assert isinstance(result["observation"], dict)
    assert result["observation"]["hook_inbox_contract_preserved"] is True
    assert result["observation"]["real_agy_binary_invoked"] is True
    assert isinstance(result["artifact_manifest"], list)
    assert result["artifact_manifest"]

    written = json.loads((evidence_root / "result.json").read_text())
    assert written == result
    manifest_paths = {entry["path"] for entry in result["artifact_manifest"]}
    assert "provider-binary-receipt.json" in manifest_paths
    assert "no-token-canary-result.json" in manifest_paths
    assert "hook-inbox-assertions.json" in manifest_paths
    assert "cleanup-receipt.json" in manifest_paths
    cleanup = json.loads((evidence_root / "cleanup-receipt.json").read_text())
    assert cleanup == {"verified": True, "orphan_count": 0}


def test_hook_inbox_launch_fail_when_oracle_reports_semantic_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_execute(monkeypatch, hook_inbox_outcome=AssertionOutcome.SEMANTIC_FAIL)
    binary = _fake_binary(tmp_path)
    evidence_root = tmp_path / "evidence"
    args = _run_args(tmp_path, evidence_root=evidence_root, provider_bin=binary)

    result = antigravity_launch_hook_inbox.run_hook_inbox_launch(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {"hook_inbox_contract_preserved": False}


def test_hook_inbox_launch_fails_closed_on_unexpected_assertion_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute(binary, evidence_root):  # noqa: ANN001
        # Only one assertion instead of the declared two -- must not be
        # silently accepted.
        assertions = (
            SemanticAssertion("hook_inbox_contract_preserved", AssertionOutcome.PASS, EvidenceClass.LIVE_NO_TOKEN),
        )
        return {"status": "blocked", "no_token_canary": _fake_no_token_canary_artifact()}, assertions, ()

    monkeypatch.setattr(antigravity_launch_hook_inbox.antigravity_hook_qualification, "_execute", fake_execute)
    binary = _fake_binary(tmp_path)
    evidence_root = tmp_path / "evidence"
    args = _run_args(tmp_path, evidence_root=evidence_root, provider_bin=binary)

    result = antigravity_launch_hook_inbox.run_hook_inbox_launch(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "antigravity_launch_hook_inbox_failed"
    assert "assertion set" in result["error"]
    assert result["assertions"] == {"hook_inbox_contract_preserved": False}


def test_main_rejects_missing_provider_binary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing_binary = tmp_path / "agy-missing"
    evidence_root = tmp_path / "evidence"

    exit_code = antigravity_launch_hook_inbox.main(
        [
            "--variant",
            "cell:antigravity:hook_inbox_contract_preserved:antigravity_hook_inbox",
            "--evidence-root",
            str(evidence_root),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
            "--provider-bin",
            str(missing_binary),
        ]
    )

    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "fail"
    assert printed["failure_code"] == "antigravity_binary_missing"
    assert not evidence_root.exists()


def test_main_pass_exit_code_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _install_fake_execute(monkeypatch, hook_inbox_outcome=AssertionOutcome.PASS)
    binary = _fake_binary(tmp_path)
    evidence_root = tmp_path / "evidence"

    exit_code = antigravity_launch_hook_inbox.main(
        [
            "--variant",
            "cell:antigravity:hook_inbox_contract_preserved:antigravity_hook_inbox",
            "--evidence-root",
            str(evidence_root),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
            "--provider-bin",
            str(binary),
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "pass"
    os.stat(evidence_root / "result.json")
