from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys

import pytest

from zerg.qa.codex_native_resume import REGISTRATION
from zerg.qa.resume_assurance import capability_contract_shape
from zerg.qa.resume_assurance import compile_resume_plan
from zerg.qa.resume_assurance import content_digest
from zerg.services.provider_capability_schema import load_capability_assertions


def _inputs() -> dict:
    contract = capability_contract_shape(
        load_capability_assertions(),
        provider="codex",
        capability="session.resume.helm",
    )
    selected = [
        {
            "provider": "codex",
            "capability": "session.resume.helm",
            "assertion_id": "native_provider_resume_proven",
            "variant": variant,
        }
        for variant in ("clean_exit", "process_loss")
    ]
    producer = {
        "registration": REGISTRATION.to_dict(),
        "code_digest": "sha256:" + "a" * 64,
        "oracle_digest": "sha256:" + "b" * 64,
    }
    protected_inputs = {
        "schemas/managed_providers.yml": "sha256:" + "c" * 64,
        "server/zerg/qa/codex_native_resume.py": producer["code_digest"],
        "server/zerg/qa/provider_resume_oracles.py": producer["oracle_digest"],
        "server/zerg/qa/resume_assurance.py": "sha256:" + "d" * 64,
        "engine/src/codex_bridge.rs": "sha256:" + "e" * 64,
    }
    epoch = {
        "schema_version": 1,
        "epoch_id": "helm-resume-v1-test",
        "profile": "helm_resume_v1",
        "accepted_longhouse_sha": "2" * 40,
        "contract_shape": copy.deepcopy(contract),
        "selected_cells": copy.deepcopy(selected),
        "protected_inputs": dict(protected_inputs),
        "verifier_bundle_digest": "sha256:" + "f" * 64,
        "compiler_digest": protected_inputs["server/zerg/qa/resume_assurance.py"],
        "producers": [copy.deepcopy(producer)],
        "protected_path_authority": "human_review",
        "automation_policy_version": "factory-automation-v1",
        "closure_rules": ["same_assertion_variant_approved_successor"],
    }
    epoch["epoch_digest"] = content_digest(epoch, "epoch_digest")
    census = {
        "schema_version": 1,
        "artifact_kind": "provider_factory_worker_census",
        "worker_id": "clifford:provider-factory",
        "factory_source_sha": "1" * 40,
        "longhouse_source_sha": "2" * 40,
        "verifier_bundle_digest": epoch["verifier_bundle_digest"],
        "compiler_digest": epoch["compiler_digest"],
        "producers": [copy.deepcopy(producer)],
        "platform": "linux",
        "architecture": "x86_64",
        "sandbox_policy": "provider-qualification-bwrap-v3",
        "network_policy": "shared_provider_egress",
        "credential_binding_ids": ["codex_provider_token", "runtime_host_control"],
        "acquisition_methods": ["staged_release"],
    }
    census["census_digest"] = content_digest(census, "census_digest")
    return {
        "accepted_epoch": epoch,
        "current_contract": contract,
        "protected_inputs": protected_inputs,
        "factory_source_sha": census["factory_source_sha"],
        "worker_census": census,
        "subject": {
            "subject_id": "helm-resume-v1:fixture",
            "longhouse_source_sha": census["longhouse_source_sha"],
        },
        "subjects": {
            "codex": {
                "subject_id": "codex:0.999.0:fixture",
                "longhouse_source_sha": census["longhouse_source_sha"],
                "provider_artifact": {
                    "provider": "codex",
                    "version": "0.999.0",
                    "executable_identity": "sha256:" + "9" * 64,
                    "build_identity": "sha256:" + "8" * 64,
                    "acquisition_method": "staged_release",
                    "platform": "linux",
                    "architecture": "x86_64",
                    "entrypoint": "/provider-builds/codex/0.999.0/provider",
                    "build_root": "/provider-builds/codex/0.999.0",
                },
            },
        },
        "scheduling": {
            "requested_cells": copy.deepcopy(selected),
            "evaluated_at": "2026-08-03T00:00:00Z",
            "changed_paths": [],
            "decisions": [
                {
                    "cell": copy.deepcopy(cell),
                    "action": "execute",
                    "reason": "never_proven",
                    "priority": "release_gate",
                    "timeout_seconds": 600,
                    "max_cost_usd": 2.0,
                }
                for cell in selected
            ],
            "max_concurrency": 1,
            "total_execute_cost_budget_usd": 4.0,
        },
    }


