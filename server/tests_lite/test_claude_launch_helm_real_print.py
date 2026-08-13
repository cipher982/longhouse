from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from zerg.qa import claude_launch_helm_real_print as m
from zerg.qa.resume_assurance import execution_variant_key


def _args(tmp_path: Path) -> argparse.Namespace:
    claude_bin = tmp_path / "claude"
    claude_bin.write_text("#!/bin/sh\n")
    claude_bin.chmod(0o755)
    return argparse.Namespace(
        evidence_root=tmp_path / "evidence",
        claude_bin=claude_bin,
        wait_ready_secs=1.0,
        variant=m._EXECUTION_VARIANT,
    )


def test_registration_matches_the_schemas_declared_cell() -> None:
    assert m.REGISTRATION.producer_id == "claude.launch_helm_real_print.v1"
    assert m.REGISTRATION.scenario_id == "claude_real_print"
    assert m.REGISTRATION.assertion_cells == ((m._ASSERTION_ID, None),)
    assert m.REGISTRATION.evidence_classes == ("live_no_token",)
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
    assert payload["producer_id"] == "claude.launch_helm_real_print.v1"
    assert payload["assertion_cells"] == [{"assertion_id": m._ASSERTION_ID, "variant": None}]


def test_run_launch_helm_passes_on_a_green_no_token_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(
        m,
        "run_provider_live_canary",
        lambda _request: {
            "verdict": "green",
            "failure_code": None,
            "recommendation": None,
            "provider_version": "1.2.3",
            "canaries": {"binary_identity": {"status": "pass"}},
            "operation_evidence": {},
        },
    )

    result = m.run_launch_helm_scenario(args)

    # The hard-won lesson this task warns about: the real validator checks
    # status == "pass" (no "-ed") and a per-assertion boolean in "assertions".
    assert result["status"] == "pass"
    assert result["assertions"] == {m._ASSERTION_ID: True}
    assert result["provider"] == "claude"
    assert result["variant"] is None
    assert result["scenario_id"] == m._SCENARIO_ID
    assert result["evidence_class"] == "live_no_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert isinstance(result["artifact_manifest"], list) and result["artifact_manifest"]

    on_disk = json.loads((args.evidence_root / "result.json").read_text(encoding="utf-8"))
    assert on_disk == result


def test_run_launch_helm_fails_on_a_red_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(
        m,
        "run_provider_live_canary",
        lambda _request: {
            "verdict": "red",
            "failure_code": "claude_binary_identity_failed",
            "recommendation": "reinstall claude",
            "provider_version": None,
            "canaries": {"binary_identity": {"status": "fail"}},
        },
    )

    result = m.run_launch_helm_scenario(args)

    assert result["status"] == "fail"
    assert result["assertions"] == {m._ASSERTION_ID: False}


def test_run_launch_helm_records_a_typed_failure_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)

    def _boom(_request: object) -> object:
        raise RuntimeError("claude binary launch timed out")

    monkeypatch.setattr(m, "run_provider_live_canary", _boom)

    result = m.run_launch_helm_scenario(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "claude_launch_helm_real_print_failed"
    assert "timed out" in result["error"]


def test_main_serializes_result_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = tmp_path / "claude"
    provider.write_text("#!/bin/sh\n")
    provider.chmod(0o755)
    monkeypatch.setattr(m, "run_launch_helm_scenario", lambda _args: {"status": "pass", "marker": "sentinel"})

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
