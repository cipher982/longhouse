from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zerg.qa import codex_release_identity
from zerg.qa import codex_tool_call_result as profile
from zerg.qa import provider_qualification


@pytest.fixture(autouse=True)
def _stable_runner_checkout(monkeypatch) -> None:
    monkeypatch.setattr(codex_release_identity, "_git_sha", lambda _root: "test-sha")
    monkeypatch.setattr(codex_release_identity, "_git_dirty", lambda _root: False)
    monkeypatch.delenv(profile.MANAGED_PACKAGE_ROOT_ENV, raising=False)


def _fake_codex(tmp_path: Path, *, behavior: str = "pass") -> tuple[Path, str, Path]:
    path = tmp_path / "codex"
    calls = tmp_path / "calls.jsonl"
    descendant_marker = tmp_path / "timeout-descendant-survived"
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

calls = Path({str(calls)!r})
with calls.open("a", encoding="utf-8") as handle:
    sandbox_helper = shutil.which("codex-linux-sandbox")
    handle.write(json.dumps({{
        "argv": sys.argv[1:],
        "has_api_key": bool(os.environ.get("CODEX_API_KEY")),
        "managed_package_root": os.environ.get("CODEX_MANAGED_PACKAGE_ROOT"),
        "sandbox_helper": sandbox_helper,
        "sandbox_helper_source": os.path.realpath(sandbox_helper) if sandbox_helper else None,
        "path": os.environ.get("PATH"),
    }}) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("codex-cli 1.2.3")
    raise SystemExit(0)
prompt = sys.argv[-1]
command = prompt.split("exactly this one command: ", 1)[1].split("\\nThen", 1)[0]
behavior = {behavior!r}
if behavior == "timeout":
    subprocess.Popen([
        sys.executable,
        "-c",
        "import time; from pathlib import Path; time.sleep(0.2); Path({str(descendant_marker)!r}).write_text('alive')",
    ])
    time.sleep(1)
if behavior == "nonzero":
    print(os.environ.get("CODEX_API_KEY", ""), file=sys.stderr)
    raise SystemExit(7)
output = "0123456789abcdef0123456789abcdef\\n"
print(json.dumps({{"type": "item.started", "item": {{
    "id": "tool-1", "type": "command_execution", "command": command,
    "aggregated_output": "", "exit_code": None, "status": "in_progress"
}}}}))
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "tool-1", "type": "command_execution", "command": command,
    "aggregated_output": output, "exit_code": 0, "status": "completed"
}}}}))
if behavior == "extra_command":
    print(json.dumps({{"type": "item.completed", "item": {{
        "id": "tool-2", "type": "command_execution", "command": "pwd",
        "aggregated_output": "/tmp\\n", "exit_code": 0, "status": "completed"
    }}}}))
print(json.dumps({{"type": "item.completed", "item": {{
    "id": "message-1", "type": "agent_message",
    "text": output.rstrip("\\n") if behavior != "semantic_mismatch" else "DIFFERENT"
}}}}))
print(os.environ.get("CODEX_API_KEY", ""), file=sys.stderr)
print(os.environ.get("CODEX_MANAGED_PACKAGE_ROOT", ""), file=sys.stderr)
if behavior == "mutate":
    with Path(__file__).open("a", encoding="utf-8") as handle:
        handle.write("# mutation\\n")
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, f"sha256:{digest}", calls


def _official_package_root(tmp_path: Path) -> tuple[Path, Path]:
    package_root = tmp_path / "official-codex-package"
    helper = package_root / "codex-resources" / "bwrap"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o700)
    return package_root, helper


