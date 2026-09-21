from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zerg.qa import antigravity_launch_hook_inbox
from zerg.qa import antigravity_resume_policy
from zerg.qa import cursor_coordination_producer
from zerg.qa import cursor_native_resume
from zerg.qa import cursor_turn_boundary_producer
from zerg.qa import ios_workspace_selection_source_producer
from zerg.qa import live_session_toolkit
from zerg.qa import opencode_native_resume
from zerg.qa import opencode_qualification_profile
from zerg.qa import opencode_server_contract_producer
from zerg.qa import opencode_turn_boundary_quiescent
from zerg.qa import provider_native_resume
from zerg.qa import provider_semantic_qualification
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

COVERED_PRODUCERS = frozenset(
    {
        "zerg.qa.antigravity_launch_hook_inbox",
        "zerg.qa.antigravity_resume_policy",
        "zerg.qa.cursor_coordination_producer",
        "zerg.qa.cursor_native_resume",
        "zerg.qa.cursor_turn_boundary_producer",
        "zerg.qa.ios_workspace_selection_source_producer",
        "zerg.qa.opencode_native_resume",
        "zerg.qa.opencode_server_contract_producer",
        "zerg.qa.opencode_turn_boundary_quiescent",
    }
)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class _FakeShipper:
    def __init__(self) -> None:
        self.receipt = {"status": "started", "events_shipped": 0}
        self.stop_calls = 0

    def flush(self, _label: str) -> dict[str, Any]:
        return {"status": "pass", "events_shipped": 0}

    def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        return {"status": "pass", "stopped": True, "process_dead": True, "process_group_dead": True, "stop_calls": self.stop_calls}

    def capture_cursor_projection_diagnostics(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakePty:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.process = SimpleNamespace(pid=4242, poll=lambda: None)
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def settle(self) -> None:
        return None

    def drain(self) -> bytes:
        return b""

    def close(self) -> None:
        return None

    def wait(self, _timeout: float) -> int:
        return 1

    def kill_group(self, _signal: object) -> None:
        return None


class _FakeCursorSession:
    def __init__(self, session_id: str, provider_cwd: Path) -> None:
        self.session_id = session_id
        self.provider_thread_id = f"thread-{session_id}"
        self.home = provider_cwd
        self.environment = {"HOME": str(provider_cwd)}
        self.process = _FakePty()
        self.provider_cwd = provider_cwd
        self.state = {
            "session_id": session_id,
            "provider_session_id": self.provider_thread_id,
            "run_id": f"run-{session_id}",
            "connection_id": f"connection-{session_id}",
        }


def test_antigravity_launch_failure_keeps_external_evidence_and_fail_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _executable(tmp_path / "agy")
    evidence = tmp_path / "evidence"

    def fake_execute(_binary: Path, _evidence_root: Path):
        declared = antigravity_launch_hook_inbox.semantic_oracles.assertions_for(antigravity_launch_hook_inbox.SCENARIO_ID)
        assertions = tuple(
            provider_semantic_qualification.SemanticAssertion(
                assertion_id,
                AssertionOutcome.SEMANTIC_FAIL if assertion_id == antigravity_launch_hook_inbox.ASSERTION_ID else AssertionOutcome.BLOCKED,
                EvidenceClass.LIVE_NO_TOKEN,
            )
            for assertion_id in declared
        )
        canaries = {name: {"status": "pass"} for name in antigravity_launch_hook_inbox._NO_TOKEN_REQUIRED_CANARIES}
        canaries["hook_inbox_claim_contract"] = {"status": "fail"}
        return {"no_token_canary": {"canaries": canaries}}, assertions, ()

    monkeypatch.setattr(antigravity_launch_hook_inbox.antigravity_hook_qualification, "_execute", fake_execute)
    args = argparse.Namespace(
        variant="cell:antigravity:hook_inbox_contract_preserved:antigravity_hook_inbox",
        evidence_root=evidence,
        provider_bin=binary,
    )

    result = antigravity_launch_hook_inbox.run_hook_inbox_launch(args)

    assert result["status"] == "fail"
    assert result["assertions"]["hook_inbox_contract_preserved"] is False
    assertion_evidence = json.loads((evidence / "hook-inbox-assertions.json").read_text())
    assert assertion_evidence["hook_inbox_contract_preserved"]["outcome"] == "semantic_fail"
    assert assertion_evidence["real_print_injection_observed"]["outcome"] == "blocked"
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["required_cleanup"]["no_orphan_provider_processes"] is True
    assert json.loads((evidence / "result.json").read_text()) == result


def test_antigravity_resume_policy_persists_contract_failure_without_spawn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    manifest = repo / "server/zerg/config/managed_provider_contracts.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "antigravity",
                        "reattach": True,
                        "capabilities": {"session.resume.helm": {"disposition": "policy_disabled"}},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"

    exit_code = antigravity_resume_policy.main(
        [
            "--variant",
            "policy_disabled",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo),
            "--engine",
            str(tmp_path / "engine"),
            "--longhouse-cli",
            str(tmp_path / "longhouse"),
        ]
    )

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 0  # This atomic policy producer reports assertion status in result.json.
    assert printed["status"] == "fail"
    assert printed["assertions"]["unsupported_resume_is_typed_and_side_effect_free"] is True
    assert printed["observation"]["provider_spawn_count"] == 0
    assert json.loads((evidence / "cleanup-receipt.json").read_text())["orphan_count"] == 0
    assert json.loads((evidence / "result.json").read_text()) == printed


