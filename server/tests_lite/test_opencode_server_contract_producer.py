from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from zerg.qa import opencode_server_contract_producer as m
from zerg.qa.resume_assurance import capability_contract_shape
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_schema import load_capability_assertions


def _schema_cells() -> dict[str, dict[str, Any]]:
    launch = capability_contract_shape(
        load_capability_assertions(),
        provider="opencode",
        capability="session.launch.helm",
    )
    reattach = capability_contract_shape(
        load_capability_assertions(),
        provider="opencode",
        capability="session.reattach.helm",
    )
    assert len(launch) == 1
    assert len(reattach) == 1
    return {"session.launch.helm": launch[0], "session.reattach.helm": reattach[0]}


def test_registration_matches_both_schema_declared_cells_exactly() -> None:
    """Guard against a hand-typo'd REGISTRATION drifting from managed_providers.yml."""

    cells = _schema_cells()
    launch_cell = cells["session.launch.helm"]
    reattach_cell = cells["session.reattach.helm"]

    assert launch_cell["assertion_id"] == "serve_session_contract_preserved"
    assert reattach_cell["assertion_id"] == "process_restart_reattach_preserved"
    assert launch_cell["variant"] is None
    assert reattach_cell["variant"] is None
    # Both cells share one scenario_id -- this producer's whole design (one
    # real run answers both) depends on that being true.
    assert launch_cell["scenario_id"] == reattach_cell["scenario_id"] == m.REGISTRATION.scenario_id
    assert launch_cell["oracle_source"] == reattach_cell["oracle_source"] == m.REGISTRATION.oracle_source
    assert set(m.REGISTRATION.assertion_cells) == {
        (launch_cell["assertion_id"], launch_cell["variant"]),
        (reattach_cell["assertion_id"], reattach_cell["variant"]),
    }
    assert "live_no_token" in launch_cell["acceptable_evidence"]
    assert "live_no_token" in reattach_cell["acceptable_evidence"]
    assert m.REGISTRATION.evidence_classes == ("live_no_token",)
    assert m.REGISTRATION.providers == ("opencode",)
    assert m.REGISTRATION.executable is True
    assert m.REGISTRATION.executable_module == "zerg.qa.opencode_server_contract_producer"


def test_cli_registration_flag_prints_registration_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = m.main(["--registration"])
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == m.REGISTRATION.to_dict()


def test_requested_assertion_id_parses_the_synthetic_execution_variant_key() -> None:
    assert (
        m.requested_assertion_id("cell:opencode:serve_session_contract_preserved:opencode_server_contract")
        == "serve_session_contract_preserved"
    )
    assert (
        m.requested_assertion_id("cell:opencode:process_restart_reattach_preserved:opencode_server_contract")
        == "process_restart_reattach_preserved"
    )


def test_requested_assertion_id_accepts_a_bare_assertion_id_fallback() -> None:
    assert m.requested_assertion_id("serve_session_contract_preserved") == "serve_session_contract_preserved"


def test_requested_assertion_id_rejects_anything_else() -> None:
    with pytest.raises(RuntimeError, match="unrecognized --variant"):
        m.requested_assertion_id("cell:opencode:some_other_assertion:opencode_server_contract")


def test_no_orphan_opencode_server_processes_fails_open_without_proc(tmp_path: Path) -> None:
    # This dev machine is not Linux, so /proc genuinely does not exist; the
    # function's own OSError fallback is exercised for real, not mocked.
    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake")
    assert m._no_orphan_opencode_server_processes(provider_bin) is True


def test_no_orphan_opencode_server_processes_detects_a_leaked_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake")
    resolved = str(provider_bin.resolve())

    monkeypatch.setattr(m.os, "listdir", lambda path: ["123"] if path == "/proc" else [])

    def fake_read_bytes(self: Path) -> bytes:
        if str(self) == "/proc/123/cmdline":
            return (resolved + "\x00serve\x00--hostname\x00127.0.0.1\x00").encode()
        raise OSError("not found")

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    assert m._no_orphan_opencode_server_processes(provider_bin) is False


def test_no_orphan_opencode_server_processes_ignores_unrelated_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake")

    monkeypatch.setattr(m.os, "listdir", lambda path: ["123"] if path == "/proc" else [])

    def fake_read_bytes(self: Path) -> bytes:
        if str(self) == "/proc/123/cmdline":
            return b"/usr/bin/some-other-process\x00--flag\x00"
        raise OSError("not found")

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    assert m._no_orphan_opencode_server_processes(provider_bin) is True