def _codes(compiled: dict) -> set[str]:
    return {item["code"] for item in compiled["report"]["diagnostics"]}


def test_compiler_emits_deterministic_two_variant_plan() -> None:
    inputs = _inputs()
    first = compile_resume_plan(inputs)
    second = compile_resume_plan(copy.deepcopy(inputs))

    assert first == second
    assert first["report"]["valid"] is True
    assert first["report"]["report_digest"].startswith("sha256:")
    assert first["plan"]["plan_digest"] == first["report"]["plan_digest"]
    assert [command["variant"] for command in first["plan"]["commands"]] == [
        "clean_exit",
        "process_loss",
    ]
    assert {command["module"] for command in first["plan"]["commands"]} == {"zerg.qa.codex_native_resume"}


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    (
        (
            lambda value: value["current_contract"].pop(
                next(
                    index
                    for index, cell in enumerate(value["current_contract"])
                    if cell["assertion_id"] == "native_provider_resume_proven" and cell["variant"] == "process_loss"
                )
            ),
            "contract_cell_removed",
        ),
        (
            lambda value: next(
                cell for cell in value["current_contract"] if cell["assertion_id"] == "native_provider_resume_proven"
            ).update(minimum_scenario_revision=1),
            "minimum_scenario_revision_downgraded",
        ),
        (
            lambda value: next(cell for cell in value["current_contract"] if cell["assertion_id"] == "native_provider_resume_proven")[
                "acceptable_evidence"
            ].append("hermetic"),
            "acceptable_evidence_broadened",
        ),
        (
            lambda value: value["worker_census"].update(credential_binding_ids=["codex_provider_token"]),
            "eligible_producer_missing",
        ),
        (
            lambda value: value["worker_census"]["producers"][0]["registration"].update(scenario_revision=1),
            "eligible_producer_missing",
        ),
        (
            lambda value: value["protected_inputs"].update({"server/zerg/qa/codex_native_resume.py": "sha256:" + "0" * 64}),
            "protected_input_digest_mismatch",
        ),
        (
            lambda value: value["accepted_epoch"].update(accepted_longhouse_sha="3" * 40),
            "accepted_longhouse_source_mismatch",
        ),
        (
            lambda value: value["subjects"]["codex"]["provider_artifact"].update(architecture="aarch64"),
            "eligible_producer_missing",
        ),
    ),
)
def test_compiler_retains_invalid_report_and_no_plan(mutate, expected_code: str) -> None:
    inputs = _inputs()
    mutate(inputs)
    census = inputs["worker_census"]
    census["census_digest"] = content_digest(census, "census_digest")

    compiled = compile_resume_plan(inputs)

    assert compiled["report"]["valid"] is False
    assert compiled["plan"] is None
    assert expected_code in _codes(compiled)


def test_compiler_rejects_silent_variant_omission_before_execution() -> None:
    inputs = _inputs()
    inputs["scheduling"]["requested_cells"] = inputs["scheduling"]["requested_cells"][:1]

    compiled = compile_resume_plan(inputs)

    assert compiled["plan"] is None
    assert "scheduled_cell_omission" in _codes(compiled)


