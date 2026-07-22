"""Strict live-token Codex tool-call/result qualification profile."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from zerg.qa import codex_release_identity as identity_bridge
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore

PROFILE = "codex_tool_call_result_v1"
SCENARIO_ID = "codex_tool_call_result"
SCENARIO_REVISION = 1
TIMEOUT_SECONDS = 180
API_KEY_ENV = "CODEX_API_KEY"
MANAGED_PACKAGE_ROOT_ENV = "CODEX_MANAGED_PACKAGE_ROOT"
ASSERTIONS = (
    "exact_executable_identity_observed",
    "reported_version_matches_expected",
    "command_execution_completed_with_exact_output",
    "tool_result_linked_to_final_agent_message",
)


def _load_request(path: Path) -> dict[str, Any]:
    request = identity_bridge._load_request_for_profile(path, PROFILE)  # noqa: SLF001
    return request


def _redact(value: str, secret: str, managed_package_root: str | None = None) -> str:
    if secret:
        value = value.replace(secret, "[CODEX_API_KEY]")
    if managed_package_root:
        value = value.replace(managed_package_root, "[CODEX_MANAGED_PACKAGE_ROOT]")
    return identity_bridge._redact_text(value)  # noqa: SLF001


def _managed_package_resources() -> tuple[str, Path, str] | None:
    raw = os.environ.get(MANAGED_PACKAGE_ROOT_ENV)
    if raw is None:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise identity_bridge.RequestError(f"{MANAGED_PACKAGE_ROOT_ENV} must be an absolute directory")
    helper = path / "codex-resources" / "bwrap"
    if helper.is_symlink() or not helper.is_file() or not os.access(helper, os.X_OK):
        raise identity_bridge.RequestError(f"{MANAGED_PACKAGE_ROOT_ENV} must contain executable official codex-resources/bwrap")
    try:
        helper.resolve(strict=True).relative_to(path.resolve(strict=True))
        helper_identity = identity_bridge._sha256_file(helper)  # noqa: SLF001
    except (OSError, ValueError) as exc:
        raise identity_bridge.RequestError("official Codex sandbox helper cannot be resolved inside package root") from exc
    return raw, helper, helper_identity


def _jsonl_events(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    invalid_lines: list[str] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            invalid_lines.append(text[:200])
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            invalid_lines.append(text[:200])
    return events, invalid_lines


def _event_item(event: dict[str, Any]) -> dict[str, Any]:
    item = event.get("item")
    return item if isinstance(item, dict) else {}


def _record(
    *,
    request: dict[str, Any],
    executable_identity: str,
    contract_digest: str,
    adapter_digest: str,
    oracle_digest: str,
    generated_at: str,
    raw_digest: str,
    outcome: AssertionOutcome,
    provider_version: str,
    assertion_id: str,
    evidence_class: EvidenceClass,
) -> ProviderCapabilityProofRecord:
    return ProviderCapabilityProofRecord(
        provider="codex",
        provider_version=provider_version,
        provider_executable_identity=executable_identity,
        provider_contract_digest=contract_digest,
        adapter_digest=adapter_digest,
        scenario_id=SCENARIO_ID,
        scenario_revision=SCENARIO_REVISION,
        oracle_digest=oracle_digest,
        assertion_id=assertion_id,
        outcome=outcome,
        evidence_class=evidence_class,
        generated_at=generated_at,
        producer_class=request["producer_class"],
        producer_version=request["producer_version"],
        invocation_id=request["invocation_id"],
        mode=None,
        platform=platform.system(),
        architecture=platform.machine(),
        raw_reference_digests=(raw_digest,),
        longhouse_git_sha=request["longhouse_git_sha"],
    )


def _emit(
    *,
    request: dict[str, Any],
    output_root: Path,
    executable_identity: str,
    runner_sha: str,
    generated_at: str,
    provider_version: str,
    outcomes: dict[str, AssertionOutcome],
    execution: dict[str, Any],
    observation: dict[str, Any],
    evidence_class: EvidenceClass,
) -> dict[str, Any]:
    contract = contract_for_provider("codex")
    if contract is None:
        raise identity_bridge.RequestError("Codex managed-provider contract is missing")
    raw_bytes = (json.dumps(observation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    raw_digest = identity_bridge._sha256(raw_bytes)  # noqa: SLF001
    execution = {
        **execution,
        "invocation_id": request["invocation_id"],
        "platform": platform.system(),
        "architecture": platform.machine(),
        "raw_evidence_digest": raw_digest,
        "runner_git_sha": runner_sha,
    }
    identity_bridge._atomic_json(output_root / "request.json", request)  # noqa: SLF001
    identity_bridge._atomic_json(output_root / "raw-evidence.json", observation)  # noqa: SLF001
    identity_bridge._atomic_json(output_root / "execution-summary.json", execution)  # noqa: SLF001
    oracle_digest = identity_bridge._sha256(Path(__file__).read_bytes())  # noqa: SLF001
    store = ProviderCapabilityProofStore(output_root / "proof-store")
    records: list[ProviderCapabilityProofRecord] = []
    for assertion_id in ASSERTIONS:
        record = _record(
            request=request,
            executable_identity=executable_identity,
            contract_digest=contract.contract_entry_digest,
            adapter_digest=contract.adapter_digest,
            oracle_digest=oracle_digest,
            generated_at=generated_at,
            raw_digest=raw_digest,
            outcome=outcomes[assertion_id],
            provider_version=provider_version,
            assertion_id=assertion_id,
            evidence_class=evidence_class,
        )
        store.write(record)
        records.append(record)
    serialized_outcomes = {key: value.value for key, value in outcomes.items()}
    coverage = {
        "profile": PROFILE,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": SCENARIO_REVISION,
        "evidence_class": evidence_class.value,
        "assertions": list(ASSERTIONS),
        "outcomes": serialized_outcomes,
        "complete": set(outcomes) == set(ASSERTIONS),
    }
    identity_bridge._atomic_json(output_root / "coverage-manifest.json", coverage)  # noqa: SLF001
    bundle = {
        "artifact_kind": "provider_capability_proof_bundle",
        "schema_version": 2,
        "records": [record.serialize() for record in records],
        "execution_metadata": execution,
        "coverage_manifest": coverage,
    }
    identity_bridge._atomic_json(output_root / "proof-bundle.json", bundle)  # noqa: SLF001
    return {
        "valid": True,
        "output_root": str(output_root),
        "proof_bundle": str(output_root / "proof-bundle.json"),
        "assertions": serialized_outcomes,
        "execution_status": execution["status"],
    }


def _run_process_group(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = _load_request(request_path)
    output_root = output_root.expanduser().resolve()
    managed_package_resources = _managed_package_resources()
    managed_package_root = managed_package_resources[0] if managed_package_resources else None
    repo_root = Path(__file__).resolve().parents[3]
    binary, actual_identity, runner_sha = identity_bridge._preflight(request, output_root, repo_root)  # noqa: SLF001
    generated_at = identity_bridge._now()  # noqa: SLF001
    pre_execution_identity = identity_bridge._sha256_file(binary)  # noqa: SLF001
    if pre_execution_identity != actual_identity:
        raise identity_bridge.RequestError("provider executable changed before execution")

    api_key = os.environ.get(API_KEY_ENV, "")
    base_observation: dict[str, Any] = {
        "provider_bin": str(binary),
        "expected_executable_identity": request["expected_executable_identity"],
        "pre_execution_identity": pre_execution_identity,
        "post_execution_identity": pre_execution_identity,
        "expected_provider_version": request["expected_provider_version"],
        "reported_version": None,
        "version_probe": None,
        "tool_run": None,
    }
    if not api_key:
        outcomes = {assertion: AssertionOutcome.BLOCKED for assertion in ASSERTIONS}
        try:
            blocked_post_identity = identity_bridge._sha256_file(binary)  # noqa: SLF001
        except OSError:
            blocked_post_identity = None
        base_observation["post_execution_identity"] = blocked_post_identity
        outcomes["exact_executable_identity_observed"] = (
            AssertionOutcome.PASS if blocked_post_identity == pre_execution_identity else AssertionOutcome.INFRASTRUCTURE_ERROR
        )
        return _emit(
            request=request,
            output_root=output_root,
            executable_identity=actual_identity,
            runner_sha=runner_sha,
            generated_at=generated_at,
            provider_version="unreported",
            outcomes=outcomes,
            execution={"status": "blocked", "reason": "codex_api_key_missing", "processes_started": 0},
            observation={**base_observation, "blocked_reason": "codex_api_key_missing"},
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
        )

    version_result: subprocess.CompletedProcess[str] | None = None
    version_stdout = ""
    version_stderr = ""
    version_error: str | None = None
    version_timed_out = False
    version_env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        version_result = subprocess.run(
            [str(binary), "--version"],
            text=True,
            capture_output=True,
            env=version_env,
            timeout=identity_bridge.TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        version_timed_out = True
        version_error = "timeout"
        version_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        version_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        version_error = f"{type(exc).__name__}: {exc}"
    else:
        version_stdout = version_result.stdout
        version_stderr = version_result.stderr

    version_match = identity_bridge._VERSION_LINE.fullmatch(version_stdout.strip()) if version_result else None  # noqa: SLF001
    reported_version = version_match.group("version") if version_match else None
    version_probe = {
        "argv": [str(binary), "--version"],
        "returncode": version_result.returncode if version_result else None,
        "timed_out": version_timed_out,
        "error": version_error,
        "stdout": _redact(version_stdout, api_key),
        "stderr": _redact(version_stderr, api_key),
    }
    outcomes = {assertion: AssertionOutcome.BLOCKED for assertion in ASSERTIONS}
    outcomes["exact_executable_identity_observed"] = AssertionOutcome.PASS
    sandbox_helper_evidence: dict[str, Any] | None = None
    version_infrastructure_error = version_result is None or version_result.returncode != 0
    if version_infrastructure_error:
        outcomes["reported_version_matches_expected"] = AssertionOutcome.INFRASTRUCTURE_ERROR
        outcomes["command_execution_completed_with_exact_output"] = AssertionOutcome.INFRASTRUCTURE_ERROR
        outcomes["tool_result_linked_to_final_agent_message"] = AssertionOutcome.INFRASTRUCTURE_ERROR
        execution_status = "timed_out" if version_timed_out else "infrastructure_error"
        tool_observation = None
    elif reported_version != request["expected_provider_version"]:
        outcomes["reported_version_matches_expected"] = AssertionOutcome.SEMANTIC_FAIL
        execution_status = "completed"
        tool_observation = {"status": "not_run", "reason": "provider_version_mismatch"}
    else:
        outcomes["reported_version_matches_expected"] = AssertionOutcome.PASS
        command = f"{shlex.quote(sys.executable)} -c 'import secrets; print(secrets.token_hex(16))'"
        prompt = (
            "Use the shell tool exactly once to run exactly this one command: "
            f"{command}\nThen reply with only the command output, copied exactly."
        )
        with tempfile.TemporaryDirectory(prefix="longhouse-codex-qualification-") as raw_runtime:
            runtime_root = Path(raw_runtime)
            workspace = runtime_root / "workspace"
            codex_home = runtime_root / "codex-home"
            workspace.mkdir()
            codex_home.mkdir()
            helper_link: Path | None = None
            if managed_package_resources is not None:
                _, vendored_bwrap, vendored_bwrap_identity = managed_package_resources
                helper_bin = runtime_root / "helper-bin"
                helper_bin.mkdir()
                helper_link = helper_bin / "codex-linux-sandbox"
                helper_link.symlink_to(binary)
                sandbox_helper_evidence = {
                    "shim_target_path": str(binary),
                    "shim_target_identity": actual_identity,
                    "vendored_bwrap_path": str(vendored_bwrap),
                    "vendored_bwrap_identity": vendored_bwrap_identity,
                    "shim_path": str(helper_link),
                }
            argv = [
                str(binary),
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "-c",
                'approval_policy="never"',
                "--color",
                "never",
                "-C",
                str(workspace),
                prompt,
            ]
            tool_env = {
                **version_env,
                API_KEY_ENV: api_key,
                "HOME": str(runtime_root),
                "CODEX_HOME": str(codex_home),
            }
            if managed_package_root is not None:
                tool_env[MANAGED_PACKAGE_ROOT_ENV] = managed_package_root
                tool_env["PATH"] = f"{helper_link.parent}{os.pathsep}{tool_env['PATH']}"
            tool_result: subprocess.CompletedProcess[str] | None = None
            tool_stdout = ""
            tool_stderr = ""
            tool_error: str | None = None
            tool_timed_out = False
            try:
                tool_result = _run_process_group(
                    argv,
                    cwd=workspace,
                    env=tool_env,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                tool_timed_out = True
                tool_error = "timeout"
                tool_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                tool_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            except OSError as exc:
                tool_error = f"{type(exc).__name__}: {exc}"
            else:
                tool_stdout = tool_result.stdout
                tool_stderr = tool_result.stderr
        if sandbox_helper_evidence is not None:
            _, vendored_bwrap, vendored_bwrap_identity = managed_package_resources
            try:
                vendored_bwrap_post_identity = identity_bridge._sha256_file(vendored_bwrap)  # noqa: SLF001
            except OSError:
                vendored_bwrap_post_identity = None
            sandbox_helper_evidence["vendored_bwrap_post_identity"] = vendored_bwrap_post_identity
            sandbox_helper_evidence["vendored_bwrap_stable"] = vendored_bwrap_post_identity == vendored_bwrap_identity
            sandbox_helper_evidence["shim_removed"] = not Path(sandbox_helper_evidence["shim_path"]).exists()
        events, invalid_lines = _jsonl_events(tool_stdout)
        indexed_items = [(index, _event_item(event)) for index, event in enumerate(events)]
        command_items = [(index, item) for index, item in indexed_items if item.get("type") == "command_execution"]
        matching_commands = [
            (index, item)
            for index, item in command_items
            if item.get("status") == "completed"
            and item.get("exit_code") == 0
            and command in str(item.get("command") or "")
            and re.fullmatch(r"[0-9a-f]{32}\n", str(item.get("aggregated_output") or "")) is not None
        ]
        agent_messages = [(index, item) for index, item in indexed_items if item.get("type") == "agent_message"]
        final_message = agent_messages[-1] if agent_messages else None
        command_passed = len(command_items) == 1 and len(matching_commands) == 1
        raw_command_output = str(matching_commands[0][1].get("aggregated_output") or "") if command_passed else None
        observed_output = raw_command_output.rstrip("\n") if raw_command_output is not None else None
        linked = bool(
            command_passed
            and observed_output
            and final_message
            and final_message[0] > matching_commands[0][0]
            and str(final_message[1].get("text") or "") == observed_output
            and observed_output not in prompt
        )
        tool_infrastructure_error = tool_result is None or tool_result.returncode != 0
        if tool_infrastructure_error:
            outcomes["command_execution_completed_with_exact_output"] = AssertionOutcome.INFRASTRUCTURE_ERROR
            outcomes["tool_result_linked_to_final_agent_message"] = AssertionOutcome.INFRASTRUCTURE_ERROR
            execution_status = "timed_out" if tool_timed_out else "infrastructure_error"
        else:
            outcomes["command_execution_completed_with_exact_output"] = (
                AssertionOutcome.PASS if command_passed else AssertionOutcome.SEMANTIC_FAIL
            )
            outcomes["tool_result_linked_to_final_agent_message"] = AssertionOutcome.PASS if linked else AssertionOutcome.SEMANTIC_FAIL
            execution_status = "completed"
        if sandbox_helper_evidence is not None and (
            not sandbox_helper_evidence["vendored_bwrap_stable"] or not sandbox_helper_evidence["shim_removed"]
        ):
            outcomes["command_execution_completed_with_exact_output"] = AssertionOutcome.INFRASTRUCTURE_ERROR
            outcomes["tool_result_linked_to_final_agent_message"] = AssertionOutcome.INFRASTRUCTURE_ERROR
            execution_status = "infrastructure_error"
        tool_observation = {
            "argv": argv,
            "returncode": tool_result.returncode if tool_result else None,
            "timed_out": tool_timed_out,
            "error": tool_error,
            "stdout": _redact(tool_stdout, api_key, managed_package_root),
            "stderr": _redact(tool_stderr, api_key, managed_package_root),
            "invalid_jsonl_lines": [_redact(line, api_key, managed_package_root) for line in invalid_lines],
            "event_count": len(events),
            "command_event_count": len(command_items),
            "matching_command_count": len(matching_commands),
            "raw_command_output": raw_command_output,
            "observed_output": observed_output,
            "final_agent_message": (
                _redact(str(final_message[1].get("text") or ""), api_key, managed_package_root) if final_message else None
            ),
        }

    try:
        post_execution_identity = identity_bridge._sha256_file(binary)  # noqa: SLF001
    except OSError:
        post_execution_identity = None
    if post_execution_identity != pre_execution_identity:
        outcomes = {assertion: AssertionOutcome.INFRASTRUCTURE_ERROR for assertion in ASSERTIONS}
        execution_status = "infrastructure_error"
    if sandbox_helper_evidence is not None:
        sandbox_helper_evidence["shim_target_post_identity"] = post_execution_identity
        sandbox_helper_evidence["shim_target_stable"] = post_execution_identity == actual_identity
    observation = {
        **base_observation,
        "post_execution_identity": post_execution_identity,
        "reported_version": reported_version,
        "version_probe": version_probe,
        "tool_run": tool_observation,
    }
    semantic_process_attempted = bool(tool_observation is not None and tool_observation.get("status") != "not_run")
    return _emit(
        request=request,
        output_root=output_root,
        executable_identity=actual_identity,
        runner_sha=runner_sha,
        generated_at=generated_at,
        provider_version=reported_version or "unreported",
        outcomes=outcomes,
        execution={
            "status": execution_status,
            "processes_started": 1 + int(tool_observation is not None and tool_observation.get("status") != "not_run"),
            "sandbox_helper": sandbox_helper_evidence,
        },
        observation=observation,
        evidence_class=(EvidenceClass.LIVE_TOKEN if semantic_process_attempted else EvidenceClass.LIVE_NO_TOKEN),
    )
