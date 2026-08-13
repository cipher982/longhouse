from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from zerg.qa import claude_turn_start_real_print as m
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass


class _Assertion:
    def __init__(self, assertion_id: str, outcome: AssertionOutcome, evidence_class: EvidenceClass) -> None:
        self.assertion_id = assertion_id
        self.outcome = outcome
        self.evidence_class = evidence_class


def _args(tmp_path: Path) -> argparse.Namespace:
    claude_bin = tmp_path / "claude"
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)
    return argparse.Namespace(
        evidence_root=tmp_path / "evidence",
        claude_bin=claude_bin,
        variant=m._EXECUTION_VARIANT,
    )


def test_registration_matches_the_schemas_declared_cell() -> None:
    assert m.REGISTRATION.producer_id == "claude.turn_start_real_print.v1"
    assert m.REGISTRATION.scenario_id == "claude_real_print"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_ID, None),)
    assert m.REGISTRATION.evidence_classes == ("live_token",)
    assert m.REGISTRATION.providers == ("claude",)
    assert m._EXECUTION_VARIANT == execution_variant_key(
        provider="claude",
        assertion_id=m._ASSERTION_ID,
        scenario_id=m._SCENARIO_ID,
        variant=None,
    )


def test_main_registration_mode_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert m.main(["--registration"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_id"] == "claude.turn_start_real_print.v1"
    assert payload["assertion_cells"] == [{"assertion_id": m._ASSERTION_ID, "variant": None}]


def test_run_turn_start_passes_when_the_real_print_marker_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    secret = "sk-test-credential-value"

    def fake_execute(_binary: Path, _evidence_root: Path):
        observation = {"status": "pass", "real_print_canary": {"status": "pass", "credential_used": secret}}
        assertions = (
            _Assertion("claude_cli_channel_contract_preserved", AssertionOutcome.PASS, EvidenceClass.LIVE_NO_TOKEN),
            _Assertion("real_print_marker_returned", AssertionOutcome.PASS, EvidenceClass.LIVE_TOKEN),
        )
        return observation, assertions, (secret,)

    monkeypatch.setattr(m.real_print_qual, "_execute", fake_execute)

    result = m.run_turn_start_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions".
    assert result["status"] == "pass"
    assert result["assertions"] == {
        "claude_cli_channel_contract_preserved": True,
        "real_print_marker_returned": True,
    }
    assert result["provider"] == "claude"
    assert result["scenario_id"] == m._SCENARIO_ID
    assert result["evidence_class"] == "live_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    # _execute() does not redact secrets itself (run_semantic_profile normally
    # does that); this producer owns that step when it bypasses that caller.
    assert secret not in json.dumps(result["observation"])

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_turn_start_fails_when_the_live_phase_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)

    def fake_execute(_binary: Path, _evidence_root: Path):
        observation = {"status": "blocked", "real_print_canary": {"status": "blocked"}}
        assertions = (
            _Assertion("claude_cli_channel_contract_preserved", AssertionOutcome.PASS, EvidenceClass.LIVE_NO_TOKEN),
            _Assertion("real_print_marker_returned", AssertionOutcome.BLOCKED, EvidenceClass.LIVE_NO_TOKEN),
        )
        return observation, assertions, ()

    monkeypatch.setattr(m.real_print_qual, "_execute", fake_execute)

    result = m.run_turn_start_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"]["real_print_marker_returned"] is False


def test_run_turn_start_records_a_typed_failure_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)

    def _boom(_binary: Path, _evidence_root: Path) -> object:
        raise RuntimeError("claude --print invocation timed out")

    monkeypatch.setattr(m.real_print_qual, "_execute", _boom)

    result = m.run_turn_start_scenario(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "claude_turn_start_real_print_failed"
    assert "timed out" in result["error"]


def test_main_serializes_result_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = tmp_path / "claude"
    provider.write_text("#!/bin/sh\n")
    provider.chmod(0o755)
    monkeypatch.setattr(m, "run_turn_start_scenario", lambda _args: {"status": "pass", "marker": "sentinel"})

    exit_code = m.main(
        [
            "--variant",
            m._EXECUTION_VARIANT,
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--claude-bin",
            str(provider),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["marker"] == "sentinel"


def test_main_rejects_a_variant_it_does_not_recognize(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        m.main(["--variant", "bogus-variant", "--evidence-root", str(tmp_path / "evidence")])
