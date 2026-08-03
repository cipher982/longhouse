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
from zerg.qa.scenario_equivalence import compare_scenario_results
from tests_lite._provider_harness_test_helpers import install_fake_engine

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "scenario_equivalence"


@pytest.fixture(autouse=True)
def _stable_runner_checkout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(codex_release_identity, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(codex_release_identity, "_git_dirty", lambda _root: False)
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
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "tool-1", "type": "command_execution", "command": "/bin/zsh -lc " + json.dumps(command),
    "aggregated_output": output, "exit_code": 0, "status": "completed"
}}}}))
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "message-1", "type": "agent_message",
    "text": output.rstrip("\\n") if behavior != "semantic_mismatch" else "DIFFERENT"
}}}}))
"""


def _request(tmp_path: Path, *, profile: str, binary: Path, identity: str, build_identity: str, invocation_id: str) -> Path:
    payload = {
        "schema_version": 1,
        "provider": "codex",
        "profile": profile,
        "provider_bin": str(binary),
        "expected_provider_version": "1.2.3",
        "expected_executable_identity": identity,
        "expected_provider_build_identity": build_identity,
        "expected_provider_build_granularity": "full_installed_tree",
        "invocation_id": invocation_id,
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": "test-sha",
    }
    path = tmp_path / f"{invocation_id}-request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _closure_digest(package_root: Path) -> str:
    from zerg.qa.provider_build_store import closure_digest

    return closure_digest(package_root, granularity="full_installed_tree")


def _as_scenario_result(run_result: dict) -> dict:
    """Adapt an emit_proof_bundle()-shaped run() return value (legacy
    release-lane executors and the harness bridge both return this shape,
    see codex_tool_call_result.emit_proof_bundle's docstring) into the
    ScenarioResult.to_json()-shaped envelope compare_scenario_results()
    expects: top-level status, assertion outcomes nested under
    data.assertions."""
    return {"status": run_result["execution_status"], "data": {"assertions": run_result["assertions"]}}


def _write_fixture_pair(name: str, baseline: dict, candidate: dict) -> None:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    (FIXTURE_ROOT / f"{name}.baseline.json").write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    (FIXTURE_ROOT / f"{name}.candidate.json").write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n")


def _load_fixture_pair(name: str) -> tuple[dict, dict]:
    baseline = json.loads((FIXTURE_ROOT / f"{name}.baseline.json").read_text())
    candidate = json.loads((FIXTURE_ROOT / f"{name}.candidate.json").read_text())
    return baseline, candidate


# --- Corpus generation -------------------------------------------------
#
# These two functions capture real output from the legacy release-lane
# executor and the harness-backed bridge (Phase 2's bridge/dispatcher
# design) against identical fake codex binaries -- the same equivalence
# already proven by test_provider_harness_qualification.py's hand-written
# assertions, but stored here as a reusable fixture corpus so
# compare_scenario_results() has real baseline/candidate data to work
# against, not synthetic guesses. Regenerate with `--write-fixtures` (see
# test_generate_corpus below) whenever the underlying profiles change.


def _capture_tool_call_result_pair(tmp_path: Path, monkeypatch) -> tuple[dict, dict]:
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
        invocation_id="corpus-tool-call-result-legacy",
    )
    legacy_result = codex_tool_call_result.run(legacy_request, tmp_path / "legacy-output")

    harness_request = _request(
        tmp_path,
        profile=codex_tool_call_result.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="corpus-tool-call-result-harness",
    )
    harness_result = bridge.run(harness_request, tmp_path / "harness-output")
    return _as_scenario_result(legacy_result), _as_scenario_result(harness_result)


def _capture_helm_interrupt_pair(tmp_path: Path, monkeypatch) -> tuple[dict, dict]:
    package_root, binary, identity = _codex_package(tmp_path, behavior="pass")
    build_identity = f"sha256:{_closure_digest(package_root)}"
    for name in ("CODEX_API_URL", "CODEX_AGENTS_TOKEN", codex_helm_interrupt.ENGINE_ENV, codex_helm_interrupt.PACKAGE_ROOT_ENV, codex_helm_interrupt.PROVIDER_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)

    legacy_request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="corpus-helm-interrupt-legacy",
    )
    legacy_result = codex_helm_interrupt.run(legacy_request, tmp_path / "legacy-helm-output")

    harness_request = _request(
        tmp_path,
        profile=codex_helm_interrupt.PROFILE,
        binary=binary,
        identity=identity,
        build_identity=build_identity,
        invocation_id="corpus-helm-interrupt-harness",
    )
    harness_result = bridge.run(harness_request, tmp_path / "harness-helm-output")
    return _as_scenario_result(legacy_result), _as_scenario_result(harness_result)


@pytest.mark.timeout(30)
def test_generate_corpus(tmp_path: Path, monkeypatch) -> None:
    """Not a real assertion -- (re)captures the fixture corpus from live
    runs against fake binaries and writes it to disk, so the comparison
    tests below run against real data without re-running the executors
    every time. Run explicitly when a profile's output shape changes;
    otherwise the checked-in fixtures are what the other tests use."""
    tool_call_result_pair = _capture_tool_call_result_pair(tmp_path, monkeypatch)
    _write_fixture_pair("codex_tool_call_result_v1", *tool_call_result_pair)
    helm_interrupt_pair = _capture_helm_interrupt_pair(tmp_path, monkeypatch)
    _write_fixture_pair("codex_helm_interrupt_v1", *helm_interrupt_pair)


@pytest.mark.parametrize("name", ["codex_tool_call_result_v1", "codex_helm_interrupt_v1"])
def test_corpus_fixtures_are_equivalent(name: str) -> None:
    baseline, candidate = _load_fixture_pair(name)
    report = compare_scenario_results(baseline, candidate)
    assert report.equivalent, report.mismatches


def test_status_mismatch_is_caught() -> None:
    baseline = {"status": "completed", "data": {"assertions": {"a": "pass"}}}
    candidate = {"status": "infrastructure_error", "data": {"assertions": {"a": "pass"}}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any(m.field == "status" for m in report.mismatches)


def test_assertion_outcome_mismatch_is_caught() -> None:
    baseline = {"status": "completed", "data": {"assertions": {"a": "pass", "b": "pass"}}}
    candidate = {"status": "completed", "data": {"assertions": {"a": "pass", "b": "semantic_fail"}}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any(m.field == "data.assertions.b" for m in report.mismatches)


def test_dropped_assertion_id_is_caught() -> None:
    baseline = {"status": "completed", "data": {"assertions": {"a": "pass", "b": "pass"}}}
    candidate = {"status": "completed", "data": {"assertions": {"a": "pass"}}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any("assertion identities" in m.field for m in report.mismatches)


def test_command_mismatch_is_caught() -> None:
    baseline = {"status": "pass", "data": {"command": "echo hi"}}
    candidate = {"status": "pass", "data": {"command": "echo bye"}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any(m.field == "data.command" for m in report.mismatches)


def test_capability_boolean_mismatch_is_caught() -> None:
    baseline = {"status": "pass", "data": {"operation_evidence": {"interrupt": {"status": "pass"}}}}
    candidate = {"status": "pass", "data": {"operation_evidence": {"interrupt": {"status": "fail"}}}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any(m.field == "data.operation_evidence.interrupt.status" for m in report.mismatches)


def test_missing_identity_digest_on_candidate_is_caught() -> None:
    baseline = {"status": "pass", "data": {"executable_identity": "sha256:" + "a" * 64}}
    candidate = {"status": "pass", "data": {}}
    report = compare_scenario_results(baseline, candidate)
    assert not report.equivalent
    assert any(m.field == "data.executable_identity" for m in report.mismatches)


def test_different_identity_digest_values_are_not_flagged() -> None:
    # Two separate runs legitimately produce different hashes for the same
    # binary content only if the binary itself differs; this checks that
    # *equal-but-different* digest values (same field, different value) are
    # not flagged -- only presence/absence is, per the module's docstring.
    baseline = {"status": "pass", "data": {"executable_identity": "sha256:" + "a" * 64}}
    candidate = {"status": "pass", "data": {"executable_identity": "sha256:" + "b" * 64}}
    report = compare_scenario_results(baseline, candidate)
    assert report.equivalent