def test_cursor_coordination_failure_retains_launch_and_cleanup_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = cursor_coordination_producer
    provider_bin = _executable(tmp_path / "cursor-agent")
    evidence = tmp_path / "evidence"
    variant = next(key for key, value in module._DISPATCH.items() if value[0] == "directed_input")
    args = argparse.Namespace(
        variant=variant,
        evidence_root=evidence,
        repo_root=tmp_path / "repo",
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=provider_bin,
        live_timeout_secs=1.0,
        api_url="https://runtime.example",
        agents_token="device-token",
    )
    shipper = _FakeShipper()
    machine = module._CursorMachine(tmp_path / "machine-home", {"HOME": str(tmp_path / "machine-home")}, shipper)  # type: ignore[arg-type]
    sessions = iter((_FakeCursorSession("source", tmp_path / "source"), _FakeCursorSession("target", tmp_path / "target")))

    monkeypatch.setattr(module, "_start_cursor_machine", lambda *_args, **_kwargs: machine)
    monkeypatch.setattr(module, "_launch_cursor_session", lambda *_args, **_kwargs: next(sessions))
    monkeypatch.setattr(module, "_wait_marker_reply", lambda _api, _token, _session, marker, **_kwargs: marker)
    monkeypatch.setattr(module, "_wait_first_turn_settled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "wait_session_tail", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "_mint_coordination_token", lambda *_args, **_kwargs: "scoped-token")
    monkeypatch.setattr(
        module,
        "_create_directed_input",
        lambda *_args, **_kwargs: {"id": 7, "input_receipt": {"receipt_id": "r-7"}},
    )
    monkeypatch.setattr(
        module,
        "wait_assistant_response_after_marker",
        lambda *_args, **_kwargs: ([], {"marker_observed_in_assistant": False}),
    )
    monkeypatch.setattr(module, "_find_inbound_directed_input", lambda *_args, **_kwargs: None)
    stop_calls: list[dict[str, Any]] = []
    cleanup_calls: list[dict[str, Any]] = []
    stale_socket = tmp_path / "stale-control.sock"
    stale_socket.write_text("stale", encoding="utf-8")

    def fake_stop_session(_spec: Any, _args: Any, state: dict[str, Any], process: Any, **kwargs: Any) -> dict[str, Any]:
        stop_calls.append({"session_id": state["session_id"], "pid": process.process.pid, "force": kwargs["force"]})
        return {"status": "pass", "method": "controlled-native-stop"}

    def fake_cleanup_processes(_spec: Any, processes: tuple[Any, ...], states: list[dict[str, Any]]) -> dict[str, Any]:
        cleanup_calls.append(
            {"session_ids": [state["session_id"] for state in states], "pids": [process.process.pid for process in processes]}
        )
        return {
            "verification": {"verified": False},
            "verified": False,
            "orphan_count": 0,
            "control_endpoints": [{"kind": "unix_socket", "endpoint": str(stale_socket), "absent": False}],
        }

    monkeypatch.setattr(module, "stop_session", fake_stop_session)
    monkeypatch.setattr(module, "cleanup_processes", fake_cleanup_processes)

    result = module.run_coordination(args)

    assert result["status"] == "fail"
    assert result["assertions"]["provider_input_receipt_linked"] is True
    assert result["assertions"]["attributed_input_visible"] is False
    launches = json.loads((evidence / "session-launch-receipts.json").read_text())
    assert set(launches["sessions"]) == {"source", "target"}
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert cleanup["required_cleanup"] == {"no_orphan_provider_processes": True, "final_socket_absent": True}
    assert stop_calls == [
        {"session_id": "target", "pid": 4242, "force": False},
        {"session_id": "source", "pid": 4242, "force": False},
    ]
    assert cleanup_calls == [
        {"session_ids": ["target"], "pids": [4242]},
        {"session_ids": ["source"], "pids": [4242]},
    ]
    assert not stale_socket.exists()
    assert json.loads((evidence / "result.json").read_text()) == result


@pytest.mark.parametrize(
    ("producer_module", "provider"),
    ((cursor_native_resume, "cursor"), (opencode_native_resume, "opencode")),
)
def test_native_resume_entrypoints_retain_initial_evidence_when_late_boundary_fails(
    producer_module: Any,
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _executable(tmp_path / "engine")
    cli = _executable(tmp_path / "longhouse")
    provider_bin = _executable(tmp_path / f"{provider}-provider")
    repo = tmp_path / "repo"
    repo.mkdir()
    evidence = tmp_path / "evidence"
    home = tmp_path / "provider-home"
    home.mkdir()

    monkeypatch.setenv(live_session_toolkit.RUNTIME_API_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(live_session_toolkit.RUNTIME_AGENTS_TOKEN_ENV, "device-token")
    monkeypatch.setattr(live_session_toolkit, "require_disposable_runtime", lambda _url: None)
    monkeypatch.setattr(
        provider_native_resume,
        "subprocess",
        SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(stdout="provider 1.0\n", stderr="", returncode=0)),
    )
    monkeypatch.setattr(provider_native_resume, "sha256_file", lambda _path: "sha256:test")
    monkeypatch.setattr(live_session_toolkit, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(live_session_toolkit, "initialize_cursor_workspace", lambda _path: None)
    monkeypatch.setattr(live_session_toolkit, "launch_command", lambda *_args, **_kwargs: ["fake-provider"])
    monkeypatch.setattr(live_session_toolkit, "PtyProcess", _FakePty)
    monkeypatch.setattr(live_session_toolkit, "start_transcript_shipper", lambda *_args, **_kwargs: _FakeShipper())
    monkeypatch.setattr(
        live_session_toolkit,
        "wait_state",
        lambda *_args, **_kwargs: {
            "session_id": "session-1",
            "run_id": "run-1",
            "connection_id": "connection-1",
            "provider_session_id": "provider-thread-1",
        },
    )
    monkeypatch.setattr(live_session_toolkit, "wait_session_tail", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "assistant_event_digests", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(live_session_toolkit, "provider_process_pid", lambda *_args, **_kwargs: 4242)
    monkeypatch.setattr(
        live_session_toolkit,
        "wait_assistant_response_after_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controlled native response failure")),
    )
    monkeypatch.setattr(live_session_toolkit, "cleanup_processes", lambda *_args, **_kwargs: {"verified": True, "orphan_count": 0})
    monkeypatch.setattr(live_session_toolkit, "secret_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "qualification_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_session_toolkit, "bound_terminal_recordings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_native_resume, "_close_recordings", lambda *_args: None)
    monkeypatch.setattr(
        provider_native_resume,
        "_cursor_shutdown_barrier",
        lambda *_args, **_kwargs: {"status": "pass", "clean": True, "dead": True, "provider_process_dead": True},
    )
    monkeypatch.setattr(
        opencode_qualification_profile, "prepare_opencode_qualification_profile", lambda *_args, **_kwargs: {"profile": "fake"}
    )
    monkeypatch.setattr(provider_native_resume, "_send_initial_seed", lambda *_args, **_kwargs: {"returncode": 0})
    monkeypatch.setattr(provider_native_resume, "_cursor_initial_send_then_flush", lambda *_args, **_kwargs: ({}, {"status": "pass"}))
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_initial_idle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(provider_native_resume, "_wait_cursor_bootstrap_hook_sequence", lambda *_args, **_kwargs: {"observed": True})
    monkeypatch.setattr(provider_native_resume, "_cursor_hook_event_bytes", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(live_session_toolkit, "wait_cursor_tui_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_session_toolkit, "wait_opencode_tui_ready", lambda *_args, **_kwargs: None)

    exit_code = producer_module.main_for(
        provider,
        [
            "--variant",
            "clean_exit",
            "--evidence-root",
            str(evidence),
            "--repo-root",
            str(repo),
            "--engine",
            str(engine),
            "--longhouse-cli",
            str(cli),
            "--provider-bin",
            str(provider_bin),
            "--live-send-timeout-secs",
            "1",
        ],
    )

    printed = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert printed["status"] == "fail"
    assert printed["failure_code"] == "direct_native_resume_failed"
    assert "controlled native response failure" in printed["error"]
    assert (evidence / "provider-binary-receipt.json").exists()
    assert (evidence / "initial-seed-send.json").exists()
    assert (evidence / "initial-transcript-ship-receipt.json").exists()
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["verified"] is True
    assert cleanup["orphan_count"] == 0
    assert json.loads((evidence / "result.json").read_text())["status"] == "fail"


def test_cursor_turn_boundary_failure_keeps_failure_report_and_stop_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = cursor_turn_boundary_producer
    provider_bin = _executable(tmp_path / "cursor-agent")
    evidence = tmp_path / "evidence"
    home = tmp_path / "home"
    home.mkdir()
    shipper = _FakeShipper()
    args = argparse.Namespace(
        variant="cell:cursor:activity_returns_to_quiescent_at_turn_boundary:cursor_turn_boundary_quiescent",
        evidence_root=evidence,
        repo_root=tmp_path / "repo",
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=provider_bin,
        model=None,
        timeout_secs=1.0,
        max_archive_lag_secs=1.0,
        api_url="https://runtime.example",
        agents_token="device-token",
    )

    monkeypatch.setattr(module, "sha256_file", lambda _path: "sha256:test")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="cursor 1.0\n"))
    monkeypatch.setattr(module, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(module, "start_transcript_shipper", lambda *_args, **_kwargs: shipper)

    def fail_product_e2e(_e2e_args: argparse.Namespace) -> dict[str, Any]:
        raise RuntimeError("controlled quiescence boundary failure")

    monkeypatch.setattr(module.cursor_helm_product_e2e, "run_product_e2e", fail_product_e2e)
    monkeypatch.setattr(module, "secret_scan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "qualification_secrets", lambda *_args, **_kwargs: [])

    result = module.run_turn_boundary(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "direct_turn_boundary_failed"
    assert "controlled quiescence boundary failure" in result["error"]
    report = json.loads((evidence / "product-e2e-report.json").read_text())
    assert report["status"] == "failed"
    assert json.loads((evidence / "transcript-shipper-receipt.json").read_text())["status"] == "pass"
    assert shipper.stop_calls >= 2
    assert json.loads((evidence / "result.json").read_text()) == result


def test_ios_workspace_source_failure_keeps_typed_result_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = ios_workspace_selection_source_producer
    evidence = tmp_path / "evidence"

    def fail_source_oracle(*, evidence_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
        raise RuntimeError("controlled source boundary failure")

    monkeypatch.setattr(module, "run_ios_workspace_selection_source_oracle", fail_source_oracle)
    result = module.run(evidence, repo_root=tmp_path / "repo")

    assert result["status"] == "fail"
    assert result["failure_code"] == "ios_workspace_selection_source_contract_failed"
    assert "controlled source boundary failure" in result["error"]
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert cleanup["orphan_count"] == 0
    assert result["assertions"]["ios_fresh_ranking_replaces_implicit_cache"] is False
    assert json.loads((evidence / "result.json").read_text()) == result


def test_opencode_server_contract_failure_serializes_all_assertions_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = opencode_server_contract_producer
    provider_bin = _executable(tmp_path / "opencode")
    evidence = tmp_path / "evidence"
    variant = execution_variant_key(
        provider="opencode",
        assertion_id="serve_session_contract_preserved",
        scenario_id=module.REGISTRATION.scenario_id,
        variant=None,
    )
    args = argparse.Namespace(evidence_root=evidence, variant=variant, provider_bin=provider_bin)

    def fake_execute(_binary: Path, _canary_root: Path):
        assertions = tuple(
            provider_semantic_qualification.SemanticAssertion(
                assertion_id,
                AssertionOutcome.SEMANTIC_FAIL if assertion_id == "serve_session_contract_preserved" else AssertionOutcome.PASS,
                EvidenceClass.LIVE_NO_TOKEN,
            )
            for assertion_id, _variant in module.REGISTRATION.assertion_cells
        )
        canaries = {
            name: {"status": "pass"}
            for name in (
                "binary_identity",
                "attach_command_shape",
                "server_startup",
                "schema_probe",
                "session_create",
                "session_get",
                "prompt_async_no_reply_delivery",
                "session_abort",
                "process_restart_reattach_contract",
            )
        }
        canaries["server_startup"] = {"status": "fail"}
        return {"status": "fail", "provider_live_canary": {"canaries": canaries}}, assertions, ()

    monkeypatch.setattr(module.opencode_server_qualification, "_execute", fake_execute)
    monkeypatch.setattr(module, "sha256_file", lambda _path: "sha256:test")
    monkeypatch.setattr(module, "_no_orphan_opencode_server_processes", lambda _path: True)

    result = module.run_server_contract(args)

    assert result["status"] == "fail"
    assert result["assertions"]["serve_session_contract_preserved"] is False
    assert result["assertions"]["process_restart_reattach_preserved"] is True
    canary = json.loads((evidence / "provider-live-canary-artifact.json").read_text())
    assert canary["provider_live_canary"]["canaries"]["server_startup"]["status"] == "fail"
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["status"] == "pass"
    assert cleanup["required_cleanup"]["no_orphan_provider_processes"] is True
    assert json.loads((evidence / "result.json").read_text()) == result


def test_opencode_turn_boundary_failure_preserves_activity_and_cleanup_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = opencode_turn_boundary_quiescent
    provider_bin = _executable(tmp_path / "opencode")
    evidence = tmp_path / "evidence"
    home = tmp_path / "home"
    home.mkdir()
    shipper = _FakeShipper()
    args = argparse.Namespace(
        variant="cell:opencode:activity_returns_to_quiescent_at_turn_boundary:opencode_turn_boundary_quiescent",
        evidence_root=evidence,
        repo_root=tmp_path / "repo",
        engine=tmp_path / "engine",
        longhouse_cli=tmp_path / "longhouse",
        provider_bin=provider_bin,
        api_url="https://runtime.example",
        agents_token="device-token",
        live_send_timeout_secs=1.0,
    )

    monkeypatch.setattr(module, "sha256_file", lambda _path: "sha256:test")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="opencode 1.0\n"))
    monkeypatch.setattr(module, "isolated_provider_home", lambda: home)
    monkeypatch.setattr(module, "prepare_opencode_qualification_profile", lambda *_args, **_kwargs: {"profile": "fake"})
    monkeypatch.setattr(module, "start_transcript_shipper", lambda *_args, **_kwargs: shipper)
    monkeypatch.setattr(module, "PtyProcess", _FakePty)
    monkeypatch.setattr(module, "launch_command", lambda *_args, **_kwargs: ["fake-opencode"])
    monkeypatch.setattr(module, "wait_state", lambda *_args, **_kwargs: {"session_id": "session-1", "run_id": "run-1"})
    monkeypatch.setattr(module, "wait_opencode_tui_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "wait_session_tail", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "assistant_event_digests", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(module, "_wait_terminal_growth", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        module,
        "wait_assistant_response_after_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("controlled served transcript failure")),
    )
    monkeypatch.setattr(module, "stop_session", lambda *_args, **_kwargs: {"clean": True, "dead": True, "provider_process_dead": True})
    monkeypatch.setattr(module, "_redact_retained_secrets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(module, "qualification_secrets", lambda *_args, **_kwargs: [])

    result = module.run_turn_boundary_quiescent(args)

    assert result["status"] == "fail"
    assert result["failure_code"] == "direct_turn_boundary_quiescent_failed"
    assert "controlled served transcript failure" in result["error"]
    assert (evidence / "turn-activity-receipt.json").exists()
    cleanup = json.loads((evidence / "cleanup-receipt.json").read_text())
    assert cleanup["required_cleanup"] == {
        "managed_opencode_process_exited": True,
        "no_orphan_provider_processes": True,
    }
    assert shipper.stop_calls >= 1
    assert json.loads((evidence / "result.json").read_text()) == result
