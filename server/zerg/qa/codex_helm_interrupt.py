"""Strict live-token qualification for the managed Codex Helm interrupt bridge."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any
from typing import Callable

from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa import codex_release_identity as identity_bridge
from zerg.qa import qualification_request
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore

PROFILE = "codex_helm_interrupt_v1"
SCENARIO_ID = "codex_helm_interrupt"
SCENARIO_REVISION = 1
ENGINE_ENV = "LONGHOUSE_ENGINE_BIN"
PACKAGE_ROOT_ENV = "CODEX_MANAGED_PACKAGE_ROOT"
API_URL_ENV = bridge_canary.CODEX_API_URL_ENV
AGENTS_TOKEN_ENV = bridge_canary.CODEX_AGENTS_TOKEN_ENV
PROVIDER_TOKEN_ENV = "CODEX_API_KEY"
ASSERTIONS = (
    "active_managed_turn_observed",
    "interrupt_terminal_cancelled_or_interrupted",
    "managed_bridge_cleanup_completed",
)
PACKAGE_MEMBERS = frozenset(
    {
        "bin/codex",
        "bin/codex-code-mode-host",
        "codex-package.json",
        "codex-path/rg",
        "codex-resources/bwrap",
        "codex-resources/zsh/bin/zsh",
    }
)
_EXECUTABLE_PACKAGE_MEMBERS = PACKAGE_MEMBERS - {"codex-package.json"}
_SEMANTIC_FAILURE_CODES = frozenset(
    {
        "managed_live_interrupt_not_interrupted",
        "managed_live_interrupt_timeout",
    }
)
_BUILD_IDENTITY_KEYS = frozenset({"version", "commit", "commit_short", "dirty", "built_at", "channel"})
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHORT_GIT_SHA = re.compile(r"^[0-9a-f]{7,12}$")
_INERT_MCP_COMMAND = Path("/usr/bin/true")
_INERT_MCP_CONFIG = '[mcp_servers.longhouse]\ncommand = "/usr/bin/true"\nargs = []\n'


def _load_request(path: Path) -> dict[str, Any]:
    return identity_bridge._load_request_for_profile(path, PROFILE)  # noqa: SLF001


def _required_environment() -> tuple[dict[str, str], tuple[str, ...]]:
    values = {
        name: str(os.environ.get(name) or "").strip()
        for name in (ENGINE_ENV, PACKAGE_ROOT_ENV, API_URL_ENV, AGENTS_TOKEN_ENV, PROVIDER_TOKEN_ENV)
    }
    return values, tuple(name for name, value in values.items() if not value)


def _file_identity(path: Path, *, label: str, executable: bool = False) -> str:
    if path.is_symlink() or not path.is_file():
        raise identity_bridge.RequestError(f"{label} must be a regular non-symlink file")
    if executable and not os.access(path, os.X_OK):
        raise identity_bridge.RequestError(f"{label} must be executable")
    try:
        return identity_bridge._sha256_file(path)  # noqa: SLF001
    except OSError as exc:
        raise identity_bridge.RequestError(f"{label} cannot be read: {exc}") from exc


def _package_identity(raw_root: str, provider_bin: Path) -> tuple[Path, str, dict[str, str]]:
    root = Path(raw_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise identity_bridge.RequestError(f"{PACKAGE_ROOT_ENV} must be an absolute non-symlink directory")
    root = root.resolve(strict=True)
    observed = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if observed != PACKAGE_MEMBERS:
        missing = sorted(PACKAGE_MEMBERS - observed)
        unexpected = sorted(observed - PACKAGE_MEMBERS)
        raise identity_bridge.RequestError(f"managed Codex package members mismatch: missing={missing}, unexpected={unexpected}")
    identities: dict[str, str] = {}
    for name in sorted(PACKAGE_MEMBERS):
        member = root / name
        try:
            member.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise identity_bridge.RequestError(f"managed Codex package member escapes package root: {name}") from exc
        identities[name] = _file_identity(
            member,
            label=f"managed Codex package member {name}",
            executable=name in _EXECUTABLE_PACKAGE_MEMBERS,
        )
    package_binary = (root / "bin/codex").resolve(strict=True)
    if package_binary != provider_bin.resolve(strict=True):
        raise identity_bridge.RequestError("provider_bin must be the managed package bin/codex")
    encoded = json.dumps(identities, separators=(",", ":"), sort_keys=True).encode()
    return root, identity_bridge._sha256(encoded), identities  # noqa: SLF001


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for index, secret in enumerate(secrets, start=1):
        if secret:
            value = value.replace(secret, f"[QUALIFICATION_SECRET_{index}]")
    return identity_bridge._redact_text(value)  # noqa: SLF001


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item, secrets) for key, item in value.items()}
    return value


def _scrub_evidence_tree(root: Path, secrets: tuple[str, ...]) -> None:
    replacements = tuple(
        (secret.encode(), f"[QUALIFICATION_SECRET_{index}]".encode()) for index, secret in enumerate(secrets, start=1) if secret
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        redacted = data
        for secret, replacement in replacements:
            redacted = redacted.replace(secret, replacement)
        if redacted != data:
            path.write_bytes(redacted)


def _stop_evidence(canary_root: Path) -> dict[str, Any] | None:
    path = canary_root / "managed-live-interrupt" / "stop.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _managed_bridge_starts_observed(canary_result: dict[str, Any]) -> int:
    summary = canary_result.get("start_summary")
    if not isinstance(summary, dict):
        return 0
    return int(bool(str(summary.get("session_id") or "").strip()))


def _probe_engine_build_identity(engine: Path, request_sha: str, secrets: tuple[str, ...]) -> dict[str, Any]:
    command = [str(engine), "build-identity", "--json"]
    probe_env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=probe_env,
            timeout=identity_bridge.TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "infrastructure_error",
            "reason": type(exc).__name__,
            "identity": None,
            "evidence": {"argv": command, "error": type(exc).__name__},
        }
    evidence = {
        "argv": command,
        "returncode": result.returncode,
        "stdout": _redact_text(result.stdout, secrets),
        "stderr": _redact_text(result.stderr, secrets),
    }
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError:
        identity = None
    well_formed = bool(
        result.returncode == 0
        and isinstance(identity, dict)
        and set(identity) == _BUILD_IDENTITY_KEYS
        and all(
            isinstance(identity.get(key), str) and bool(identity[key].strip())
            for key in ("version", "commit", "commit_short", "built_at", "channel")
        )
        and identity.get("channel") in {"dev", "release"}
        and isinstance(identity.get("dirty"), bool)
        and _FULL_GIT_SHA.fullmatch(str(identity.get("commit") or ""))
        and _SHORT_GIT_SHA.fullmatch(str(identity.get("commit_short") or ""))
        and str(identity.get("commit") or "").startswith(str(identity.get("commit_short") or ""))
    )
    if not well_formed:
        return {
            "status": "blocked",
            "reason": "malformed_engine_build_identity",
            "identity": identity if isinstance(identity, dict) else None,
            "evidence": evidence,
        }
    commit = str(identity["commit"])
    commit_short = str(identity["commit_short"])
    if commit != request_sha or commit_short != request_sha[: len(commit_short)] or identity["dirty"] is not False:
        return {
            "status": "blocked",
            "reason": "engine_build_identity_mismatch",
            "identity": identity,
            "evidence": evidence,
        }
    return {"status": "pass", "reason": None, "identity": identity, "evidence": evidence}


def _prepare_inert_mcp_bootstrap(
    codex_home: Path,
    evidence_root: Path,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    command_identity = _file_identity(
        _INERT_MCP_COMMAND,
        label="qualification inert MCP command",
        executable=True,
    )
    encoded = _INERT_MCP_CONFIG.encode()
    if any(secret and secret.encode() in encoded for secret in secrets):
        raise identity_bridge.RequestError("qualification MCP bootstrap unexpectedly contains a credential")
    config_path = codex_home / "config.toml"
    config_path.write_text(_INERT_MCP_CONFIG, encoding="utf-8")
    retained_root = evidence_root / "qualification-bootstrap"
    retained_root.mkdir()
    retained_config = retained_root / "codex-config.toml"
    retained_config.write_text(_INERT_MCP_CONFIG, encoding="utf-8")
    return {
        "purpose": "codex_transport_shape_bootstrap_only",
        "coordination_mcp_semantics": "not_exercised",
        "ambient_codex_config_used": False,
        "command": str(_INERT_MCP_COMMAND),
        "command_identity": command_identity,
        "config_digest": identity_bridge._sha256(encoded),  # noqa: SLF001
        "retained_config": str(retained_config),
    }


# os.environ.clear()/update() below is process-global. If this function is
# ever called from more than one thread of the same process at once, the two
# calls' environments would race. Guarded defensively per Hatch Sol review,
# even though today's callers (the release lane's run(), and the harness's
# interrupt_cancel driver) are each single-threaded per process.
_ENV_ISOLATION_LOCK = threading.Lock()


def run_isolated_codex_operation(
    provider_bin: Path,
    *,
    engine: Path,
    package_root: str,
    api_url: str,
    agents_token: str,
    provider_token: str,
    output_root: Path,
    operation: Callable[[Path, Path], Any],
) -> tuple[Any, str | None, dict[str, Any] | None, dict[str, Any]]:
    """Run `operation(canary_root, provider_bin)` under full environment
    isolation and inert MCP bootstrap.

    Extracted from run() (Phase 2, "closing the observation gap" —
    docs/specs/provider-factory-coherence.md) so both the release lane and
    the harness's interrupt_cancel driver can produce the same trustworthy
    observation `codex_helm_interrupt_oracle` expects, instead of the harness
    calling into a live interrupt from its own unisolated ambient process
    environment. `operation` is deliberately a callable rather than a fixed
    call to `bridge_canary.run_managed_live_interrupt` directly: the release
    lane needs that exact low-level call, but the harness needs to go through
    `run_codex_provider_release_canary`'s dict-based entrypoint instead (to
    keep producing the wrapper-shaped artifact its operation_evidence/DB-ingest
    projection code already depends on) — both get the identical isolation
    setup and teardown either way, since `run_codex_provider_release_canary`
    re-derives `api_url`/`agents_token` from `os.environ` internally and will
    see the isolated values.

    Returns (operation_result, error, stop_evidence, mcp_bootstrap). Whatever
    `operation` returns is returned unchanged; a raised exception is caught
    and reported as `error` instead, preserving an infrastructure-error
    outcome as evidence rather than propagating.
    """
    secrets = (agents_token, provider_token)
    canary_root = output_root / "canary-evidence"
    canary_root.mkdir(parents=True, exist_ok=True)
    result: Any = None
    error: str | None = None
    with _ENV_ISOLATION_LOCK, tempfile.TemporaryDirectory(prefix="longhouse-helm-qualification-") as runtime:
        runtime_root = Path(runtime)
        codex_home = runtime_root / "codex-home"
        codex_home.mkdir()
        (runtime_root / "tmp").mkdir()
        mcp_bootstrap = _prepare_inert_mcp_bootstrap(codex_home, canary_root, secrets)
        strict_env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": runtime,
            "CODEX_HOME": str(codex_home),
            "TMPDIR": str(runtime_root / "tmp"),
            ENGINE_ENV: str(engine),
            PACKAGE_ROOT_ENV: package_root,
            API_URL_ENV: api_url,
            AGENTS_TOKEN_ENV: agents_token,
            PROVIDER_TOKEN_ENV: provider_token,
        }
        original_env = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(strict_env)
            result = operation(canary_root, provider_bin)
        except Exception as exc:  # noqa: BLE001 - preserve infrastructure outcome as evidence
            result = None
            error = f"{type(exc).__name__}: {exc}"
        finally:
            os.environ.clear()
            os.environ.update(original_env)
    _scrub_evidence_tree(canary_root, secrets)
    result = _redact_value(result, secrets)
    error = _redact_text(error, secrets) if error else None
    stop = _stop_evidence(canary_root)
    return result, error, stop, mcp_bootstrap


def run_isolated_managed_live_interrupt(
    provider_bin: Path,
    *,
    engine: Path,
    package_root: str,
    api_url: str,
    agents_token: str,
    provider_token: str,
    repo_root: Path,
    output_root: Path,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None, dict[str, Any]]:
    """The release lane's exact original call shape, now a thin wrapper over
    run_isolated_codex_operation. Returns (canary_result, canary_error,
    stop_evidence, mcp_bootstrap)."""

    def _operation(canary_root: Path, binary: Path) -> dict[str, Any]:
        args = argparse.Namespace(
            engine=str(engine),
            repo_root=repo_root,
            api_url=api_url,
            agents_token=agents_token,
            model=None,
            bridge_start_timeout_secs=30,
            live_interrupt_timeout_secs=45,
        )
        return bridge_canary.run_managed_live_interrupt(args, canary_root, str(binary))

    canary_result, canary_error, stop, mcp_bootstrap = run_isolated_codex_operation(
        provider_bin,
        engine=engine,
        package_root=package_root,
        api_url=api_url,
        agents_token=agents_token,
        provider_token=provider_token,
        output_root=output_root,
        operation=_operation,
    )
    return canary_result or {}, canary_error, stop, mcp_bootstrap


def _record(
    *,
    request: dict[str, Any],
    provider_identity: str,
    provider_version: str,
    engine_identity: str | None,
    contract_digest: str,
    adapter_digest: str,
    oracle_digest: str,
    generated_at: str,
    raw_digest: str,
    assertion_id: str,
    outcome: AssertionOutcome,
    evidence_class: EvidenceClass,
) -> ProviderCapabilityProofRecord:
    return ProviderCapabilityProofRecord(
        provider="codex",
        provider_version=provider_version,
        provider_executable_identity=provider_identity,
        provider_build_identity=request["expected_provider_build_identity"],
        provider_build_granularity=request["expected_provider_build_granularity"],
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
        run_reference=request.get("run_reference"),
        mode="helm",
        permission_mode="bypass",
        platform=platform.system(),
        architecture=platform.machine(),
        raw_reference_digests=(raw_digest,),
        longhouse_build_id=engine_identity,
        longhouse_git_sha=request["longhouse_git_sha"],
    )


def emit_proof_bundle(
    *,
    request: dict[str, Any],
    output_root: Path,
    provider_identity: str,
    provider_version: str,
    engine_identity: str | None,
    runner_sha: str,
    outcomes: dict[str, AssertionOutcome],
    evidence_class: EvidenceClass,
    execution: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Serialize outcomes into the standard proof-bundle.json shape.

    Public (was `_emit`) so both this module's own `run()` (the release-lane
    legacy path) and the harness-backed bridge script
    (scripts/qa/provider-harness-qualification.py, Phase 2's "bridge/dispatcher
    design" — docs/specs/provider-factory-coherence.md) produce byte-identical
    proof records from the same inputs. The caller is responsible for
    obtaining and validating identity/version/engine provenance before
    calling this — that acquisition logic legitimately differs between
    staged-release qualification and harness-backed qualification and is not
    shared.
    """
    contract = contract_for_provider("codex")
    if contract is None:
        raise identity_bridge.RequestError("Codex managed-provider contract is missing")
    raw_bytes = (json.dumps(observation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    raw_digest = identity_bridge._sha256(raw_bytes)  # noqa: SLF001
    generated_at = identity_bridge._now()  # noqa: SLF001
    execution = {
        **execution,
        "invocation_id": request["invocation_id"],
        "platform": platform.system(),
        "architecture": platform.machine(),
        "raw_evidence_digest": raw_digest,
        "runner_git_sha": runner_sha,
        "engine_executable_identity": engine_identity,
    }
    identity_bridge._atomic_json(output_root / "request.json", request)  # noqa: SLF001
    identity_bridge._atomic_json(output_root / "raw-evidence.json", observation)  # noqa: SLF001
    identity_bridge._atomic_json(output_root / "execution-summary.json", execution)  # noqa: SLF001
    oracle_digest = identity_bridge._sha256(Path(__file__).read_bytes())  # noqa: SLF001
    store = ProviderCapabilityProofStore(output_root / "proof-store")
    records = []
    for assertion_id in ASSERTIONS:
        record = _record(
            request=request,
            provider_identity=provider_identity,
            provider_version=provider_version,
            engine_identity=engine_identity,
            contract_digest=contract.contract_entry_digest,
            adapter_digest=contract.adapter_digest,
            oracle_digest=oracle_digest,
            generated_at=generated_at,
            raw_digest=raw_digest,
            assertion_id=assertion_id,
            outcome=outcomes[assertion_id],
            evidence_class=evidence_class,
        )
        store.write(record)
        records.append(record)
    serialized = {key: value.value for key, value in outcomes.items()}
    coverage = {
        "profile": PROFILE,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": SCENARIO_REVISION,
        "evidence_class": evidence_class.value,
        "assertions": list(ASSERTIONS),
        "outcomes": serialized,
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
    if request.get("schema_version") == qualification_request.SCHEMA_VERSION:
        bundle.update(
            {
                "qualification_request_digest": request["semantic_digest"],
                "qualification_request_policy": qualification_request.policy_payload(request),
                "qualification_request_metadata": qualification_request.metadata_payload(request),
            }
        )
    identity_bridge._atomic_json(output_root / "proof-bundle.json", bundle)  # noqa: SLF001
    return {
        "valid": True,
        "output_root": str(output_root),
        "proof_bundle": str(output_root / "proof-bundle.json"),
        "assertions": serialized,
        "execution_status": execution["status"],
    }


def codex_helm_interrupt_oracle(
    canary_result: dict[str, Any],
    stop: dict[str, Any] | None,
    *,
    canary_error: str | None,
) -> dict[str, AssertionOutcome]:
    """Pure: the managed-live-interrupt canary result + stop.json evidence ->
    typed outcomes for all three ASSERTIONS. No I/O, no subprocess.

    Extracted per Phase 2 step 1 (docs/specs/provider-factory-coherence.md).
    This profile judges exact turn identity (the interrupt must have hit the
    exact turn that was sent, not just any turn) and verified cleanup (stop.json
    must show an attempted stop, a clean subprocess exit, and post-hoc
    verification that the process reached a terminal state with its socket
    gone) — both timing-coupled interventions the spec's run-evidence index is
    designed to carry (see run_evidence_index.InterventionLogEntry).
    """
    start_summary = canary_result.get("start_summary") if isinstance(canary_result.get("start_summary"), dict) else {}
    send_summary = canary_result.get("send_summary") if isinstance(canary_result.get("send_summary"), dict) else {}
    retained_state = canary_result.get("state") if isinstance(canary_result.get("state"), dict) else {}
    send_active_evidence = bool(
        str(send_summary.get("thread_id") or "").strip()
        and str(send_summary.get("turn_id") or "").strip()
        and str(send_summary.get("turn_status") or "").strip()
    )
    send_active = send_active_evidence and str(send_summary.get("turn_status") or "").lower() in {
        "inprogress",
        "in_progress",
        "running",
    }
    state_active_evidence = bool(
        str(retained_state.get("active_turn_id") or "").strip()
        and str(retained_state.get("last_turn_status") or "").strip()
        and str(retained_state.get("thread_id") or start_summary.get("thread_id") or "").strip()
    )
    state_active = state_active_evidence and str(retained_state.get("last_turn_status") or "").lower() in {
        "inprogress",
        "in_progress",
        "running",
    }
    active_turn = send_active or state_active
    active_evidence_available = send_active_evidence or state_active_evidence
    sent_turn_id = str(send_summary.get("turn_id") or "")
    interrupted_turn_id = str(canary_result.get("interrupted_turn_id") or "")
    exact_turn_interrupted = bool(sent_turn_id and interrupted_turn_id == sent_turn_id)
    terminal_status = canary_result.get("last_turn_status") or retained_state.get("last_turn_status")
    terminal_evidence_available = bool(str(terminal_status or "").strip())
    terminal = exact_turn_interrupted and str(terminal_status or "").lower() in {"interrupted", "cancelled"}
    cleanup = bool(
        stop
        and stop.get("attempted") is True
        and isinstance(stop.get("evidence"), dict)
        and stop["evidence"].get("returncode") == 0
        and isinstance(stop.get("verification"), dict)
        and stop["verification"].get("verified") is True
        and stop["verification"].get("terminal_state") is True
        and stop["verification"].get("socket_absent") is True
    )
    failure_code = str(canary_result.get("failure_code") or "")
    semantic_completion = canary_result.get("status") == "pass" or failure_code in _SEMANTIC_FAILURE_CODES
    if canary_error or not semantic_completion:
        return {assertion: AssertionOutcome.INFRASTRUCTURE_ERROR for assertion in ASSERTIONS}
    return {
        "active_managed_turn_observed": (
            AssertionOutcome.PASS
            if active_turn
            else AssertionOutcome.SEMANTIC_FAIL
            if active_evidence_available
            else AssertionOutcome.INFRASTRUCTURE_ERROR
        ),
        "interrupt_terminal_cancelled_or_interrupted": (
            AssertionOutcome.PASS
            if terminal
            else AssertionOutcome.SEMANTIC_FAIL
            if terminal_evidence_available
            else AssertionOutcome.INFRASTRUCTURE_ERROR
        ),
        "managed_bridge_cleanup_completed": (AssertionOutcome.PASS if cleanup else AssertionOutcome.INFRASTRUCTURE_ERROR),
    }


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = _load_request(request_path)
    output_root = output_root.expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[3]
    provider_bin, provider_identity, runner_sha = identity_bridge._preflight(  # noqa: SLF001
        request, output_root, repo_root
    )
    values, missing = _required_environment()
    if missing:
        outcomes = {assertion: AssertionOutcome.BLOCKED for assertion in ASSERTIONS}
        return emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=provider_identity,
            provider_version="unreported",
            engine_identity=None,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={
                "status": "blocked",
                "reason": "required_environment_missing",
                "engine_build_identity_probe_invocations": 0,
                "provider_version_probe_invocations": 0,
                "managed_bridge_starts_observed": 0,
            },
            observation={"blocked_reason": "required_environment_missing", "missing_environment": list(missing)},
        )

    engine = Path(values[ENGINE_ENV])
    if not engine.is_absolute():
        raise identity_bridge.RequestError(f"{ENGINE_ENV} must be an absolute path")
    engine_identity = _file_identity(engine, label=ENGINE_ENV, executable=True)
    package_root, package_identity, package_members = _package_identity(values[PACKAGE_ROOT_ENV], provider_bin)
    secrets = (values[AGENTS_TOKEN_ENV], values[PROVIDER_TOKEN_ENV])
    engine_build_probe = _probe_engine_build_identity(engine, request["longhouse_git_sha"], secrets)
    engine_observation = {
        "engine_executable_identity": engine_identity,
        "package_identity": package_identity,
        "engine_build_identity": engine_build_probe["identity"],
        "engine_build_identity_probe": engine_build_probe["evidence"],
    }
    if engine_build_probe["status"] != "pass":
        outcome = AssertionOutcome.BLOCKED if engine_build_probe["status"] == "blocked" else AssertionOutcome.INFRASTRUCTURE_ERROR
        outcomes = {assertion: outcome for assertion in ASSERTIONS}
        return emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=provider_identity,
            provider_version="unreported",
            engine_identity=engine_identity,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={
                "status": engine_build_probe["status"],
                "reason": engine_build_probe["reason"],
                "engine_build_identity_probe_invocations": 1,
                "provider_version_probe_invocations": 0,
                "managed_bridge_starts_observed": 0,
            },
            observation={
                **engine_observation,
                "blocked_reason": engine_build_probe["reason"],
            },
        )

    version_env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    try:
        version = subprocess.run(
            [str(provider_bin), "--version"],
            text=True,
            capture_output=True,
            env=version_env,
            timeout=identity_bridge.TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        outcomes = {assertion: AssertionOutcome.INFRASTRUCTURE_ERROR for assertion in ASSERTIONS}
        return emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=provider_identity,
            provider_version="unreported",
            engine_identity=engine_identity,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={
                "status": "infrastructure_error",
                "reason": type(exc).__name__,
                "engine_build_identity_probe_invocations": 1,
                "provider_version_probe_invocations": 1,
                "managed_bridge_starts_observed": 0,
            },
            observation={
                **engine_observation,
                "version_probe_error": type(exc).__name__,
            },
        )
    match = identity_bridge._VERSION_LINE.fullmatch(version.stdout.strip())  # noqa: SLF001
    reported_version = match.group("version") if match else None
    if version.returncode != 0 or reported_version is None:
        outcomes = {assertion: AssertionOutcome.INFRASTRUCTURE_ERROR for assertion in ASSERTIONS}
        return emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=provider_identity,
            provider_version="unreported",
            engine_identity=engine_identity,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={
                "status": "infrastructure_error",
                "reason": "provider_version_probe_failed",
                "engine_build_identity_probe_invocations": 1,
                "provider_version_probe_invocations": 1,
                "managed_bridge_starts_observed": 0,
            },
            observation={
                **engine_observation,
                "version_probe": {
                    "returncode": version.returncode,
                    "stdout": _redact_text(version.stdout, secrets),
                    "stderr": _redact_text(version.stderr, secrets),
                },
            },
        )
    if reported_version != request["expected_provider_version"]:
        outcomes = {assertion: AssertionOutcome.BLOCKED for assertion in ASSERTIONS}
        return emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=provider_identity,
            provider_version=reported_version,
            engine_identity=engine_identity,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={
                "status": "completed",
                "reason": "provider_version_mismatch",
                "engine_build_identity_probe_invocations": 1,
                "provider_version_probe_invocations": 1,
                "managed_bridge_starts_observed": 0,
            },
            observation={
                **engine_observation,
                "expected_provider_version": request["expected_provider_version"],
                "reported_provider_version": reported_version,
            },
        )

    canary_result, canary_error, stop, mcp_bootstrap = run_isolated_managed_live_interrupt(
        provider_bin,
        engine=engine,
        package_root=str(package_root),
        api_url=values[API_URL_ENV],
        agents_token=values[AGENTS_TOKEN_ENV],
        provider_token=values[PROVIDER_TOKEN_ENV],
        repo_root=repo_root,
        output_root=output_root,
    )

    outcomes = codex_helm_interrupt_oracle(canary_result, stop, canary_error=canary_error)
    execution_status = "infrastructure_error" if AssertionOutcome.INFRASTRUCTURE_ERROR in outcomes.values() else "completed"
    failure_code = str(canary_result.get("failure_code") or "")

    try:
        post_engine_identity = identity_bridge._sha256_file(engine)  # noqa: SLF001
        _, post_package_identity, _ = _package_identity(str(package_root), provider_bin)
        post_provider_identity = identity_bridge._sha256_file(provider_bin)  # noqa: SLF001
    except (OSError, identity_bridge.RequestError):
        post_engine_identity = None
        post_package_identity = None
        post_provider_identity = None
    identities_stable = (
        post_engine_identity == engine_identity
        and post_package_identity == package_identity
        and post_provider_identity == provider_identity
    )
    if not identities_stable:
        outcomes = {assertion: AssertionOutcome.INFRASTRUCTURE_ERROR for assertion in ASSERTIONS}
        execution_status = "infrastructure_error"

    observation = {
        "scope": "helm_bridge_interrupt_not_runtime_host_dispatch",
        "provider_executable_identity": provider_identity,
        "post_provider_executable_identity": post_provider_identity,
        "engine_path": str(engine),
        "engine_executable_identity": engine_identity,
        "engine_build_identity": engine_build_probe["identity"],
        "engine_build_identity_probe": engine_build_probe["evidence"],
        "post_engine_executable_identity": post_engine_identity,
        "package_root": str(package_root),
        "package_identity": package_identity,
        "post_package_identity": post_package_identity,
        "package_members": package_members,
        "reported_provider_version": reported_version,
        "mcp_bootstrap": mcp_bootstrap,
        "canary_result": canary_result,
        "canary_error": canary_error,
        "stop_evidence": stop,
        "identities_stable": identities_stable,
    }
    return emit_proof_bundle(
        request=request,
        output_root=output_root,
        provider_identity=provider_identity,
        provider_version=reported_version,
        engine_identity=engine_identity,
        runner_sha=runner_sha,
        outcomes=outcomes,
        evidence_class=EvidenceClass.LIVE_TOKEN,
        execution={
            "status": execution_status,
            "engine_build_identity_probe_invocations": 1,
            "provider_version_probe_invocations": 1,
            "managed_bridge_starts_observed": _managed_bridge_starts_observed(canary_result),
            "canary_invoked": True,
            "canary_status": canary_result.get("status"),
            "canary_failure_code": failure_code or None,
        },
        observation=observation,
    )