def _request(tmp_path: Path, binary: Path, identity: str, **changes: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "provider": "codex",
        "profile": profile.PROFILE,
        "provider_bin": str(binary),
        "expected_provider_version": "1.2.3",
        "expected_executable_identity": identity,
        "invocation_id": "tool-run-1",
        "producer_class": "local_diagnostic",
        "producer_version": "test",
        "longhouse_git_sha": "test-sha",
    }
    payload.update(changes)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path: Path, monkeypatch, *, behavior: str = "pass") -> tuple[dict, Path, Path]:
    binary, identity, calls = _fake_codex(tmp_path, behavior=behavior)
    request = _request(tmp_path, binary, identity)
    output = tmp_path / "output"
    monkeypatch.setenv(profile.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    result = provider_qualification.run(request, output)
    return result, output, calls


def test_live_profile_emits_strict_v2_bundle_and_least_privilege_command(tmp_path: Path, monkeypatch) -> None:
    result, output, calls = _run(tmp_path, monkeypatch)

    assert result["execution_status"] == "completed"
    bundle = json.loads((output / "proof-bundle.json").read_text())
    assert bundle["coverage_manifest"] == json.loads((output / "coverage-manifest.json").read_text())
    assert bundle["coverage_manifest"]["scenario_id"] == "codex_tool_call_result"
    assert bundle["coverage_manifest"]["scenario_revision"] == 1
    assert {record["outcome"] for record in bundle["records"]} == {"pass"}
    assert {record["evidence_class"] for record in bundle["records"]} == {"live_token"}
    assert {record["assertion_id"] for record in bundle["records"]} == set(profile.ASSERTIONS)
    raw = (output / "raw-evidence.json").read_bytes()
    assert bundle["execution_metadata"]["raw_evidence_digest"] == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations[0]["argv"] == ["--version"]
    assert invocations[0]["has_api_key"] is False
    assert invocations[0]["managed_package_root"] is None
    assert invocations[1]["has_api_key"] is True
    assert invocations[1]["managed_package_root"] is None
    assert invocations[1]["sandbox_helper"] is None
    live = invocations[1]["argv"]
    assert "--sandbox" in live and live[live.index("--sandbox") + 1] == "workspace-write"
    assert 'approval_policy="never"' in live
    assert "--ephemeral" in live
    assert "--ignore-user-config" in live
    assert "--dangerously-bypass-approvals-and-sandbox" not in live
    raw_evidence = json.loads(raw)
    tool_run = raw_evidence["tool_run"]
    assert tool_run["command_event_count"] == 2
    assert tool_run["command_item_count"] == 1
    assert tool_run["observed_output"] not in tool_run["argv"][-1]
    assert tool_run["final_agent_message"] == tool_run["observed_output"]
    assert {record["mode"] for record in bundle["records"]} == {None}


def test_missing_credential_is_blocked_without_process_execution(tmp_path: Path, monkeypatch) -> None:
    binary, identity, calls = _fake_codex(tmp_path)
    monkeypatch.delenv(profile.API_KEY_ENV, raising=False)
    output = tmp_path / "output"

    result = provider_qualification.run(_request(tmp_path, binary, identity), output)

    assert result["execution_status"] == "blocked"
    assert not calls.exists()
    execution = json.loads((output / "execution-summary.json").read_text())
    assert execution["processes_started"] == 0
    outcomes = result["assertions"]
    assert outcomes["exact_executable_identity_observed"] == "pass"
    assert set(outcomes.values()) == {"pass", "blocked"}
    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    assert {record["evidence_class"] for record in records} == {"live_no_token"}
    coverage = json.loads((output / "coverage-manifest.json").read_text())
    assert coverage["evidence_class"] == "live_no_token"


def test_semantic_mismatch_does_not_change_execution_state(tmp_path: Path, monkeypatch) -> None:
    result, output, _ = _run(tmp_path, monkeypatch, behavior="semantic_mismatch")

    assert result["execution_status"] == "completed"
    assert result["assertions"]["command_execution_completed_with_exact_output"] == "pass"
    assert result["assertions"]["tool_result_linked_to_final_agent_message"] == "semantic_fail"
    assert json.loads((output / "execution-summary.json").read_text())["status"] == "completed"


def test_version_mismatch_never_claims_live_token_evidence(tmp_path: Path, monkeypatch) -> None:
    binary, identity, _ = _fake_codex(tmp_path)
    request = _request(tmp_path, binary, identity, expected_provider_version="9.9.9")
    output = tmp_path / "output"
    monkeypatch.setenv(profile.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")

    result = provider_qualification.run(request, output)

    assert result["execution_status"] == "completed"
    records = json.loads((output / "proof-bundle.json").read_text())["records"]
    assert {record["evidence_class"] for record in records} == {"live_no_token"}


def test_more_than_one_command_is_a_semantic_failure(tmp_path: Path, monkeypatch) -> None:
    result, _, _ = _run(tmp_path, monkeypatch, behavior="extra_command")

    assert result["execution_status"] == "completed"
    assert result["assertions"]["command_execution_completed_with_exact_output"] == "semantic_fail"
    assert result["assertions"]["tool_result_linked_to_final_agent_message"] == "semantic_fail"


@pytest.mark.parametrize("behavior", ["nonzero", "timeout"])
def test_process_failure_is_infrastructure_error(tmp_path: Path, monkeypatch, behavior: str) -> None:
    if behavior == "timeout":
        monkeypatch.setattr(profile, "TIMEOUT_SECONDS", 0.01)
    result, output, _ = _run(tmp_path, monkeypatch, behavior=behavior)

    assert result["execution_status"] in {"infrastructure_error", "timed_out"}
    assert result["assertions"]["command_execution_completed_with_exact_output"] == "infrastructure_error"
    assert result["assertions"]["tool_result_linked_to_final_agent_message"] == "infrastructure_error"
    assert json.loads((output / "execution-summary.json").read_text())["status"] == result["execution_status"]
    if behavior == "timeout":
        time.sleep(0.3)
        assert not (tmp_path / "timeout-descendant-survived").exists()


def test_subject_mutation_invalidates_all_assertions(tmp_path: Path, monkeypatch) -> None:
    result, _, _ = _run(tmp_path, monkeypatch, behavior="mutate")

    assert result["execution_status"] == "infrastructure_error"
    assert set(result["assertions"].values()) == {"infrastructure_error"}


def test_secret_is_never_serialized_even_when_provider_echoes_it(tmp_path: Path, monkeypatch) -> None:
    secret = "seeded-test-api-key-not-a-real-secret"
    _, output, _ = _run(tmp_path, monkeypatch)

    retained = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert secret.encode() not in retained
    assert b"[CODEX_API_KEY]" in retained


def test_managed_package_root_is_validated_live_only_and_redacted(tmp_path: Path, monkeypatch) -> None:
    package_root, helper = _official_package_root(tmp_path)
    monkeypatch.setenv(profile.MANAGED_PACKAGE_ROOT_ENV, str(package_root))

    _, output, calls = _run(tmp_path, monkeypatch)

    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert invocations[0]["managed_package_root"] is None
    assert "helper-bin" not in invocations[0]["path"]
    assert invocations[1]["managed_package_root"] == str(package_root)
    assert invocations[1]["sandbox_helper_source"] == str((tmp_path / "codex").resolve())
    assert invocations[1]["sandbox_helper_source"] != str(helper)
    shim = Path(invocations[1]["sandbox_helper"])
    assert shim.name == "codex-linux-sandbox"
    assert shim.parent.name == "helper-bin"
    assert invocations[1]["path"].split(":", 1)[0] == str(shim.parent)
    assert not shim.exists()
    retained = b"".join(path.read_bytes() for path in output.rglob("*") if path.is_file())
    assert b"[CODEX_MANAGED_PACKAGE_ROOT]" in retained
    execution = json.loads((output / "execution-summary.json").read_text())
    helper_evidence = execution["sandbox_helper"]
    assert helper_evidence["shim_target_path"] == str((tmp_path / "codex").resolve())
    assert helper_evidence["shim_target_identity"].startswith("sha256:")
    assert helper_evidence["shim_target_post_identity"] == helper_evidence["shim_target_identity"]
    assert helper_evidence["shim_target_stable"] is True
    assert helper_evidence["vendored_bwrap_path"] == str(helper)
    assert helper_evidence["vendored_bwrap_identity"].startswith("sha256:")
    assert helper_evidence["vendored_bwrap_post_identity"] == helper_evidence["vendored_bwrap_identity"]
    assert helper_evidence["vendored_bwrap_stable"] is True
    assert helper_evidence["shim_removed"] is True
    assert not Path(helper_evidence["shim_path"]).exists()


@pytest.mark.parametrize("package_root", ["", "relative/package", "/definitely/not/a/codex/package"])
def test_managed_package_root_must_be_an_absolute_directory(tmp_path: Path, monkeypatch, package_root: str) -> None:
    binary, identity, calls = _fake_codex(tmp_path)
    monkeypatch.setenv(profile.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    monkeypatch.setenv(profile.MANAGED_PACKAGE_ROOT_ENV, package_root)
    output = tmp_path / "output"

    with pytest.raises(codex_release_identity.RequestError, match="must be an absolute directory"):
        provider_qualification.run(_request(tmp_path, binary, identity), output)

    assert not calls.exists()
    assert not output.exists()


@pytest.mark.parametrize("resource_shape", ["missing", "not_executable", "system_symlink"])
def test_managed_package_root_rejects_non_official_helper(tmp_path: Path, monkeypatch, resource_shape: str) -> None:
    binary, identity, calls = _fake_codex(tmp_path)
    package_root = tmp_path / "candidate-package"
    package_root.mkdir()
    helper = package_root / "codex-resources" / "bwrap"
    if resource_shape != "missing":
        helper.parent.mkdir()
        if resource_shape == "system_symlink":
            helper.symlink_to("/bin/sh")
        else:
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv(profile.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    monkeypatch.setenv(profile.MANAGED_PACKAGE_ROOT_ENV, str(package_root))
    output = tmp_path / "output"

    with pytest.raises(codex_release_identity.RequestError, match="official codex-resources/bwrap"):
        provider_qualification.run(_request(tmp_path, binary, identity), output)

    assert not calls.exists()
    assert not output.exists()


def test_router_and_profile_requests_are_strict(tmp_path: Path, monkeypatch) -> None:
    binary, identity, _ = _fake_codex(tmp_path)
    monkeypatch.setenv(profile.API_KEY_ENV, "seeded-test-api-key-not-a-real-secret")
    bad_schema = _request(tmp_path, binary, identity, schema_version=2)
    assert provider_qualification.main(["--request", str(bad_schema), "--output-root", str(tmp_path / "one")]) == 2
    unknown = _request(tmp_path, binary, identity, profile="codex_unknown_v1")
    assert provider_qualification.main(["--request", str(unknown), "--output-root", str(tmp_path / "two")]) == 2
    extra = _request(tmp_path, binary, identity, unexpected=True)
    assert provider_qualification.main(["--request", str(extra), "--output-root", str(tmp_path / "three")]) == 2


def test_router_imports_without_optional_server_dependencies() -> None:
    server_root = Path(provider_qualification.__file__).resolve().parents[2]
    command = f"import sys; sys.path.insert(0, {str(server_root)!r}); import zerg.qa.provider_qualification"
    result = subprocess.run([sys.executable, "-S", "-c", command], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