def _args(tmp_path: Path, *, variant: str) -> argparse.Namespace:
    return argparse.Namespace(
        evidence_root=tmp_path / "evidence",
        variant=variant,
        repo_root=tmp_path / "repo",
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=tmp_path / "opencode",
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    launch_passes: bool,
    reattach_passes: bool,
) -> None:
    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake-opencode-binary")

    def fake_execute(binary: Path, evidence_root: Path):
        evidence_root.mkdir(parents=True, exist_ok=True)
        observation = {"status": "pass" if launch_passes and reattach_passes else "fail", "provider_live_canary": {"canaries": {}}}
        assertions = (
            m.opencode_server_qualification.semantic.SemanticAssertion(
                "serve_session_contract_preserved",
                AssertionOutcome.PASS if launch_passes else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_NO_TOKEN,
            ),
            m.opencode_server_qualification.semantic.SemanticAssertion(
                "process_restart_reattach_preserved",
                AssertionOutcome.PASS if reattach_passes else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_NO_TOKEN,
            ),
        )
        return observation, assertions, ()

    monkeypatch.setattr(m.opencode_server_qualification, "_execute", fake_execute)
    monkeypatch.setattr(m, "_no_orphan_opencode_server_processes", lambda *_a, **_k: True)


@pytest.mark.parametrize(
    "assertion_id,launch_passes,reattach_passes,expected_status",
    [
        ("serve_session_contract_preserved", True, True, "pass"),
        ("serve_session_contract_preserved", True, False, "pass"),
        ("process_restart_reattach_preserved", True, False, "fail"),
        ("process_restart_reattach_preserved", True, True, "pass"),
    ],
)
def test_run_server_contract_reports_status_for_the_requested_cell_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assertion_id: str,
    launch_passes: bool,
    reattach_passes: bool,
    expected_status: str,
) -> None:
    """One shared run must not let the other assertion's outcome leak into this cell's status."""

    _install_fakes(monkeypatch, tmp_path, launch_passes=launch_passes, reattach_passes=reattach_passes)
    args = _args(tmp_path, variant=f"cell:opencode:{assertion_id}:opencode_server_contract")

    result = m.run_server_contract(args)

    assert result["status"] == expected_status
    assert result["provider"] == "opencode"
    assert result["variant"] is None
    assert result["scenario_id"] == m.REGISTRATION.scenario_id
    assert result["scenario_revision"] == m.REGISTRATION.scenario_revision
    assert result["evidence_class"] == "live_no_token"
    assert result["producer"]["producer_id"] == m.REGISTRATION.producer_id
    assert result["assertions"][assertion_id] is (expected_status == "pass")
    assert isinstance(result["observation"], dict)
    assert isinstance(result["artifact_manifest"], list)
    assert result["artifact_manifest"]

    on_disk = json.loads((args.evidence_root / "result.json").read_text())
    assert on_disk == result

    for relative in (
        "provider-binary-receipt.json",
        "provider-live-canary-artifact.json",
        "server-contract-receipt.json",
        "cleanup-receipt.json",
    ):
        assert (args.evidence_root / relative).is_file(), relative


def test_run_server_contract_rejects_an_unexpected_assertion_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_bin = tmp_path / "opencode"
    provider_bin.write_bytes(b"fake")

    def fake_execute(binary: Path, evidence_root: Path):
        evidence_root.mkdir(parents=True, exist_ok=True)
        assertions = (
            m.opencode_server_qualification.semantic.SemanticAssertion(
                "some_unexpected_assertion", AssertionOutcome.PASS, EvidenceClass.LIVE_NO_TOKEN
            ),
        )
        return {"status": "pass"}, assertions, ()

    monkeypatch.setattr(m.opencode_server_qualification, "_execute", fake_execute)
    args = _args(tmp_path, variant="cell:opencode:serve_session_contract_preserved:opencode_server_contract")

    with pytest.raises(RuntimeError, match="unexpected assertion set"):
        m.run_server_contract(args)


def test_main_requires_the_provider_binary_to_exist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = m.main(
        [
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--variant",
            "cell:opencode:serve_session_contract_preserved:opencode_server_contract",
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
            "--provider-bin",
            str(tmp_path / "missing-opencode"),
        ]
    )
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_code"] == "opencode_binary_missing"