def test_compiler_reuses_only_exact_fresh_published_proof() -> None:
    inputs = _inputs()
    cell = inputs["scheduling"]["decisions"][0]["cell"]
    artifact = inputs["subjects"]["codex"]["provider_artifact"]
    inputs["scheduling"]["decisions"][0].update(
        action="reuse",
        reason="fresh_exact_proof",
        proof={
            "artifact_id": "a" * 64,
            "provider": "codex",
            "assertion_id": cell["assertion_id"],
            "variant": cell["variant"],
            "evidence_class": "live_token",
            "provider_version": artifact["version"],
            "provider_executable_identity": artifact["executable_identity"],
            "provider_build_identity": artifact["build_identity"],
            "longhouse_source_sha": inputs["subject"]["longhouse_source_sha"],
            "accepted_epoch_digest": inputs["accepted_epoch"]["epoch_digest"],
            "generated_at": "2026-08-02T23:59:00Z",
            "publication_message_id": "provider-assurance-proof:fixture",
        },
    )

    compiled = compile_resume_plan(inputs)

    assert compiled["report"]["valid"] is True
    assert len(compiled["plan"]["commands"]) == 1
    assert compiled["plan"]["reused_proofs"][0]["artifact_id"] == "a" * 64


def test_compiler_rejects_nearby_reuse_variant() -> None:
    inputs = _inputs()
    decision = inputs["scheduling"]["decisions"][0]
    artifact = inputs["subjects"]["codex"]["provider_artifact"]
    decision.update(
        action="reuse",
        proof={
            "artifact_id": "a" * 64,
            "provider": "codex",
            "assertion_id": decision["cell"]["assertion_id"],
            "variant": "nearby-variant",
            "evidence_class": "live_token",
            "provider_version": artifact["version"],
            "provider_executable_identity": artifact["executable_identity"],
            "provider_build_identity": artifact["build_identity"],
            "longhouse_source_sha": inputs["subject"]["longhouse_source_sha"],
            "accepted_epoch_digest": inputs["accepted_epoch"]["epoch_digest"],
            "generated_at": "2026-08-02T23:59:00Z",
            "publication_message_id": "provider-assurance-proof:fixture",
        },
    )

    compiled = compile_resume_plan(inputs)

    assert compiled["report"]["valid"] is False
    assert "scheduled_proof_not_reusable" in _codes(compiled)


def test_compiler_cli_is_deterministic_across_fresh_processes() -> None:
    payload = json.dumps(_inputs())
    command = [sys.executable, "-m", "zerg.qa.resume_assurance"]
    first = subprocess.run(command, input=payload, text=True, capture_output=True, check=True)
    second = subprocess.run(command, input=payload, text=True, capture_output=True, check=True)

    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["report"]["valid"] is True


def test_native_resume_oracle_requires_post_resume_activity_and_variant_cleanup() -> None:
    from zerg.qa.provider_resume_oracles import native_resume_assertions

    observation = {
        "same_longhouse_session": True,
        "same_provider_thread": True,
        "new_run": True,
        "new_connection": True,
        "new_app_server_process": True,
        "provider_neutral_resume_intent": True,
        "native_resume_command": True,
        "bridge_subscribed": True,
        "post_resume_provider_activity": True,
        "post_resume_marker_in_assistant_transcript": True,
        "stale_input_rejected": True,
        "stale_generation_dispatched": False,
        "concurrent_resume_refused": True,
        "artifact_secret_scan_passed": True,
        "final_cleanup_verified": True,
        "orphan_count": 0,
        "clean_stop_verified": True,
    }
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": True}
    observation["stale_generation_dispatched"] = True
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": False}
    observation["stale_generation_dispatched"] = False
    observation["post_resume_provider_activity"] = False
    assert native_resume_assertions("clean_exit", observation) == {"native_provider_resume_proven": False}


