from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests_lite._provider_harness_test_helpers import install_fake_engine
from zerg.qa import antigravity_hook_qualification
from zerg.qa import claude_real_print_qualification
from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result
from zerg.qa import cursor_release_identity
from zerg.qa import opencode_server_qualification
from zerg.qa import provider_harness_qualification as bridge
from zerg.qa import provider_interaction_semantics as interaction_semantics
from zerg.qa import provider_release_identity
from zerg.qa.provider_factory_model import DEFAULT_HARNESS_SCENARIOS
from zerg.qa.provider_factory_model import LIVE_TOKEN_HARNESS_SCENARIO


@pytest.fixture(autouse=True)
def _stable_runner_checkout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_release_identity, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(codex_release_identity, "_git_dirty", lambda _root: False)
    monkeypatch.setattr(provider_release_identity, "git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(provider_release_identity, "git_dirty", lambda _root: False)
    engine = install_fake_engine(tmp_path / "longhouse-engine")
    monkeypatch.setenv("LONGHOUSE_ENGINE_BIN", str(engine))


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
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli 1.2.3")
    raise SystemExit(0)
if sys.argv[1:] == ["login", "--with-api-key"]:
    if not sys.stdin.read().strip():
        raise SystemExit(4)
    auth_path = Path(os.environ["CODEX_HOME"]) / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("{{}}", encoding="utf-8")
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
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 12, "output_tokens": 3}}}}))
"""


def _claude_single_asset(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "claude-closure"
    root.mkdir()
    binary = root / "provider"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("2.1.220 (Claude Code)")\n'
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    identity = f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"
    return root, binary, identity


def _opencode_single_asset(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "opencode-closure"
    root.mkdir()
    binary = root / "provider"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("1.17.20")\n'
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    identity = f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"
    return root, binary, identity


def _antigravity_single_asset(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "antigravity-closure"
    root.mkdir()
    binary = root / "provider"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'if sys.argv[1:] == ["--version"]:\n'
        '    print("1.1.5")\n'
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    identity = f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}"
    return root, binary, identity


def _request(
    tmp_path: Path,
    *,
    profile: str,
    binary: Path,
    identity: str,
    build_identity: str,
    provider: str = "codex",
    version: str = "1.2.3",
    granularity: str = "full_installed_tree",
    **changes: object,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "provider": provider,
        "profile": profile,
        "provider_bin": str(binary),
        "expected_provider_version": version,
        "expected_executable_identity": identity,
        "expected_provider_build_identity": build_identity,
        "expected_provider_build_granularity": granularity,
        "invocation_id": "harness-bridge-run-1",
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": "test-sha",
    }
    payload.update(changes)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _closure_digest(package_root: Path, *, granularity: str = "full_installed_tree") -> str:
    from zerg.qa.provider_build_store import closure_digest

    return closure_digest(package_root, granularity=granularity)


def _passing_full_column_payload() -> dict:
    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_CODEX_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row = {"provider": "codex", "scenario": scenario, "status": status}
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)
    return {
        "results": results,
        "provider_execution_coverage_matrix": {
            "provider_coverage_gap_kind_counts": {"codex": {"passed": 32, "provider_contract_unsupported": 1}},
            "missing_provider_actions": [],
        },
    }


def _passing_claude_full_column_payload() -> dict:
    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_CLAUDE_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row = {"provider": "claude", "scenario": scenario, "status": status}
        if scenario == "probe_identity":
            row["data"] = {"version": "2.1.220 (Claude Code)"}
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)
    results.append(
        {
            "provider": "claude",
            "scenario": LIVE_TOKEN_HARNESS_SCENARIO,
            "status": "pass",
        }
    )
    return {
        "results": results,
        "provider_execution_coverage_matrix_path": "/evidence/provider-execution-coverage-matrix.json",
        "provider_execution_coverage_matrix": {
            "provider_coverage_gap_kind_counts": {"claude": {"passed": 32, "no_token_safety_gate": 1}},
            "missing_provider_actions": [],
        },
    }


def _live_claude_interaction_data(
    tmp_path: Path,
    *,
    digest: str,
    evidence_class: str = "live_no_token",
) -> dict[str, object]:
    observation = interaction_semantics.generated_fake_observation("claude")
    effort_probe = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
    observation["probes"] = [effort_probe]
    observation["raw_events"] = [*effort_probe["raw_events"], *observation["raw_events"][-2:]]
    observation.update(
        {
            "evidence_class": evidence_class,
            "synthetic": False,
            "provider_version": "2.1.220",
            "provider_executable_identity": "sha256:" + "a" * 64,
            "qualification_request_digest": digest,
            "native_source_root": str(tmp_path),
        }
    )
    for index, row in enumerate(observation["probes"]):
        events = row.get("raw_events") or []
        if not events:
            continue
        source_path = tmp_path / f"claude-probe-{index}.jsonl"
        lines = [json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for event in events]
        source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        source_bytes = source_path.read_bytes()
        source_rows = []
        offset = 0
        for line, event in zip(lines, events, strict=True):
            source_rows.append(
                {
                    "source_path": str(source_path),
                    "source_offset": offset,
                    "line": line,
                    "line_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                    "event_sha256": interaction_semantics.raw_event_digest(event),
                    "source_binding": "file_bytes_at_offset",
                    "source_file_bytes": len(source_bytes),
                    "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
                }
            )
            offset += len(line.encode("utf-8")) + 1
        row["status"] = "observed"
        row["capture_complete"] = True
        row["post_interaction_quiescent"] = True
        row["native_source_rows"] = source_rows
        row["capture_receipt"] = {
            "stable_snapshots": 3,
            "stable_seconds": 1.5,
            "raw_event_count": len(source_rows),
            "window_sha256": hashlib.sha256("".join(source["event_sha256"] for source in source_rows).encode("ascii")).hexdigest(),
        }

    observation_path = tmp_path / "provider-interaction-observation.json"
    events_path = tmp_path / "provider-interaction-raw.jsonl"
    observation_path.write_text(json.dumps(observation, sort_keys=True), encoding="utf-8")
    events_path.write_text(interaction_semantics.jsonl_events(observation), encoding="utf-8")
    evaluation = interaction_semantics.evaluate_observation("claude", observation)
    assert evaluation["status"] == "pass"
    return {
        **evaluation,
        "verification_scope": "provider_native",
        "evidence_class": evidence_class,
        "raw_observation_path": str(observation_path),
        "raw_events_path": str(events_path),
        "qualification_request_digest": digest,
    }


def _passing_opencode_full_column_payload(evidence_root: Path) -> dict:
    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_OPENCODE_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row: dict[str, object] = {
            "provider": "opencode",
            "scenario": scenario,
            "status": status,
        }
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)

    live_path = evidence_root / "opencode" / "managed_session_e2e" / "raw" / "provider-live-canary.json"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(
        json.dumps(
            {
                "canaries": {
                    name: {"status": "pass"}
                    for name in {
                        *opencode_server_qualification._SERVE_REQUIRED_CANARIES,  # noqa: SLF001
                        *opencode_server_qualification._RESTART_REQUIRED_CANARIES,  # noqa: SLF001
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    managed = next(row for row in results if row["scenario"] == "managed_session_e2e")
    managed["data"] = {"provider_live_artifact_path": str(live_path)}
    results.extend(
        [
            {"provider": "opencode", "scenario": "tool_call_result", "status": "pass"},
            {
                "provider": "opencode",
                "scenario": LIVE_TOKEN_HARNESS_SCENARIO,
                "status": "pass",
            },
        ]
    )
    return {
        "results": results,
        "provider_execution_coverage_matrix_path": "/evidence/provider-execution-coverage-matrix.json",
        "provider_execution_coverage_matrix": {
            "provider_coverage_gap_kind_counts": {
                "opencode": {
                    "passed": 30,
                    "no_token_safety_gate": 1,
                    "not_applicable": 1,
                    "provider_contract_unsupported": 1,
                }
            },
            "missing_provider_actions": [],
        },
    }


def _passing_antigravity_full_column_payload(evidence_root: Path) -> dict:
    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_ANTIGRAVITY_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row: dict[str, object] = {
            "provider": "antigravity",
            "scenario": scenario,
            "status": status,
        }
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)

    live_path = evidence_root / "antigravity" / "launch_managed_session" / "raw" / "provider-live-canary.json"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(
        json.dumps(
            {
                "canaries": {
                    name: {"status": "pass"}
                    for name in antigravity_hook_qualification._NO_TOKEN_REQUIRED_CANARIES  # noqa: SLF001
                }
            }
        ),
        encoding="utf-8",
    )
    launch = next(row for row in results if row["scenario"] == "launch_managed_session")
    launch["data"] = {"provider_live_artifact_path": str(live_path)}
    return {
        "results": results,
        "provider_execution_coverage_matrix_path": "/evidence/provider-execution-coverage-matrix.json",
        "provider_execution_coverage_matrix": {
            "provider_coverage_gap_kind_counts": {
                "antigravity": {
                    "passed": 26,
                    "no_token_safety_gate": 1,
                    "not_applicable": 1,
                    "provider_contract_unsupported": 5,
                }
            },
            "missing_provider_actions": [],
        },
    }


def test_release_bridge_preserves_native_source_artifacts_for_each_model_backed_lane() -> None:
    source_artifacts = [
        {
            "path": "evidence/provider.jsonl",
            "sha256": "a" * 64,
            "kind": "provider_jsonl_stream",
            "event_type": "result",
            "event_sha256": "b" * 64,
        }
    ]
    for provider in ("claude", "codex", "opencode", "cursor"):
        evidence = {
            "source_canary": f"{provider}_model_probe",
            "model": "fixture-model",
            "result_event": {"type": "result"},
            "source_artifacts": source_artifacts,
        }
        observation: dict[str, object] = {}

        bridge._copy_live_model_evidence(  # noqa: SLF001
            observation,
            {"data": {"live_model_evidence": evidence}},
        )

        assert observation["live_model_evidence"] == evidence
        assert observation["live_model_evidence"] is not evidence


def test_full_column_gate_accepts_only_the_complete_known_codex_surface() -> None:
    gate = bridge._full_column_gate(_passing_full_column_payload())  # noqa: SLF001

    assert gate["status"] == "pass"
    assert gate["provider_status"] == "not_applicable"
    assert gate["expected_scenario_count"] == 32
    assert gate["captured_scenario_count"] == 32
    assert gate["unexpected_results"] == []


def test_full_column_gate_exposes_the_interaction_request_binding() -> None:
    payload = _passing_full_column_payload()
    digest = "sha256:" + "a" * 64
    interaction = next(result for result in payload["results"] if result["scenario"] == "interaction_semantics")
    interaction["data"] = {"qualification_request_digest": digest}

    gate = bridge._full_column_gate(  # noqa: SLF001
        payload,
        qualification_request_digest=digest,
    )

    assert gate["status"] == "pass"
    assert gate["qualification_request_digest"] == digest
    assert gate["qualification_request_binding"] == "pass"


@pytest.mark.parametrize(
    ("status", "failure_code", "evidence_class"),
    (
        ("pass", None, "live_no_token"),
        ("pass", None, "live_token"),
        ("blocked", "interaction_live_probe_setup_failed", "live_no_token"),
    ),
)
def test_full_column_gate_accepts_the_result_of_an_explicit_live_interaction_attempt(
    status: str,
    failure_code: str | None,
    evidence_class: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = _passing_claude_full_column_payload()
    digest = "sha256:" + "a" * 64
    interaction = next(result for result in payload["results"] if result["scenario"] == "interaction_semantics")
    interaction["status"] = status
    if failure_code is not None:
        interaction["failure_code"] = failure_code
    else:
        interaction.pop("failure_code", None)
        contract = interaction_semantics.contract_for_provider("claude")
        assert contract is not None
        reduced_contract = replace(contract, interaction_probes=(contract.interaction_probes[0],))
        monkeypatch.setattr(
            interaction_semantics,
            "contract_for_provider",
            lambda _provider: reduced_contract,
        )
        interaction["data"] = _live_claude_interaction_data(
            tmp_path,
            digest=digest,
            evidence_class=evidence_class,
        )
    interaction["qualification_request_digest"] = digest

    gate = bridge._full_column_gate(  # noqa: SLF001
        payload,
        provider="claude",
        qualification_request_digest=digest,
        interaction_evidence_class=evidence_class,
    )

    assert gate["status"] == "pass"
    assert gate["provider_status"] == ("pass" if status == "pass" else "blocked")


def test_full_column_gate_rejects_live_pass_without_materialized_raw_provenance(tmp_path: Path) -> None:
    payload = _passing_full_column_payload()
    interaction = next(result for result in payload["results"] if result["scenario"] == "interaction_semantics")
    interaction["status"] = "pass"
    interaction.pop("failure_code", None)
    interaction["data"] = {
        "verification_scope": "provider_native",
        "provider_status": "pass",
        "evidence_class": "live_no_token",
        "raw_observation_path": str(tmp_path / "missing-observation.json"),
        "raw_events_path": str(tmp_path / "missing-events.jsonl"),
        "assertions": [
            {
                "probe_id": "provider_control",
                "status": "pass",
                "evidence_basis": {"raw_provenance": "pass"},
            }
        ],
    }

    gate = bridge._full_column_gate(  # noqa: SLF001
        payload,
        interaction_evidence_class="live_no_token",
    )

    assert gate["status"] == "fail"
    assert gate["provider_status"] == "fail"
    assert gate["unexpected_results"] == [
        {
            "scenario": "interaction_semantics",
            "expected_status": "blocked",
            "expected_failure_code": "interaction_live_policy_missing",
            "actual_status": "pass",
            "actual_failure_code": "interaction_live_provenance_missing",
        }
    ]


def test_full_column_gate_rejects_a_pass_claim_with_native_evidence_missing() -> None:
    payload = _passing_full_column_payload()
    interaction = next(result for result in payload["results"] if result["scenario"] == "interaction_semantics")
    interaction["status"] = "pass"
    interaction["failure_code"] = "interaction_native_raw_evidence_missing"
    interaction["data"] = {
        "verification_scope": "provider_native",
        "provider_status": "pass",
        "evidence_class": "live_token",
        "assertions": [{"probe_id": "provider_control", "status": "blocked"}],
    }

    gate = bridge._full_column_gate(payload, interaction_evidence_class="live_token")  # noqa: SLF001

    assert gate["status"] == "fail"
    assert gate["provider_status"] == "fail"
    assert gate["unexpected_results"][0]["actual_failure_code"] == "interaction_native_raw_evidence_missing"


def test_full_column_gate_rejects_one_regressed_scenario() -> None:
    payload = _passing_full_column_payload()
    row = next(result for result in payload["results"] if result["scenario"] == "timeline_projection")
    row["status"] = "fail"
    row["failure_code"] = "projection_regressed"
    payload["provider_execution_coverage_matrix"]["provider_coverage_gap_kind_counts"]["codex"] = {"passed": 31, "unexpected_failure": 1}

    gate = bridge._full_column_gate(payload)  # noqa: SLF001

    assert gate["status"] == "fail"
    assert gate["failure_code"] == "codex_full_column_regression"
    assert gate["unexpected_results"] == [
        {
            "scenario": "timeline_projection",
            "expected_status": "pass",
            "expected_failure_code": None,
            "actual_status": "fail",
            "actual_failure_code": "projection_regressed",
        }
    ]
    assert gate["unexpected_coverage_gap_kinds"] == {"unexpected_failure": 1}


def test_claude_full_column_gate_accepts_explicit_no_token_limits() -> None:
    gate = bridge._full_column_gate(  # noqa: SLF001
        _passing_claude_full_column_payload(),
        provider="claude",
    )

    assert gate["status"] == "pass"
    assert gate["provider"] == "claude"
    assert gate["expected_scenario_count"] == 32
    assert gate["coverage_gap_kind_counts"] == {
        "passed": 32,
        "no_token_safety_gate": 1,
    }
    assert gate["expected_limits"]["full_action_suite"] == {
        "status": "blocked",
        "failure_code": "full_action_suite_has_explicit_gaps",
    }


def test_opencode_full_column_gate_accepts_measured_contract_limits(tmp_path: Path) -> None:
    gate = bridge._full_column_gate(  # noqa: SLF001
        _passing_opencode_full_column_payload(tmp_path),
        provider="opencode",
    )

    assert gate["status"] == "pass"
    assert gate["provider"] == "opencode"
    assert gate["expected_scenario_count"] == 32
    assert gate["coverage_gap_kind_counts"] == {
        "passed": 30,
        "no_token_safety_gate": 1,
        "not_applicable": 1,
        "provider_contract_unsupported": 1,
    }


def test_antigravity_full_column_gate_accepts_maintenance_tier_limits(tmp_path: Path) -> None:
    gate = bridge._full_column_gate(  # noqa: SLF001
        _passing_antigravity_full_column_payload(tmp_path),
        provider="antigravity",
    )

    assert gate["status"] == "pass"
    assert gate["provider"] == "antigravity"
    assert gate["expected_scenario_count"] == 32
    assert gate["coverage_gap_kind_counts"] == {
        "passed": 26,
        "no_token_safety_gate": 1,
        "not_applicable": 1,
        "provider_contract_unsupported": 5,
    }


def test_cursor_full_column_gate_accepts_live_gate0_limits() -> None:
    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_CURSOR_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row = {"provider": "cursor", "scenario": scenario, "status": status}
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)
    payload = {
        "results": results,
        "provider_execution_coverage_matrix": {
            "provider_coverage_gap_kind_counts": {
                "cursor": {
                    "passed": 29,
                    "no_token_safety_gate": 1,
                    "not_applicable": 2,
                    "provider_contract_unsupported": 1,
                }
            },
            "missing_provider_actions": [],
        },
    }

    gate = bridge._full_column_gate(payload, provider="cursor")  # noqa: SLF001

    assert gate["status"] == "pass"


def test_cursor_observed_install_executor_demotes_blocked_live_provider_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "cursor-install"
    root.mkdir()
    binary = root / "cursor-agent"
    binary.write_text("cursor fixture\n", encoding="utf-8")
    binary.chmod(0o700)
    build_identity = f"sha256:{_closure_digest(root)}"
    request = _request(
        tmp_path,
        profile=cursor_release_identity.OBSERVED_INSTALL_PROFILE,
        binary=binary,
        identity=f"sha256:{hashlib.sha256(binary.read_bytes()).hexdigest()}",
        build_identity=build_identity,
        provider="cursor",
        version="2026.07.23-test",
        granularity="full_installed_tree",
        scenario_evidence={"interaction_semantics": "live_token"},
    )
    evidence_root = tmp_path / "output" / "evidence"
    gate0 = tmp_path / "gate0.json"
    gate0.write_text(json.dumps({"status": "passed", "provider": "cursor"}), encoding="utf-8")
    monkeypatch.setenv("LONGHOUSE_CURSOR_GATE0_ARTIFACT", str(gate0))

    results = []
    for scenario in DEFAULT_HARNESS_SCENARIOS:
        status, failure_code = bridge._EXPECTED_CURSOR_FULL_COLUMN_LIMITS.get(  # noqa: SLF001
            scenario, ("pass", None)
        )
        row: dict[str, object] = {"provider": "cursor", "scenario": scenario, "status": status}
        if failure_code is not None:
            row["failure_code"] = failure_code
        results.append(row)
    interaction = next(row for row in results if row["scenario"] == "interaction_semantics")
    interaction.update(status="blocked", failure_code="interaction_native_raw_evidence_missing")
    results.append(
        {
            "provider": "cursor",
            "scenario": LIVE_TOKEN_HARNESS_SCENARIO,
            "status": "pass",
        }
    )
    monkeypatch.setattr(
        bridge,
        "run_harness",
        lambda _options: {
            "results": results,
            "provider_execution_coverage_matrix": {
                "provider_coverage_gap_kind_counts": {"cursor": {"passed": 29}},
                "missing_provider_actions": [],
            },
        },
    )

    observation, assertions, _secrets = bridge._cursor_observed_install_executor(  # noqa: SLF001
        binary,
        evidence_root,
        request=json.loads(request.read_text(encoding="utf-8")),
    )

    assert observation["status"] == "blocked"
    assert observation["full_column_gate"]["provider_status"] == "blocked"
    assert assertions[0].outcome.value == "blocked"


def test_claude_profile_runs_one_full_column_and_reuses_live_print_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, binary, identity = _claude_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=claude_real_print_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="claude",
        version="2.1.220",
        granularity="single_asset",
    )
    captured_options = []

    def fake_run_harness(options):
        captured_options.append(options)
        return _passing_claude_full_column_payload()

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.providers == ("claude",)
    assert options.scenarios == (
        *DEFAULT_HARNESS_SCENARIOS,
        LIVE_TOKEN_HARNESS_SCENARIO,
    )
    assert options.provider_bins == {"claude": binary}
    assert options.provider_builds["claude"].closure_granularity == "single_asset"
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["claude_cli_channel_contract_preserved"] == "pass"
    assert outcomes["real_print_marker_returned"] == "pass"
    assert bundle["execution_metadata"]["semantic_status"] == "pass"


def test_claude_full_column_regression_fails_both_profile_assertions_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, binary, identity = _claude_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=claude_real_print_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="claude",
        version="2.1.220",
        granularity="single_asset",
    )
    payload = _passing_claude_full_column_payload()
    row = next(item for item in payload["results"] if item["scenario"] == "timeline_projection")
    row.update(status="fail", failure_code="projection_regressed")
    monkeypatch.setattr(bridge, "run_harness", lambda _options: payload)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["claude_cli_channel_contract_preserved"] == "infrastructure_error"
    assert outcomes["real_print_marker_returned"] == "infrastructure_error"
    assert bundle["execution_metadata"]["semantic_status"] == "infrastructure_error"


def test_opencode_profile_runs_one_full_column_with_server_tool_and_live_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, binary, identity = _opencode_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=opencode_server_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="opencode",
        version="1.17.20",
        granularity="single_asset",
    )
    captured_options = []

    def fake_run_harness(options):
        captured_options.append(options)
        return _passing_opencode_full_column_payload(options.evidence_root)

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.providers == ("opencode",)
    assert options.scenarios == (
        *DEFAULT_HARNESS_SCENARIOS,
        "tool_call_result",
        LIVE_TOKEN_HARNESS_SCENARIO,
    )
    assert options.provider_bins == {"opencode": binary}
    assert options.provider_builds["opencode"].closure_granularity == "single_asset"
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["serve_session_contract_preserved"] == "pass"
    assert outcomes["process_restart_reattach_preserved"] == "pass"
    assert bundle["execution_metadata"]["semantic_status"] == "pass"


@pytest.mark.parametrize("regressed_scenario", ["timeline_projection", "tool_call_result", LIVE_TOKEN_HARNESS_SCENARIO])
def test_opencode_release_gate_regression_fails_profile_assertions_closed(
    tmp_path: Path,
    monkeypatch,
    regressed_scenario: str,
) -> None:
    root, binary, identity = _opencode_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=opencode_server_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="opencode",
        version="1.17.20",
        granularity="single_asset",
    )

    def fake_run_harness(options):
        payload = _passing_opencode_full_column_payload(options.evidence_root)
        row = next(item for item in payload["results"] if item["scenario"] == regressed_scenario)
        row.update(status="fail", failure_code="release_gate_regressed")
        return payload

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["serve_session_contract_preserved"] == "infrastructure_error"
    assert outcomes["process_restart_reattach_preserved"] == "infrastructure_error"
    assert bundle["execution_metadata"]["semantic_status"] == "infrastructure_error"


def test_antigravity_profile_runs_full_column_and_preserves_blocked_live_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, binary, identity = _antigravity_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=antigravity_hook_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="antigravity",
        version="1.1.5",
        granularity="single_asset",
    )
    captured_options = []

    def fake_run_harness(options):
        captured_options.append(options)
        return _passing_antigravity_full_column_payload(options.evidence_root)

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.providers == ("antigravity",)
    assert options.scenarios == DEFAULT_HARNESS_SCENARIOS
    assert options.provider_bins == {"antigravity": binary}
    assert options.provider_builds["antigravity"].closure_granularity == "single_asset"
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["hook_inbox_contract_preserved"] == "pass"
    assert outcomes["real_print_injection_observed"] == "blocked"
    assert bundle["execution_metadata"]["semantic_status"] == "blocked"


def test_antigravity_full_column_regression_fails_profile_assertions_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, binary, identity = _antigravity_single_asset(tmp_path)
    request = _request(
        tmp_path,
        profile=antigravity_hook_qualification.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=f"sha256:{_closure_digest(root, granularity='single_asset')}",
        provider="antigravity",
        version="1.1.5",
        granularity="single_asset",
    )

    def fake_run_harness(options):
        payload = _passing_antigravity_full_column_payload(options.evidence_root)
        row = next(item for item in payload["results"] if item["scenario"] == "timeline_projection")
        row.update(status="fail", failure_code="projection_regressed")
        return payload

    monkeypatch.setattr(bridge, "run_harness", fake_run_harness)

    result = bridge.run(request, tmp_path / "output")

    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    outcomes = bundle["coverage_manifest"]["outcomes"]
    assert outcomes["hook_inbox_contract_preserved"] == "infrastructure_error"
    assert outcomes["real_print_injection_observed"] == "infrastructure_error"
    assert bundle["execution_metadata"]["semantic_status"] == "infrastructure_error"


@pytest.mark.timeout(30)
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

    assert (
        legacy_result["assertions"]
        == harness_result["assertions"]
        == {
            "exact_executable_identity_observed": "pass",
            "reported_version_matches_expected": "pass",
            "command_execution_completed_with_exact_output": "pass",
            "tool_result_linked_to_final_agent_message": "pass",
        }
    )
    assert legacy_result["execution_status"] == harness_result["execution_status"] == "completed"
    legacy_bundle = json.loads((tmp_path / "legacy-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    harness_bundle = json.loads((tmp_path / "harness-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    assert legacy_bundle["coverage_manifest"]["evidence_class"] == harness_bundle["coverage_manifest"]["evidence_class"] == "live_token"
    strict_capture = json.loads(
        (
            tmp_path
            / "harness-output"
            / "harness-evidence"
            / "codex"
            / "codex_tool_call_result_strict"
            / "raw"
            / "codex-tool-call-result-strict.json"
        ).read_text(encoding="utf-8")
    )
    helper = strict_capture["sandbox_helper"]
    assert helper["vendored_bwrap_stable"] is True
    assert helper["shim_removed"] is True
    assert not Path(helper["shim_path"]).exists()


def test_helm_interrupt_legacy_and_harness_paths_agree_when_bridge_credentials_are_missing(tmp_path: Path, monkeypatch) -> None:
    """Equivalence check for codex_helm_interrupt_v1, the harder of the two
    bridged profiles: a real live interrupt needs a managed engine/MCP
    bootstrap that isn't hermetically testable end-to-end. The one case both
    the legacy executor and the harness bridge can reach without any of that
    is bridge credentials (CODEX_API_URL/CODEX_AGENTS_TOKEN) missing --
    legacy's codex_helm_interrupt._required_environment() short-circuits
    immediately to BLOCKED; the harness's interrupt_cancel Stage 1 instead
    runs a real hermetic-only dispatch proof
    (universal_agent_harness._run_codex_interrupt_dispatch_proof, exercised
    for real here, not mocked) and only maps to BLOCKED via the
    _strict_outcomes() fix above. Confirms that fix actually produces
    equivalence, not just the shape the regression test checks in isolation.
    """
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    monkeypatch.delenv("CODEX_API_URL", raising=False)
    monkeypatch.delenv("CODEX_AGENTS_TOKEN", raising=False)
    monkeypatch.delenv(codex_helm_interrupt.ENGINE_ENV, raising=False)
    monkeypatch.delenv(codex_helm_interrupt.PACKAGE_ROOT_ENV, raising=False)
    monkeypatch.delenv(codex_helm_interrupt.PROVIDER_TOKEN_ENV, raising=False)

    legacy_request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="helm-equivalence-legacy",
    )
    legacy_result = codex_helm_interrupt.run(legacy_request, tmp_path / "legacy-output")

    harness_request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="helm-equivalence-harness",
    )
    harness_result = bridge.run(harness_request, tmp_path / "harness-output")

    assert (
        legacy_result["assertions"]
        == harness_result["assertions"]
        == {
            "active_managed_turn_observed": "blocked",
            "interrupt_terminal_cancelled_or_interrupted": "blocked",
            "managed_bridge_cleanup_completed": "blocked",
        }
    )
    legacy_bundle = json.loads((tmp_path / "legacy-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    harness_bundle = json.loads((tmp_path / "harness-output" / "proof-bundle.json").read_text(encoding="utf-8"))
    assert legacy_bundle["coverage_manifest"]["evidence_class"] == harness_bundle["coverage_manifest"]["evidence_class"] == "live_no_token"


@pytest.mark.timeout(30)
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
    semantic_path = output_root / "semantic-evidence" / "semantic-observation.json"
    semantic_observation = json.loads(semantic_path.read_text(encoding="utf-8"))
    source = semantic_observation["live_model_evidence"]["source_artifacts"][0]
    assert not Path(source["path"]).is_absolute()
    assert (output_root.parent / source["path"]).is_file()
    assert bundle["execution_metadata"]["semantic_evidence_digest"] == ("sha256:" + hashlib.sha256(semantic_path.read_bytes()).hexdigest())


@pytest.mark.timeout(30)
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


@pytest.mark.timeout(30)
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
    captured_package_roots: list[str | None] = []
    monkeypatch.setenv(codex_helm_interrupt.PACKAGE_ROOT_ENV, "original-package-root")

    def fake_run_harness(options: uah.HarnessOptions):
        captured_options.append(options)
        captured_package_roots.append(os.environ.get(codex_helm_interrupt.PACKAGE_ROOT_ENV))
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
    assert captured_package_roots == [str(captured_options[0].provider_builds["codex"].build_root)]
    assert os.environ[codex_helm_interrupt.PACKAGE_ROOT_ENV] == "original-package-root"
    bundle = json.loads(Path(result["proof_bundle"]).read_text(encoding="utf-8"))
    assert bundle["coverage_manifest"]["outcomes"] == {
        "active_managed_turn_observed": "pass",
        "interrupt_terminal_cancelled_or_interrupted": "pass",
        "managed_bridge_cleanup_completed": "pass",
    }
    assert bundle["coverage_manifest"]["evidence_class"] == "live_token"
    semantic_path = tmp_path / "output" / "semantic-evidence" / "semantic-observation.json"
    assert semantic_path.is_file()
    semantic_observation = json.loads(semantic_path.read_text(encoding="utf-8"))
    assert "live_model_evidence" not in semantic_observation
    assert semantic_observation["interrupt_cancel"]["status"] == "pass"
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
