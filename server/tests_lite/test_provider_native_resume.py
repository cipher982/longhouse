from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from zerg.managed_provider_contract_manifest import managed_provider_contract_entry_digest
from zerg.qa import antigravity_resume_policy
from zerg.qa import codex_native_resume
from zerg.qa.codex_native_resume import _write_json as write_codex_json
from zerg.qa.provider_native_resume import SPECS
from zerg.qa.provider_native_resume import _cleanup_processes
from zerg.qa.provider_native_resume import _command_from_resume_intent
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _launch_command
from zerg.qa.provider_native_resume import _provider_process_pid
from zerg.qa.provider_native_resume import _state_candidates
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


def test_claude_resume_probe_follows_native_channel_state_root(tmp_path: Path) -> None:
    state = tmp_path / ".claude" / "channels" / "longhouse" / "sessions" / "session.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")

    assert state in _state_candidates(SPECS["claude"], tmp_path)


def test_codex_resume_receipts_normalize_path_values(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    write_codex_json(receipt, {"path": tmp_path / "provider"})

    assert json.loads(receipt.read_text()) == {"path": str(tmp_path / "provider")}


def test_native_resume_rejects_normal_provider_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", "/root")

    with pytest.raises(RuntimeError, match="isolated provider HOME"):
        _isolated_provider_home()


def test_native_resume_requires_the_factory_sandbox_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "provider-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LONGHOUSE_QUALIFICATION_SANDBOX", raising=False)
    monkeypatch.delenv("LONGHOUSE_QUALIFICATION_HOME", raising=False)

    with pytest.raises(RuntimeError, match="qualification sandbox"):
        _isolated_provider_home()


def test_native_resume_accepts_disposable_provider_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "provider-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_SANDBOX", "provider-qualification-bwrap-v3")
    monkeypatch.setenv("LONGHOUSE_QUALIFICATION_HOME", str(home))

    assert _isolated_provider_home() == home


def test_codex_native_resume_tui_uses_the_bridge_provider_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/root")
    isolation_root = tmp_path / "isolation"

    environment = codex_native_resume._native_resume_tui_environment(isolation_root, "session-1")

    assert environment["HOME"] == str(isolation_root / "provider-home")
    assert environment["CODEX_HOME"] == str(isolation_root / "provider-home" / ".codex")
    assert environment["LONGHOUSE_MANAGED_SESSION_ID"] == "session-1"


def test_codex_main_serializes_path_values_in_result_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    engine = tmp_path / "longhouse-engine"
    provider = tmp_path / "codex"
    for executable in (engine, provider):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
    result_path = tmp_path / "evidence"

    monkeypatch.setenv("CODEX_API_URL", "https://runtime.example")
    monkeypatch.setenv("CODEX_AGENTS_TOKEN", "device-token")
    monkeypatch.setattr(
        codex_native_resume,
        "run_native_resume",
        lambda args: {"status": "pass", "path": result_path},
    )

    assert (
        codex_native_resume.main(
            [
                "--variant",
                "clean_exit",
                "--evidence-root",
                str(tmp_path),
                "--repo-root",
                str(tmp_path),
                "--engine",
                str(engine),
                "--codex-bin",
                str(provider),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["path"] == str(result_path)


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


@pytest.mark.parametrize(
    ("provider", "selector"),
    (("claude", "--resume"), ("cursor", "--resume-session"), ("opencode", "--resume-session")),
)
def test_resume_command_is_derived_from_exact_provider_neutral_intent(
    tmp_path: Path,
    provider: str,
    selector: str,
) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "session_id": session_id,
        "provider": provider,
        "machine_id": "factory-worker",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", provider, "--cwd", str(args.repo_root), selector, session_id],
        "command": f"longhouse {provider}",
        "handoff": "terminal_command",
    }

    command, receipt = _command_from_resume_intent(SPECS[provider], args, session_id, intent)

    assert receipt["identity_valid"] is True
    assert command[:2] == [str(args.longhouse_cli), provider]
    assert command[command.index(selector) + 1] == session_id
    assert command[command.index(SPECS[provider].binary_flag) + 1] == str(args.provider_bin)
    assert args.agents_token in command
    assert args.agents_token not in json.dumps(receipt)
    assert "<redacted>" in receipt["executed_argv"]


def test_resume_command_rejects_nearby_session_intent(tmp_path: Path) -> None:
    args = _args(tmp_path)
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "session_id": "22222222-2222-4222-8222-222222222222",
        "provider": "claude",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", "claude", "--cwd", str(args.repo_root), "--resume", session_id],
        "handoff": "terminal_command",
    }

    with pytest.raises(RuntimeError, match="exact session"):
        _command_from_resume_intent(SPECS["claude"], args, session_id, intent)


def test_provider_process_identity_comes_from_exact_provider_state() -> None:
    states = {
        "claude": {"claude_pid": 101},
        "cursor": {"cursor_pid": 202},
        "opencode": {"pid": 303},
    }

    for provider, state in states.items():
        assert _provider_process_pid(SPECS[provider], state) == next(iter(state.values()))


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "101"])
def test_provider_process_identity_rejects_missing_or_invalid_pid(invalid: object) -> None:
    with pytest.raises(RuntimeError, match="positive claude_pid"):
        _provider_process_pid(SPECS["claude"], {"claude_pid": invalid})


def test_cleanup_retains_failed_pid_identity_as_unverified_receipt() -> None:
    receipt = _cleanup_processes(SPECS["claude"], (), [{"session_id": "session-without-provider-pid"}])

    assert receipt["verified"] is False
    assert receipt["orphan_count"] == 1
    assert receipt["provider_pid_errors"][0]["session_id"] == "session-without-provider-pid"


def test_antigravity_policy_proof_has_no_registration_or_spawn(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    # The producer must read the generated, digest-pinned contract that the
    # factory mounts as --repo-root; a synthetic tmp_path would bypass that seam.
    repo_root = Path(__file__).resolve().parents[2]
    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo_root),
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
    source = json.loads((evidence / "policy-source-receipt.json").read_text())
    assert source["reattach"] is False
    assert source["resume_capability"]["disposition"] == "policy_disabled"
    assert source["scenario_result"]["status"] == "pass"
    assert json.loads((evidence / "cleanup-receipt.json").read_text())["orphan_count"] == 0


def test_antigravity_policy_contract_digest_matches_canonical_authority() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    _contract, digest = antigravity_resume_policy._policy_contract(repo_root)

    assert digest == managed_provider_contract_entry_digest("antigravity")


@pytest.mark.parametrize(
    ("reattach", "disposition"),
    ((True, "policy_disabled"), (False, "implemented")),
)
def test_antigravity_policy_proof_fails_closed_when_contract_enables_resume(
    tmp_path: Path,
    reattach: bool,
    disposition: str,
) -> None:
    manifest_path = tmp_path / "server/zerg/config/managed_provider_contracts.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": [
                    {
                        "provider": "antigravity",
                        "reattach": reattach,
                        "capabilities": {"session.resume.helm": {"disposition": disposition}},
                    }
                ],
            }
        )
    )
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
    assert result["status"] == "fail"
    source = json.loads((evidence / "policy-source-receipt.json").read_text())
    assert source["scenario_result"]["failure_code"] == "resume_unsupported_oracle_failed"
