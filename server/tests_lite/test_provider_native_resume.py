from __future__ import annotations

import argparse
import json
from pathlib import Path

from zerg.qa import antigravity_resume_policy
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import registration_for


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        longhouse_cli=tmp_path / "longhouse",
        repo_root=tmp_path / "repo",
        api_url="https://runtime.example",
        agents_token="device-token",
        provider_bin=tmp_path / "provider",
    )


def test_each_native_provider_registers_both_exact_resume_variants() -> None:
    for provider in ("claude", "cursor", "opencode"):
        registration = registration_for(provider)
        assert registration.providers == (provider,)
        assert registration.assertion_cells == (
            ("native_provider_resume_proven", "clean_exit"),
            ("native_provider_resume_proven", "process_loss"),
        )
        assert registration.evidence_classes == ("live_token",)
        assert registration.executable is True
        assert registration.executable_module == SPECS[provider].executable_module


def test_shipped_facade_receives_provider_native_resume_selector(tmp_path: Path) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"

    for provider, selector in (
        ("claude", "--resume"),
        ("cursor", "--resume-session"),
        ("opencode", "--resume-session"),
    ):
        command = _launch_command(SPECS[provider], args, session_id)
        assert command[:2] == [str(args.longhouse_cli), provider]
        assert command[command.index(selector) + 1] == session_id
        assert command[command.index(SPECS[provider].binary_flag) + 1] == str(args.provider_bin)


def test_antigravity_policy_proof_has_no_registration_or_spawn(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(tmp_path),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
        ]
    )

    result = json.loads((evidence / "result.json").read_text())
    assert exit_code == 0
    assert result["status"] == "pass"
    assert result["observation"] == {
        "disposition": "policy_disabled",
        "provider_spawn_count": 0,
        "registration_count": 0,
    }
    assert json.loads((evidence / "cleanup-receipt.json").read_text())["orphan_count"] == 0
