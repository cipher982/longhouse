"""Provider-neutral strict identity qualification for staged CLI executables."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Pattern

from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore

SCHEMA_VERSION = 1
SCENARIO_REVISION = 1
ASSERTIONS = ("exact_executable_identity_observed", "reported_version_matches_expected")
TIMEOUT_SECONDS = 10
SEMVER = (
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)" r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?" r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
STRICT_SEMVER = re.compile(rf"^{SEMVER}$")
IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "profile",
        "provider_bin",
        "expected_provider_version",
        "expected_executable_identity",
        "invocation_id",
        "producer_class",
        "producer_version",
        "longhouse_git_sha",
    }
)
REDACTIONS = (
    (re.compile(r"\bsk-[\w-]{20,}\b"), "[OPENAI_KEY]"),
    (re.compile(r"(?i)(bearer\s+)[a-zA-Z0-9_.-]{20,}"), r"\1[BEARER_TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[AWS_ACCESS_KEY]"),
    (
        re.compile(r"(?i)(secret|password|token|credential)[_-]?\s*[=:]\s*['\"]?[^\s'\"]{8,}['\"]?"),
        r"\1=[REDACTED]",
    ),
)


class RequestError(ValueError):
    pass


@dataclass(frozen=True)
class IdentityProfile:
    provider: str
    profile: str
    scenario_id: str
    version_line: Pattern[str]
    oracle_source: Path


def redact_text(value: str) -> str:
    for pattern, replacement in REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def atomic_json(path: Path, payload: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_request(path: Path, *, provider: str, profile: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestError(f"invalid request JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestError("request must be an object")
    unknown = set(payload) - REQUEST_KEYS
    if unknown:
        raise RequestError(f"unknown request keys: {sorted(unknown)}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RequestError("schema_version must be 1")
    if payload.get("provider") != provider or payload.get("profile") != profile:
        raise RequestError("unsupported provider/profile")
    for key in REQUEST_KEYS - {"schema_version", "provider", "profile"}:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise RequestError(f"{key} must be a non-empty string")
    if not Path(payload["provider_bin"]).is_absolute():
        raise RequestError("provider_bin must be absolute")
    if not IDENTITY.fullmatch(payload["expected_executable_identity"]):
        raise RequestError("expected_executable_identity must be sha256:<64 lowercase hex>")
    if not STRICT_SEMVER.fullmatch(payload["expected_provider_version"]):
        raise RequestError("expected_provider_version must be strict semver")
    if payload["producer_class"] != "local_diagnostic":
        raise RequestError("producer_class must be local_diagnostic")
    return payload


def git_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def git_dirty(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "status", "--porcelain"],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 0 or bool(result.stdout.strip())


def preflight(
    request: dict[str, Any],
    output_root: Path,
    repo_root: Path,
    *,
    git_sha_fn: Callable[[Path], str | None] = git_sha,
    git_dirty_fn: Callable[[Path], bool] = git_dirty,
) -> tuple[Path, str, str]:
    if output_root.exists():
        raise RequestError("output-root must not already exist")
    binary = Path(request["provider_bin"]).resolve(strict=False)
    if not binary.is_file():
        raise RequestError("provider_bin must resolve to a file")
    actual_runner_sha = git_sha_fn(repo_root)
    if actual_runner_sha != request["longhouse_git_sha"] or git_dirty_fn(repo_root):
        raise RequestError("Longhouse runner identity does not match a clean requested checkout")
    try:
        actual = sha256_file(binary)
    except OSError as exc:
        raise RequestError(f"provider_bin cannot be read: {exc}") from exc
    if actual != request["expected_executable_identity"]:
        raise RequestError("provider executable identity mismatch")
    try:
        output_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RequestError("output-root collision") from exc
    return binary, actual, actual_runner_sha


def _record(
    *,
    profile: IdentityProfile,
    request: dict[str, Any],
    identity: str,
    contract_digest: str,
    adapter_digest: str,
    oracle_digest: str,
    generated_at: str,
    raw_digest: str,
    outcome: AssertionOutcome,
    provider_version: str,
    assertion_id: str,
) -> ProviderCapabilityProofRecord:
    return ProviderCapabilityProofRecord(
        provider=profile.provider,
        provider_version=provider_version,
        provider_executable_identity=identity,
        provider_contract_digest=contract_digest,
        adapter_digest=adapter_digest,
        scenario_id=profile.scenario_id,
        scenario_revision=SCENARIO_REVISION,
        oracle_digest=oracle_digest,
        assertion_id=assertion_id,
        outcome=outcome,
        evidence_class=EvidenceClass.LIVE_NO_TOKEN,
        generated_at=generated_at,
        producer_class=request["producer_class"],
        producer_version=request["producer_version"],
        invocation_id=request["invocation_id"],
        platform=platform.system(),
        architecture=platform.machine(),
        raw_reference_digests=(raw_digest,),
        longhouse_git_sha=request["longhouse_git_sha"],
    )


def run_identity_profile(
    request_path: Path,
    output_root: Path,
    *,
    profile: IdentityProfile,
    repo_root: Path,
    timeout_seconds: float = TIMEOUT_SECONDS,
    git_sha_fn: Callable[[Path], str | None] = git_sha,
    git_dirty_fn: Callable[[Path], bool] = git_dirty,
) -> dict[str, Any]:
    request = load_request(request_path, provider=profile.provider, profile=profile.profile)
    output_root = output_root.expanduser().resolve()
    binary, actual_identity, runner_sha = preflight(
        request,
        output_root,
        repo_root,
        git_sha_fn=git_sha_fn,
        git_dirty_fn=git_dirty_fn,
    )
    generated_at = now()
    contract = contract_for_provider(profile.provider)
    if contract is None:
        raise RequestError(f"{profile.provider} managed-provider contract is missing")
    oracle_digest = sha256(Path(__file__).read_bytes() + b"\0" + profile.oracle_source.read_bytes())
    pre_execution_identity = sha256_file(binary)
    if pre_execution_identity != actual_identity:
        raise RequestError("provider executable changed before execution")
    argv = [str(binary), "--version"]
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    timed_out = False
    error: str | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        error = "timeout"
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    except OSError as exc:
        error = str(exc)
        stdout = ""
        stderr = ""
    else:
        stdout, stderr = result.stdout, result.stderr
    try:
        post_execution_identity = sha256_file(binary)
    except OSError:
        post_execution_identity = None
    match = profile.version_line.fullmatch(stdout.strip()) if not timed_out else None
    reported_version = match.group("version") if match else None
    process_returned = result is not None and not timed_out
    identity_outcome = AssertionOutcome.PASS if post_execution_identity == pre_execution_identity else AssertionOutcome.INFRASTRUCTURE_ERROR
    if not process_returned or result.returncode != 0:
        version_outcome = AssertionOutcome.INFRASTRUCTURE_ERROR
    elif reported_version != request["expected_provider_version"]:
        version_outcome = AssertionOutcome.SEMANTIC_FAIL
    else:
        version_outcome = AssertionOutcome.PASS
    observation = {
        "argv": argv,
        "provider": profile.provider,
        "profile": profile.profile,
        "provider_bin": str(binary),
        "executable_identity": actual_identity,
        "expected_executable_identity": request["expected_executable_identity"],
        "pre_execution_identity": pre_execution_identity,
        "post_execution_identity": post_execution_identity,
        "expected_provider_version": request["expected_provider_version"],
        "reported_version": reported_version,
        "stdout": redact_text(stdout),
        "stderr": redact_text(stderr),
        "returncode": result.returncode if result else None,
        "timed_out": timed_out,
        "error": error,
    }
    raw_bytes = (json.dumps(observation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    raw_digest = sha256(raw_bytes)
    atomic_json(output_root / "request.json", request)
    atomic_json(output_root / "raw-observation.json", observation)
    execution_status = "timed_out" if timed_out else "failed_to_start" if result is None else "completed"
    execution = {
        "invocation_id": request["invocation_id"],
        "argv": argv,
        "returncode": observation["returncode"],
        "timed_out": timed_out,
        "status": execution_status,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "raw_evidence_digest": raw_digest,
        "runner_git_sha": runner_sha,
    }
    atomic_json(output_root / "execution-summary.json", execution)
    store = ProviderCapabilityProofStore(output_root / "proof-store")
    provider_version = reported_version or "unreported"
    records = []
    for assertion_id, outcome in zip(ASSERTIONS, (identity_outcome, version_outcome)):
        record = _record(
            profile=profile,
            request=request,
            identity=actual_identity,
            contract_digest=contract.contract_entry_digest,
            adapter_digest=contract.adapter_digest,
            oracle_digest=oracle_digest,
            generated_at=generated_at,
            raw_digest=raw_digest,
            outcome=outcome,
            provider_version=provider_version,
            assertion_id=assertion_id,
        )
        store.write(record)
        records.append(record)
    outcomes = {record.assertion_id: record.outcome.value for record in records}
    coverage = {
        "profile": profile.profile,
        "scenario_id": profile.scenario_id,
        "scenario_revision": SCENARIO_REVISION,
        "evidence_class": EvidenceClass.LIVE_NO_TOKEN.value,
        "diagnostic": True,
        "required_product_capability": False,
        "assertions": list(ASSERTIONS),
        "outcomes": outcomes,
        "complete": set(outcomes) == set(ASSERTIONS),
    }
    atomic_json(output_root / "coverage-manifest.json", coverage)
    bundle = {
        "artifact_kind": "provider_capability_proof_bundle",
        "schema_version": 2,
        "records": [record.serialize() for record in records],
        "execution_metadata": execution,
        "coverage_manifest": coverage,
    }
    atomic_json(output_root / "proof-bundle.json", bundle)
    return {
        "valid": True,
        "output_root": str(output_root),
        "proof_bundle": str(output_root / "proof-bundle.json"),
        "assertions": outcomes,
        "execution_status": execution_status,
    }
