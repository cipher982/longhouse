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
from zerg.qa import qualification_request
from zerg.qa.provider_event_digest import raw_event_digest as _native_event_digest
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


def _native_jsonl_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_has_provider_model(event: dict[str, Any]) -> bool:
    model = event.get("model")
    if not isinstance(model, str) and isinstance(event.get("message"), dict):
        model = event["message"].get("model")
    return isinstance(model, str) and bool(model.strip()) and model != "<synthetic>"


def _select_redacted_native_event(
    events: list[dict[str, Any]],
    *,
    source_canary: str,
    event_type: str,
    previous_digest: str | None,
) -> dict[str, Any] | None:
    if previous_digest:
        exact = next((event for event in events if _native_event_digest(event) == previous_digest), None)
        if exact is not None:
            return exact
    candidates = [event for event in events if event.get("type") == event_type]
    if source_canary == "cursor_model_probe":
        candidates = [event for event in candidates if event.get("subtype") == "success" and event.get("is_error") is not True]
        return candidates[0] if candidates else None
    if source_canary == "opencode_real_print":
        candidates = [
            event
            for event in events
            if event.get("type") in {"step_finish", "step-finish"}
            or (isinstance(event.get("part"), dict) and event["part"].get("type") == "step-finish")
        ]
    return candidates[-1] if candidates else None


def _select_model_source_event(events: list[dict[str, Any]], *, source_canary: str) -> dict[str, Any] | None:
    if source_canary == "cursor_model_probe":
        return next(
            (
                event
                for event in events
                if event.get("type") == "system" and event.get("subtype") == "init" and _event_has_provider_model(event)
            ),
            None,
        )
    if source_canary in {"claude_real_print", "real_print_canary"}:
        return next((event for event in events if _event_has_provider_model(event)), None)
    return None


def _rewrite_native_event_digests(value: Any, rewrites: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_rewrite_native_event_digests(item, rewrites) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"event_sha256", "native_event_sha256", "model_source_event_sha256"} and isinstance(item, str):
            rewritten[key] = rewrites.get(item, item)
        else:
            rewritten[key] = _rewrite_native_event_digests(item, rewrites)
    return rewritten


def _refresh_native_source_digests(value: Any, *, artifact_root: Path) -> Any:
    """Refresh and relativize source references after redaction.

    Redaction can change a JSONL event's bytes, so refresh both the file
    digest and every semantic pointer to the affected parsed event. Otherwise
    the factory would correctly reject an otherwise valid redacted capture as
    a stale native-event digest.
    """

    if isinstance(value, list):
        return [_refresh_native_source_digests(item, artifact_root=artifact_root) for item in value]
    if not isinstance(value, dict):
        return value
    refreshed = {key: _refresh_native_source_digests(item, artifact_root=artifact_root) for key, item in value.items()}
    source_artifacts = refreshed.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        return refreshed
    source_canary = str(refreshed.get("source_canary") or "")
    rewrites: dict[str, str] = {}
    updated_sources: list[Any] = []
    root = artifact_root.resolve()
    for source in source_artifacts:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            updated_sources.append(source)
            continue
        try:
            raw_path = Path(source["path"]).expanduser()
            path = (raw_path if raw_path.is_absolute() else root / raw_path).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise identity.RequestError("native source artifact escapes the qualification bundle or is missing")
            source_events = _native_jsonl_events(path) if source.get("kind") == "provider_jsonl_stream" else []
            previous_event_digest = source.get("event_sha256")
            selected_event = (
                _select_redacted_native_event(
                    source_events,
                    source_canary=source_canary,
                    event_type=str(source.get("event_type") or ""),
                    previous_digest=previous_event_digest if isinstance(previous_event_digest, str) else None,
                )
                if source_events and isinstance(source.get("event_type"), str)
                else None
            )
            if selected_event is not None and isinstance(previous_event_digest, str):
                rewrites[previous_event_digest] = _native_event_digest(selected_event)
            source = {
                **source,
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            if selected_event is not None:
                source["event_sha256"] = _native_event_digest(selected_event)
        except (OSError, ValueError) as exc:
            raise identity.RequestError("native source artifact could not be finalized") from exc
        updated_sources.append(source)
    refreshed["source_artifacts"] = updated_sources
    result_event = refreshed.get("result_event")
    model_source_digest = result_event.get("model_source_event_sha256") if isinstance(result_event, dict) else None
    if isinstance(model_source_digest, str) and model_source_digest not in rewrites:
        source_events = [
            event
            for source in updated_sources
            if isinstance(source, dict) and source.get("kind") == "provider_jsonl_stream" and isinstance(source.get("path"), str)
            for event in _native_jsonl_events((root / source["path"]).resolve())
        ]
        model_source_event = _select_model_source_event(source_events, source_canary=source_canary)
        if model_source_event is not None:
            rewrites[model_source_digest] = _native_event_digest(model_source_event)
    return _rewrite_native_event_digests(refreshed, rewrites)


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
    # The factory manifest is relative to the invocation root (the parent of
    # qualification-v2), not merely semantic-evidence. Keep source references
    # portable when the complete run bundle is copied to another node.
    observation = _refresh_native_source_digests(observation, artifact_root=output_root.parent)
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
            provider_build_identity=template.provider_build_identity,
            provider_build_granularity=template.provider_build_granularity,
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
    identity.atomic_json(output_root / "proof-bundle.json", bundle)
    return {
        "valid": True,
        "output_root": str(output_root),
        "proof_bundle": str(output_root / "proof-bundle.json"),
        "assertions": outcomes,
        "execution_status": execution["status"],
        "semantic_status": observation.get("status"),
    }