def test_process_loss_targets_only_recorded_bridge_and_provider_processes(monkeypatch) -> None:
    from pathlib import Path

    from zerg.qa import codex_native_resume

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(codex_native_resume, "_pid_alive", lambda pid: pid in {101, 202})
    monkeypatch.setattr(
        codex_native_resume,
        "_proc_command",
        lambda pid: "/opt/bin/longhouse-engine codex-bridge" if pid == 101 else "/build/provider app-server",
    )
    monkeypatch.setattr(codex_native_resume, "_wait_dead", lambda pid, timeout=10: pid in {101, 202})
    monkeypatch.setattr(
        codex_native_resume,
        "_process_start_time",
        lambda pid: "bridge-start" if pid == 101 else "provider-start",
    )
    monkeypatch.setattr(
        codex_native_resume.bridge_canary,
        "_read_json",
        lambda _path: {"terminal_state": "session_ended", "terminal_published": True},
    )
    monkeypatch.setattr(codex_native_resume.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    receipt = codex_native_resume._force_process_loss(
        {
            "pid": 101,
            "bridge_process_start_time": "bridge-start",
            "app_server_pid": 202,
            "app_server_process_start_time": "provider-start",
        },
        Path("/build/provider"),
        Path("/tmp/provider-factory-test-state.json"),
    )

    assert [pid for pid, _ in killed] == [202, 101]
    assert receipt["bridge"]["dead"] is True
    assert receipt["app_server"]["dead"] is True
    assert receipt["bridge"]["terminal_commit_observed"] is True


def test_process_loss_refuses_reused_pid(monkeypatch) -> None:
    from pathlib import Path

    from zerg.qa import codex_native_resume

    monkeypatch.setattr(codex_native_resume, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(codex_native_resume, "_proc_command", lambda _pid: "/bin/longhouse-engine provider app-server")
    monkeypatch.setattr(codex_native_resume, "_process_start_time", lambda _pid: "different-start")

    with pytest.raises(RuntimeError, match="start identity"):
        codex_native_resume._force_process_loss(
            {
                "pid": 101,
                "bridge_process_start_time": "bridge-start",
                "app_server_pid": 202,
                "app_server_process_start_time": "provider-start",
            },
            Path("/build/provider"),
            Path("/tmp/provider-factory-test-state.json"),
        )


def test_retained_artifact_secret_scan_redacts_and_reports_leak(tmp_path) -> None:
    from zerg.qa import codex_native_resume

    artifact = tmp_path / "receipt.json"
    artifact.write_text('{"token":"secret-value"}\n', encoding="utf-8")

    redacted = codex_native_resume._redact_retained_secrets(tmp_path, ["secret-value"])

    assert redacted == ["receipt.json"]
    assert "secret-value" not in artifact.read_text(encoding="utf-8")


def test_direct_producer_registration_names_real_evidence_and_cleanup() -> None:
    registration = REGISTRATION.to_dict()

    assert registration["scenario_revision"] == 4
    assert registration["assertion_cells"] == [
        {"assertion_id": "native_provider_resume_proven", "variant": "clean_exit"},
        {"assertion_id": "native_provider_resume_proven", "variant": "process_loss"},
    ]
    assert registration["observed_activity"] == [
        "provider_neutral_resume_intent",
        "native_resume_command",
        "post_resume_provider_activity",
        "stale_input_rejected",
        "concurrent_resume_refused",
        "artifact_secret_scan_passed",
    ]
    assert "resume_intent_receipt" in registration["required_artifacts"]
    assert "cleanup_receipt" in registration["required_artifacts"]
    assert "no_orphan_provider_processes" in registration["required_cleanup"]


def test_direct_producer_registration_cli_needs_no_execution_arguments(capsys) -> None:
    from zerg.qa.codex_native_resume import main

    assert main(["--registration"]) == 0
    assert "codex.native_resume.v1" in capsys.readouterr().out


def test_codex_resume_intent_maps_exact_session_to_provider_thread(tmp_path) -> None:
    from zerg.qa.codex_native_resume import _validate_resume_intent

    args = argparse.Namespace(repo_root=tmp_path / "repo")
    session_id = "11111111-1111-4111-8111-111111111111"
    intent = {
        "session_id": session_id,
        "provider": "codex",
        "cwd": str(args.repo_root),
        "available": True,
        "argv": ["longhouse", "codex", "--cwd", str(args.repo_root), "--resume-session", session_id],
        "handoff": "terminal_command",
    }

    receipt = _validate_resume_intent(args, session_id, intent, cwd=args.repo_root)

    assert receipt["identity_valid"] is True
