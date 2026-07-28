from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result
from zerg.qa import provider_harness_qualification as bridge


@pytest.fixture(autouse=True)
def _stable_runner_checkout(monkeypatch) -> None:
    monkeypatch.setattr(codex_release_identity, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(codex_release_identity, "_git_dirty", lambda _root: False)


def _codex_package(tmp_path: Path, *, behavior: str = "pass") -> tuple[Path, Path, str]:
    root = tmp_path / "codex-package"
    for name in sorted(codex_helm_interrupt.PACKAGE_MEMBERS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "bin/codex":
            path.write_text(_fake_codex_script(behavior=behavior), encoding="utf-8")
        else:
            path.write_text(name, encoding="utf-8")
        if name in codex_helm_interrupt._EXECUTABLE_PACKAGE_MEMBERS:  # noqa: SLF001
            path.chmod(0o700)
    binary = root / "bin/codex"
    identity = f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"
    return root, binary, identity


def _fake_codex_script(*, behavior: str) -> str:
    return f"""#!{sys.executable}
import json
import os
import shlex
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli 1.2.3")
    raise SystemExit(0)
prompt = sys.argv[-1]
command = prompt.split("exactly this one command: ", 1)[1].split("\\nThen", 1)[0]
behavior = {behavior!r}
output = "0123456789abcdef0123456789abcdef\\n"
reported_command = "/bin/zsh -lc " + shlex.quote(command)
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "tool-1", "type": "command_execution", "command": reported_command,
    "aggregated_output": output, "exit_code": 0, "status": "completed"
}}}}))
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "message-1", "type": "agent_message",
    "text": output.rstrip("\\n") if behavior != "semantic_mismatch" else "DIFFERENT"
}}}}))
"""


def _request(
    tmp_path: Path,
    *,
    profile: str,
    binary: Path,
    identity: str,
    build_identity: str,
    **changes: object,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "provider": "codex",
        "profile": profile,
        "provider_bin": str(binary),
        "expected_provider_version": "1.2.3",
        "expected_executable_identity": identity,
        "expected_provider_build_identity": build_identity,
        "expected_provider_build_granularity": "full_installed_tree",
        "invocation_id": "harness-bridge-run-1",
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": "test-sha",
    }
    payload.update(changes)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _closure_digest(package_root: Path) -> str:
    from zerg.qa.provider_build_store import closure_digest

    return closure_digest(package_root, granularity="full_installed_tree")


def test_tool_call_result_legacy_and_harness_paths_agree_on_the_same_binary(tmp_path: Path, monkeypatch) -> None:
    """Equivalence check for the bridge/dispatcher design's item 3
    (docs/specs/provider-factory-coherence.md): the legacy release-lane
    executor (codex_tool_call_result.run(), which launches its own inline
    subprocess) and the harness-backed bridge (which launches the shared
    run_codex_real_tool_command() inside a harness scenario) are two
    genuinely different observation-producing code paths judged by the same
    pure oracle. Run both against the identical fake codex binary/package,
    with CODEX_MANAGED_PACKAGE_ROOT set exactly as control-plane's
    run_factory() sets it in production for this profile
    (control-plane/provider_factory/core.py:655-660), and assert they reach
    the same assertion outcomes.
    """
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    monkeypatch.setenv(codex_tool_call_result.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    monkeypatch.setenv(codex_tool_call_result.MANAGED_PACKAGE_ROOT_ENV, str(package_root))

    legacy_request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="equivalence-legacy",
    )
    legacy_result = codex_tool_call_result.run(legacy_request, tmp_path / "legacy-output")

    harness_request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="equivalence-harness",
    )
    harness_result = bridge.run(harness_request, tmp_path / "harness-output")

    assert legacy_result["assertions"] == harness_result["assertions"] == {
        "exact_executable_identity_observed": "pass",
        "reported_version_matches_expected": "pass",
        "command_execution_completed_with_exact_output": "pass",
        "tool_result_linked_to_final_agent_message": "pass",
    }
    assert legacy_result["execution_status"] == harness_result["execution_status"] == "completed"
    legacy_bundle = json.loads((tmp_path / "legacy-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    harness_bundle = json.loads((tmp_path / "harness-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    assert legacy_bundle["coverage_manifest"]["evidence_class"] == harness_bundle["coverage_manifest"]["evidence_class"] == "live_token"


def test_tool_call_result_end_to_end_pass(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )
    monkeypatch.setenv(codex_tool_call_result.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    output_root = tmp_path / "output"

    result = bridge.run(request, output_root)

    assert result["valid"] is True
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert bundle["artifact_kind"] == "provider_capability_proof_bundle"
    assert bundle["coverage_manifest"]["profile"] == codex_tool_call_result.PROFILE
    assert bundle["coverage_manifest"]["outcomes"] == {
        "exact_executable_identity_observed": "pass",
        "reported_version_matches_expected": "pass",
        "command_execution_completed_with_exact_output": "pass",
        "tool_result_linked_to_final_agent_message": "pass",
    }
    assert bundle["coverage_manifest"]["evidence_class"] == "live_token"
    for record in bundle["records"]:
        assert record["provider_build_identity"] == build_identity
        assert record["provider_build_granularity"] == "full_installed_tree"


def test_tool_call_result_semantic_mismatch_is_not_infrastructure_error(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _codex_package(tmp_path, behavior="semantic_mismatch")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )
    monkeypatch.setenv(codex_tool_call_result.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["command_execution_completed_with_exact_output"] == "pass"
    assert outcomes["tool_result_linked_to_final_agent_message"] == "semantic_fail"
    assert outcomes["exact_executable_identity_observed"] == "pass"


def test_tool_call_result_blocked_without_api_key(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )
    monkeypatch.delenv(codex_tool_call_result.API_KEY_ENV, raising=False)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["command_execution_completed_with_exact_output"] == "blocked"
    assert outcomes["tool_result_linked_to_final_agent_message"] == "blocked"
    assert bundle["coverage_manifest"]["evidence_class"] == "live_no_token"


def test_rejects_single_asset_granularity(tmp_path: Path, monkeypatch) -> None:
    # Both bridged profiles always stage the managed Codex package
    # (control-plane/provider_factory/core.py:623-643) -- single_asset would
    # mean the derivation this bridge relies on (provider_bin.parent.parent
    # is the package root) is simply wrong, so this must fail closed rather
    # than silently materialize an incorrect ProviderBuildRef.
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        expected_provider_build_granularity="single_asset",
    )
    monkeypatch.setenv(codex_tool_call_result.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert set(bundle["coverage_manifest"]["outcomes"].values()) == {"blocked"}
    assert bundle["execution_metadata"]["reason"] == "provider_build_ref_invalid"


def test_rejects_mismatched_build_identity(tmp_path: Path, monkeypatch) -> None:
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity="sha256:" + "0" * 64,
    )
    monkeypatch.setenv(codex_tool_call_result.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert set(bundle["coverage_manifest"]["outcomes"].values()) == {"blocked"}


def test_unsupported_profile_is_rejected(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "codex",
                "profile": "codex_release_identity_v1",
                "provider_bin": "/bin/true",
                "expected_provider_version": "1.2.3",
                "expected_executable_identity": "sha256:" + "0" * 64,
                "expected_provider_build_identity": "sha256:" + "0" * 64,
                "expected_provider_build_granularity": "single_asset",
                "invocation_id": "x",
                "producer_class": "local_diagnostic",
                "producer_version": "test",
                "longhouse_git_sha": "test-sha",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(bridge.RequestError, match="unsupported provider/profile"):
        bridge.run(request, tmp_path / "output")


def test_helm_interrupt_uses_probe_and_interrupt_scenarios(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import universal_agent_harness as uah

    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )

    captured_options: list[uah.HarnessOptions] = []

    def fake_run_harness(options: uah.HarnessOptions):
        captured_options.append(options)
        return {
            "results": [
                {
                    "provider": "codex",
                    "scenario": "probe_identity",
                    "status": "pass",
                    "data": {"version": "codex-cli 1.2.3"},
                },
                {
                    "provider": "codex",
                    "scenario": "interrupt_cancel",
                    "status": "pass",
                    "data": {
                        "strict_oracle": {
                            "active_managed_turn_observed": "pass",
                            "interrupt_terminal_cancelled_or_interrupted": "pass",
                            "managed_bridge_cleanup_completed": "pass",
                        },
                        "engine_identity": "sha256:" + "a" * 64,
                    },
                },
            ]
        }

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    assert captured_options[0].scenarios == ("probe_identity", "interrupt_cancel")
    assert captured_options[0].providers == ("codex",)
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert bundle["coverage_manifest"]["outcomes"] == {
        "active_managed_turn_observed": "pass",
        "interrupt_terminal_cancelled_or_interrupted": "pass",
        "managed_bridge_cleanup_completed": "pass",
    }
    assert bundle["coverage_manifest"]["evidence_class"] == "live_token"
    for record in bundle["records"]:
        assert record["longhouse_build_id"] == "sha256:" + "a" * 64


def test_helm_interrupt_blocked_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from zerg.qa import universal_agent_harness as uah

    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )

    def fake_run_harness(options: uah.HarnessOptions):
        return {
            "results": [
                {"provider": "codex", "scenario": "probe_identity", "status": "pass", "data": {"version": "codex-cli 1.2.3"}},
                {
                    "provider": "codex",
                    "scenario": "interrupt_cancel",
                    "status": "blocked",
                    "failure_code": "codex_helm_strict_environment_missing",
                    "data": {"missing": ["CODEX_API_KEY"]},
                },
            ]
        }

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert set(bundle["coverage_manifest"]["outcomes"].values()) == {"blocked"}
    assert bundle["coverage_manifest"]["evidence_class"] == "live_no_token"


def test_helm_interrupt_hermetic_dispatch_fallback_is_blocked_not_infrastructure_error(tmp_path: Path, monkeypatch) -> None:
    """Regression test for a real bug found while writing the bridge/dispatcher
    design's equivalence tests (docs/specs/provider-factory-coherence.md).
    When bridge credentials (CODEX_API_URL/CODEX_AGENTS_TOKEN) are missing,
    interrupt_cancel's Stage 1 falls back to
    _run_codex_interrupt_dispatch_proof (universal_agent_harness.py:4237) --
    a hermetic-only dispatch proof that legitimately reports status="pass"
    (the hermetic check itself succeeded) with no strict_oracle key at all
    (the live/strict check was never attempted). _strict_outcomes() must
    still classify this as BLOCKED, matching the legacy release-lane path's
    own credentials-missing handling -- not INFRASTRUCTURE_ERROR, which
    would misrepresent "the live check never ran" as "something crashed."
    """
    from zerg.qa import universal_agent_harness as uah

    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
    )

    def fake_run_harness(options: uah.HarnessOptions):
        return {
            "results": [
                {"provider": "codex", "scenario": "probe_identity", "status": "pass", "data": {"version": "codex-cli 1.2.3"}},
                {
                    "provider": "codex",
                    "scenario": "interrupt_cancel",
                    "status": "pass",
                    "data": {
                        "missing_live_credentials": ["--api-url", "--agents-token"],
                        "operation_evidence": {
                            "interrupt": {"status": "pass", "level": "hermetic"},
                            "live_interrupt_canary": {"status": "blocked", "level": "live_token_required"},
                        },
                    },
                },
            ]
        }

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert set(bundle["coverage_manifest"]["outcomes"].values()) == {"blocked"}
    assert bundle["coverage_manifest"]["evidence_class"] == "live_no_token"
