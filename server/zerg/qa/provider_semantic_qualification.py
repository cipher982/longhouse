"""Shared exact-executable envelope for provider-owned semantic canaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from typing import Callable

from zerg.qa import provider_release_identity as identity
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass
from zerg.services.provider_capability_proof import ProviderCapabilityProofRecord
from zerg.services.provider_capability_proof import proof_record_from_mapping
from zerg.services.provider_capability_proof_store import ProviderCapabilityProofStore


@dataclass(frozen=True)
class SemanticAssertion:
    assertion_id: str
    outcome: AssertionOutcome
    evidence_class: EvidenceClass


SemanticExecutor = Callable[[Path, Path], tuple[dict[str, Any], tuple[SemanticAssertion, ...], tuple[str, ...]]]


@contextmanager
def temporary_environment(values: dict[str, str | None]):
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def load_control_canary_module(repo_root: Path) -> ModuleType:
    path = repo_root / "scripts" / "qa" / "provider-control-e2e-canary.py"
    spec = importlib.util.spec_from_file_location("longhouse_provider_control_e2e_canary", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load provider control canary: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = identity.redact_text(value)
        for index, secret in enumerate(secrets, start=1):
            if secret:
                redacted = redacted.replace(secret, f"[QUALIFICATION_SECRET_{index}]")
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item, secrets) for key, item in value.items()}
    return value


def _scrub_tree(root: Path, secrets: tuple[str, ...]) -> None:
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


def _identity_records(output_root: Path) -> list[ProviderCapabilityProofRecord]:
    payload = json.loads((output_root / "proof-bundle.json").read_text(encoding="utf-8"))
    return [proof_record_from_mapping(item) for item in payload.get("records") or []]


def _blocked_from_identity(
    assertion_ids: tuple[str, ...],
    identity_outcomes: dict[str, str],
) -> tuple[SemanticAssertion, ...]:
    infrastructure = "infrastructure_error" in set(identity_outcomes.values())
    outcome = AssertionOutcome.INFRASTRUCTURE_ERROR if infrastructure else AssertionOutcome.BLOCKED
    return tuple(SemanticAssertion(assertion_id, outcome, EvidenceClass.LIVE_NO_TOKEN) for assertion_id in assertion_ids)


def run_semantic_profile(
    request_path: Path,
    output_root: Path,
    *,
    profile: identity.IdentityProfile,
    assertion_ids: tuple[str, ...],
    executor: SemanticExecutor,
    oracle_source: Path,
) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    identity_result = identity.run_identity_profile(
        request_path,
        output_root,
        profile=profile,
        repo_root=repo_root,
        git_sha_fn=identity.git_sha,
        git_dirty_fn=identity.git_dirty,
    )
    output_root = output_root.expanduser().resolve()
    request = json.loads((output_root / "request.json").read_text(encoding="utf-8"))
    binary = Path(request["provider_bin"]).resolve(strict=True)
    expected_identity = str(request["expected_executable_identity"])
    identity_outcomes = dict(identity_result["assertions"])
    observation: dict[str, Any]
    secrets: tuple[str, ...] = ()
    if set(identity_outcomes.values()) == {AssertionOutcome.PASS.value}:
        try:
            observation, semantic_assertions, secrets = executor(binary, output_root / "semantic-evidence")
        except Exception as exc:  # noqa: BLE001
            observation = {
                "status": "infrastructure_error",
                "failure_code": "semantic_canary_exception",
                "error": f"{type(exc).__name__}: {exc}",
            }
            semantic_assertions = tuple(
                SemanticAssertion(
                    assertion_id,
                    AssertionOutcome.INFRASTRUCTURE_ERROR,
                    EvidenceClass.LIVE_NO_TOKEN,
                )
                for assertion_id in assertion_ids
            )
    else:
        observation = {
            "status": "blocked",
            "failure_code": "exact_executable_identity_unconfirmed",
            "identity_outcomes": identity_outcomes,
        }
        semantic_assertions = _blocked_from_identity(assertion_ids, identity_outcomes)

    if tuple(item.assertion_id for item in semantic_assertions) != assertion_ids:
        raise identity.RequestError("semantic executor returned an unexpected assertion set")
    try:
        post_semantic_identity = identity.sha256_file(binary)
    except OSError:
        post_semantic_identity = None
    if post_semantic_identity != expected_identity:
        observation = {
            **observation,
            "status": "infrastructure_error",
            "failure_code": "provider_executable_changed_during_semantic_canary",
            "post_semantic_identity": post_semantic_identity,
        }
        semantic_assertions = tuple(
            SemanticAssertion(
                item.assertion_id,
                AssertionOutcome.INFRASTRUCTURE_ERROR,
                item.evidence_class,
            )
            for item in semantic_assertions
        )

    evidence_root = output_root / "semantic-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    observation = _redact_value(observation, secrets)
    _scrub_tree(evidence_root, secrets)
    semantic_path = evidence_root / "semantic-observation.json"
    identity.atomic_json(semantic_path, observation)
    raw_digest = identity.sha256(semantic_path.read_bytes())

    existing_records = _identity_records(output_root)
    if not existing_records:
        raise identity.RequestError("identity profile emitted no proof records")
    template = existing_records[0]
    generated_at = identity.now()
    oracle_digest = hashlib.sha256(oracle_source.read_bytes()).hexdigest()
    store = ProviderCapabilityProofStore(output_root / "proof-store")
    semantic_records: list[ProviderCapabilityProofRecord] = []
    for item in semantic_assertions:
        record = ProviderCapabilityProofRecord(
            provider=profile.provider,
            provider_version=template.provider_version,
            provider_executable_identity=expected_identity,
            provider_contract_digest=template.provider_contract_digest,
            adapter_digest=template.adapter_digest,
            scenario_id=profile.scenario_id,
            scenario_revision=identity.SCENARIO_REVISION,
            oracle_digest=oracle_digest,
            assertion_id=item.assertion_id,
            outcome=item.outcome,
            evidence_class=item.evidence_class,
            generated_at=generated_at,
            producer_class=request["producer_class"],
            producer_version=request["producer_version"],
            invocation_id=request["invocation_id"],
            run_reference=request.get("run_reference"),
            platform=platform.system(),
            architecture=platform.machine(),
            raw_reference_digests=(raw_digest,),
            longhouse_git_sha=request["longhouse_git_sha"],
        )
        store.write(record)
        semantic_records.append(record)

    records = [*existing_records, *semantic_records]
    outcomes = {record.assertion_id: record.outcome.value for record in records}
    coverage = {
        "profile": profile.profile,
        "scenario_id": profile.scenario_id,
        "scenario_revision": identity.SCENARIO_REVISION,
        "assertions": [record.assertion_id for record in records],
        "outcomes": outcomes,
        "complete": set(outcomes) == set(identity.ASSERTIONS) | set(assertion_ids),
    }
    execution = json.loads((output_root / "execution-summary.json").read_text(encoding="utf-8"))
    execution["semantic_status"] = observation.get("status")
    execution["semantic_evidence_digest"] = raw_digest
    identity.atomic_json(output_root / "execution-summary.json", execution)
    identity.atomic_json(output_root / "coverage-manifest.json", coverage)
    identity.atomic_json(
        output_root / "proof-bundle.json",
        {
            "artifact_kind": "provider_capability_proof_bundle",
            "schema_version": 2,
            "records": [record.serialize() for record in records],
            "execution_metadata": execution,
            "coverage_manifest": coverage,
        },
    )
    return {
        "valid": True,
        "output_root": str(output_root),
        "proof_bundle": str(output_root / "proof-bundle.json"),
        "assertions": outcomes,
        "execution_status": execution["status"],
        "semantic_status": observation.get("status"),
    }
