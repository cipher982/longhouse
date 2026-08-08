"""Pure compiler contract for the trusted Helm Resume factory slice.

The private factory supplies already-read inputs.  This module performs no I/O,
does not inspect environment variables, and does not discover executable code.
That makes an accepted copy suitable for the read-only verifier bundle: a
candidate checkout is data to this compiler, never the authority deciding
whether its own Resume implementation is acceptable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Mapping
from typing import Sequence

PROFILE_ID = "helm_resume_v1"
SCHEMA_VERSION = 1
NATIVE_RESUME_ASSERTION = "native_provider_resume_proven"
NATIVE_RESUME_VARIANTS = ("clean_exit", "process_loss")


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_json(payload: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(payload)).hexdigest()}"


def content_digest(payload: Mapping[str, Any], field: str) -> str:
    return sha256_json({key: value for key, value in payload.items() if key != field})


def execution_variant_key(
    *,
    provider: str,
    assertion_id: str,
    scenario_id: str,
    variant: object,
) -> str:
    """Return the durable execution key for one authored assertion cell."""

    if isinstance(variant, str) and variant:
        return variant
    return f"cell:{provider}:{assertion_id}:{scenario_id}"


@dataclass(frozen=True)
class ProducerRegistration:
    producer_id: str
    producer_revision: int
    scenario_id: str
    scenario_revision: int
    assertion_cells: tuple[tuple[str, str], ...]
    providers: tuple[str, ...]
    platforms: tuple[str, ...]
    architectures: tuple[str, ...]
    modes: tuple[str, ...]
    evidence_classes: tuple[str, ...]
    observed_activity: tuple[str, ...]
    acquisition_methods: tuple[str, ...]
    credential_binding_ids: tuple[str, ...]
    sandbox_policy: str
    network_policy: str
    required_artifacts: tuple[str, ...]
    required_cleanup: tuple[str, ...]
    implementation: str
    oracle_source: str
    oracle_entrypoint: str
    executable_module: str
    provider_artifact_required: bool = True
    executable: bool = True
    # Most producers own one native scenario.  A provider-neutral producer can
    # advertise a bounded set of scenario ids while retaining one immutable
    # registration and one executable entrypoint.
    scenario_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["assertion_cells"] = [{"assertion_id": assertion_id, "variant": variant} for assertion_id, variant in self.assertion_cells]
        for field in (
            "providers",
            "platforms",
            "architectures",
            "modes",
            "evidence_classes",
            "observed_activity",
            "acquisition_methods",
            "credential_binding_ids",
            "required_artifacts",
            "required_cleanup",
            "scenario_ids",
        ):
            payload[field] = list(payload[field])
        return payload


def capability_contract_shape(assertions: Sequence[object], *, provider: str, capability: str) -> list[dict[str, Any]]:
    """Render one authored capability into the compiler's stable input shape."""

    rows = []
    for assertion in assertions:
        if getattr(assertion, "provider") != provider or getattr(assertion, "capability") != capability:
            continue
        rows.append(
            {
                "provider": provider,
                "capability": capability,
                "disposition": getattr(assertion, "disposition", "implemented"),
                "assertion_id": getattr(assertion, "assertion_id"),
                "variant": getattr(assertion, "variant"),
                "scenario_id": getattr(assertion, "scenario_id"),
                "minimum_scenario_revision": getattr(assertion, "minimum_scenario_revision"),
                "oracle_source": getattr(assertion, "oracle_source"),
                "acceptable_evidence": sorted(getattr(assertion, "acceptable_evidence")),
                "max_age_seconds": getattr(assertion, "max_age_seconds"),
            }
        )
    return sorted(rows, key=_cell_sort_key)


def _cell_sort_key(cell: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(cell.get("provider") or ""),
        str(cell.get("capability") or ""),
        str(cell.get("assertion_id") or ""),
        str(cell.get("variant") or ""),
    )


def _cell_key(cell: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return _cell_sort_key(cell)


def _diagnostic(code: str, message: str, *, cell: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if cell is not None:
        payload["cell"] = {
            "provider": cell.get("provider"),
            "capability": cell.get("capability"),
            "assertion_id": cell.get("assertion_id"),
            "variant": cell.get("variant"),
        }
    return payload


def _validate_epoch(epoch: Mapping[str, Any], diagnostics: list[dict[str, Any]]) -> None:
    if epoch.get("schema_version") != SCHEMA_VERSION or epoch.get("profile") != PROFILE_ID:
        diagnostics.append(_diagnostic("accepted_epoch_schema_invalid", "accepted epoch schema/profile is invalid"))
    if epoch.get("epoch_digest") != content_digest(epoch, "epoch_digest"):
        diagnostics.append(_diagnostic("accepted_epoch_digest_mismatch", "accepted epoch content digest is invalid"))


def _validate_contract(
    accepted_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str], Mapping[str, Any]]:
    accepted = {_cell_key(row): row for row in accepted_rows}
    current = {_cell_key(row): row for row in current_rows}
    if len(accepted) != len(accepted_rows) or len(current) != len(current_rows):
        diagnostics.append(_diagnostic("contract_duplicate_cell", "Resume contract contains a duplicate assertion cell"))
    for key, baseline in accepted.items():
        candidate = current.get(key)
        if candidate is None:
            diagnostics.append(_diagnostic("contract_cell_removed", "accepted Resume assertion cell was removed", cell=baseline))
            continue
        for field in ("scenario_id", "oracle_source"):
            if candidate.get(field) != baseline.get(field):
                diagnostics.append(_diagnostic("contract_authority_changed", f"accepted {field} changed", cell=baseline))
        minimum_revision = candidate.get("minimum_scenario_revision")
        accepted_revision = baseline.get("minimum_scenario_revision")
        if not isinstance(minimum_revision, int) or isinstance(minimum_revision, bool) or minimum_revision < accepted_revision:
            diagnostics.append(
                _diagnostic(
                    "minimum_scenario_revision_downgraded",
                    "minimum scenario revision is below the accepted epoch",
                    cell=baseline,
                )
            )
        candidate_evidence = candidate.get("acceptable_evidence")
        accepted_evidence = baseline.get("acceptable_evidence")
        if not isinstance(candidate_evidence, list) or not set(candidate_evidence).issubset(set(accepted_evidence or [])):
            diagnostics.append(_diagnostic("acceptable_evidence_broadened", "acceptable evidence was broadened", cell=baseline))
        max_age = candidate.get("max_age_seconds")
        accepted_max_age = baseline.get("max_age_seconds")
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age > accepted_max_age:
            diagnostics.append(_diagnostic("freshness_requirement_weakened", "proof freshness is weaker than accepted", cell=baseline))
    return current


def _producer_supports_cell(
    registration: Mapping[str, Any],
    cell: Mapping[str, Any],
    census: Mapping[str, Any],
    subject: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    declared_cells = {
        (item.get("assertion_id"), item.get("variant")) for item in registration.get("assertion_cells", []) if isinstance(item, Mapping)
    }
    if (cell.get("assertion_id"), cell.get("variant")) not in declared_cells:
        failures.append("cell")
    if cell.get("provider") not in registration.get("providers", []):
        failures.append("provider")
    if census.get("platform") not in registration.get("platforms", []):
        failures.append("platform")
    if census.get("architecture") not in registration.get("architectures", []):
        failures.append("architecture")
    artifact = subject.get("provider_artifact") if isinstance(subject.get("provider_artifact"), Mapping) else {}
    if registration.get("provider_artifact_required", True):
        for field in ("provider", "version", "executable_identity", "build_identity", "entrypoint", "build_root"):
            if not isinstance(artifact.get(field), str) or not artifact.get(field):
                failures.append(f"provider_artifact_{field}")
        if artifact.get("provider") != cell.get("provider"):
            failures.append("provider_artifact_provider")
        for field in ("executable_identity", "build_identity"):
            value = artifact.get(field)
            if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
                failures.append(f"provider_artifact_{field}_digest")
        if artifact.get("acquisition_method") not in registration.get("acquisition_methods", []):
            failures.append("acquisition_method")
        if artifact.get("platform") != census.get("platform"):
            failures.append("provider_artifact_platform")
        if artifact.get("architecture") != census.get("architecture"):
            failures.append("provider_artifact_architecture")
    available_bindings = set(census.get("credential_binding_ids", []))
    if not set(registration.get("credential_binding_ids", [])).issubset(available_bindings):
        failures.append("credential_binding")
    if registration.get("sandbox_policy") != census.get("sandbox_policy"):
        failures.append("sandbox_policy")
    if registration.get("network_policy") != census.get("network_policy"):
        failures.append("network_policy")
    if registration.get("executable") is not True or not registration.get("executable_module"):
        failures.append("executable")
    supported_scenarios = registration.get("scenario_ids") or [registration.get("scenario_id")]
    if cell.get("scenario_id") not in supported_scenarios:
        failures.append("scenario_id")
    scenario_revision = registration.get("scenario_revision")
    if not isinstance(scenario_revision, int) or scenario_revision < cell.get("minimum_scenario_revision", 0):
        failures.append("minimum_scenario_revision")
    if not set(registration.get("evidence_classes", [])).intersection(cell.get("acceptable_evidence", [])):
        failures.append("evidence_class")
    if not registration.get("observed_activity"):
        failures.append("observed_activity")
    if not registration.get("required_artifacts"):
        failures.append("required_artifacts")
    if not registration.get("required_cleanup"):
        failures.append("required_cleanup")
    return failures


def _reuse_failures(
    proof: Mapping[str, Any],
    cell: Mapping[str, Any],
    subject: Mapping[str, Any],
    epoch: Mapping[str, Any],
    scheduling: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    artifact = subject.get("provider_artifact") if isinstance(subject.get("provider_artifact"), Mapping) else {}
    expected = {
        "provider": cell.get("provider"),
        "assertion_id": cell.get("assertion_id"),
        "variant": execution_variant_key(
            provider=str(cell.get("provider") or ""),
            assertion_id=str(cell.get("assertion_id") or ""),
            scenario_id=str(cell.get("scenario_id") or ""),
            variant=cell.get("variant"),
        ),
        "longhouse_source_sha": subject.get("longhouse_source_sha"),
        "accepted_epoch_digest": epoch.get("epoch_digest"),
    }
    for field, value in expected.items():
        if proof.get(field) != value:
            failures.append(field)
    if proof.get("evidence_class") != "hermetic" and cell.get("provider") != "antigravity":
        for field in ("provider_version", "provider_executable_identity", "provider_build_identity"):
            expected_value = artifact.get(
                {
                    "provider_version": "version",
                    "provider_executable_identity": "executable_identity",
                    "provider_build_identity": "build_identity",
                }[field]
            )
            if proof.get(field) != expected_value:
                failures.append(field)
    if proof.get("evidence_class") not in cell.get("acceptable_evidence", []):
        failures.append("evidence_class")
    artifact_id = proof.get("artifact_id")
    if not isinstance(artifact_id, str) or len(artifact_id) != 64:
        failures.append("artifact_id")
    try:
        generated = datetime.fromisoformat(str(proof.get("generated_at") or "").replace("Z", "+00:00")).astimezone(UTC)
        evaluated = datetime.fromisoformat(str(scheduling.get("evaluated_at") or "").replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        failures.append("generated_at")
    else:
        if generated > evaluated or (evaluated - generated).total_seconds() >= int(cell.get("max_age_seconds") or 0):
            failures.append("freshness")
    if not isinstance(proof.get("publication_message_id"), str) or not proof.get("publication_message_id"):
        failures.append("publication_message_id")
    return failures


def compile_resume_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compile one deterministic report and, when valid, an executable plan."""

    diagnostics: list[dict[str, Any]] = []
    epoch = payload.get("accepted_epoch") if isinstance(payload.get("accepted_epoch"), Mapping) else {}
    census = payload.get("worker_census") if isinstance(payload.get("worker_census"), Mapping) else {}
    subjects = payload.get("subjects") if isinstance(payload.get("subjects"), Mapping) else {}
    subject = payload.get("subject") if isinstance(payload.get("subject"), Mapping) else {}
    current_rows = payload.get("current_contract") if isinstance(payload.get("current_contract"), list) else []
    current_inputs = payload.get("protected_inputs") if isinstance(payload.get("protected_inputs"), Mapping) else {}
    scheduling = payload.get("scheduling") if isinstance(payload.get("scheduling"), Mapping) else {}

    _validate_epoch(epoch, diagnostics)
    accepted_rows = epoch.get("contract_shape") if isinstance(epoch.get("contract_shape"), list) else []
    current_by_key = _validate_contract(accepted_rows, current_rows, diagnostics)

    accepted_inputs = epoch.get("protected_inputs") if isinstance(epoch.get("protected_inputs"), Mapping) else {}
    if current_inputs != accepted_inputs:
        diagnostics.append(_diagnostic("protected_input_digest_mismatch", "candidate proof-system inputs differ from the accepted epoch"))
    if census.get("factory_source_sha") != payload.get("factory_source_sha"):
        diagnostics.append(_diagnostic("factory_source_census_mismatch", "baked factory SHA differs from worker census"))
    if census.get("census_digest") != content_digest(census, "census_digest"):
        diagnostics.append(_diagnostic("worker_census_digest_mismatch", "worker census content digest is invalid"))
    if census.get("longhouse_source_sha") != subject.get("longhouse_source_sha"):
        diagnostics.append(_diagnostic("longhouse_source_census_mismatch", "subject Longhouse SHA differs from worker census"))
    if epoch.get("accepted_longhouse_sha") != subject.get("longhouse_source_sha"):
        diagnostics.append(_diagnostic("accepted_longhouse_source_mismatch", "subject Longhouse SHA is not the accepted epoch source"))
    if census.get("verifier_bundle_digest") != epoch.get("verifier_bundle_digest"):
        diagnostics.append(_diagnostic("verifier_bundle_mismatch", "worker verifier bundle is not the accepted bundle"))
    if census.get("compiler_digest") != epoch.get("compiler_digest"):
        diagnostics.append(_diagnostic("compiler_digest_mismatch", "worker compiler is not the accepted compiler"))

    accepted_producers = epoch.get("producers") if isinstance(epoch.get("producers"), list) else []
    accepted_by_id = {
        item.get("registration", {}).get("producer_id"): item
        for item in accepted_producers
        if isinstance(item, Mapping) and isinstance(item.get("registration"), Mapping)
    }
    census_producers = census.get("producers") if isinstance(census.get("producers"), list) else []
    census_by_id = {
        item.get("registration", {}).get("producer_id"): item
        for item in census_producers
        if isinstance(item, Mapping) and isinstance(item.get("registration"), Mapping)
    }
    if census_by_id != accepted_by_id:
        diagnostics.append(_diagnostic("producer_census_mismatch", "deployed producer census differs from accepted epoch"))

    expected_cells = epoch.get("selected_cells") if isinstance(epoch.get("selected_cells"), list) else []
    requested_cells = scheduling.get("requested_cells") if isinstance(scheduling.get("requested_cells"), list) else []
    if sorted(requested_cells, key=_cell_sort_key) != sorted(expected_cells, key=_cell_sort_key):
        diagnostics.append(_diagnostic("scheduled_cell_omission", "scheduling did not request every accepted Resume cell"))
    raw_decisions = scheduling.get("decisions") if isinstance(scheduling.get("decisions"), list) else []
    decision_by_key = {
        _cell_key(item["cell"]): item for item in raw_decisions if isinstance(item, Mapping) and isinstance(item.get("cell"), Mapping)
    }
    if len(decision_by_key) != len(raw_decisions) or set(decision_by_key) != {_cell_key(cell) for cell in expected_cells}:
        diagnostics.append(_diagnostic("scheduling_decisions_invalid", "scheduling did not decide every accepted Resume cell exactly once"))
    if scheduling.get("max_concurrency") != 1:
        diagnostics.append(_diagnostic("scheduling_concurrency_invalid", "Resume scheduling must remain single-concurrency"))

    compiled_cells: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    reused_proofs: list[dict[str, Any]] = []
    for selected in sorted(expected_cells, key=_cell_sort_key):
        key = _cell_key(selected)
        cell = current_by_key.get(key)
        cell_diagnostics: list[dict[str, Any]] = []
        eligible: list[Mapping[str, Any]] = []
        if cell is None:
            cell_diagnostics.append(_diagnostic("selected_cell_missing", "selected Resume cell is absent", cell=selected))
        else:
            provider_subject = subjects.get(str(cell.get("provider")))
            if not isinstance(provider_subject, Mapping):
                provider_subject = {}
            for item in census_producers:
                if not isinstance(item, Mapping) or not isinstance(item.get("registration"), Mapping):
                    continue
                registration = item["registration"]
                failures = _producer_supports_cell(registration, cell, census, provider_subject)
                if not failures:
                    eligible.append(item)
            if not eligible:
                cell_diagnostics.append(_diagnostic("eligible_producer_missing", "no deployed producer can satisfy the cell", cell=cell))
            elif len(eligible) > 1:
                cell_diagnostics.append(
                    _diagnostic("eligible_producer_ambiguous", "more than one producer can satisfy the cell", cell=cell)
                )
            decision = decision_by_key.get(key)
            action = decision.get("action") if isinstance(decision, Mapping) else None
            if action not in {"execute", "reuse"}:
                cell_diagnostics.append(_diagnostic("scheduling_action_invalid", "cell scheduling action is invalid", cell=cell))
            elif action == "reuse":
                proof = decision.get("proof") if isinstance(decision.get("proof"), Mapping) else {}
                failures = _reuse_failures(proof, cell, provider_subject, epoch, scheduling)
                if failures:
                    cell_diagnostics.append(
                        _diagnostic("scheduled_proof_not_reusable", f"scheduled proof cannot be reused: {sorted(set(failures))}", cell=cell)
                    )
        diagnostics.extend(cell_diagnostics)
        compiled: dict[str, Any] = {
            "cell": dict(selected),
            "valid": not cell_diagnostics,
            "diagnostics": cell_diagnostics,
            "producer_id": None,
            "disposition": None,
        }
        if not cell_diagnostics:
            producer = eligible[0]
            registration = producer["registration"]
            compiled["producer_id"] = registration["producer_id"]
            decision = decision_by_key[key]
            compiled["disposition"] = decision["action"]
            if decision["action"] == "reuse":
                reused_proofs.append(dict(decision["proof"]))
                compiled_cells.append(compiled)
                continue
            command = {
                "producer_id": registration["producer_id"],
                "producer_revision": registration["producer_revision"],
                "scenario_id": cell["scenario_id"],
                "scenario_revision": registration["scenario_revision"],
                "assertion_id": selected["assertion_id"],
                "variant": selected["variant"],
                "execution_variant": execution_variant_key(
                    provider=str(selected["provider"]),
                    assertion_id=str(selected["assertion_id"]),
                    scenario_id=str(cell["scenario_id"]),
                    variant=selected.get("variant"),
                ),
                "module": registration["executable_module"],
                "oracle_source": registration["oracle_source"],
                "oracle_entrypoint": registration["oracle_entrypoint"],
                "evidence_class": sorted(set(registration["evidence_classes"]).intersection(cell["acceptable_evidence"]))[0],
                "required_activity": list(registration["observed_activity"]),
                "required_artifacts": list(registration["required_artifacts"]),
                "required_cleanup": list(registration["required_cleanup"]),
                "credential_binding_ids": list(registration["credential_binding_ids"]),
                "provider": selected["provider"],
                "capability": cell.get("capability"),
                "disposition": cell.get("disposition", "implemented"),
                "subject_id": provider_subject.get("subject_id"),
                "provider_artifact": dict(provider_subject.get("provider_artifact") or {}),
                "provider_artifact_required": registration.get("provider_artifact_required", True),
                "provider_contract_digest": sha256_json(cell),
                "adapter_digest": producer.get("code_digest"),
                "oracle_digest": producer.get("oracle_digest"),
                "longhouse_source_sha": subject["longhouse_source_sha"],
                "worker_id": census.get("worker_id"),
                "worker_platform": census.get("platform"),
                "worker_architecture": census.get("architecture"),
                "sandbox_policy": registration["sandbox_policy"],
                "network_policy": registration["network_policy"],
                "priority": "release_gate",
                "timeout_seconds": 600,
                "max_cost_usd": 2.0,
                "freshness_max_age_seconds": cell["max_age_seconds"],
                "scheduling_reason": decision["reason"],
            }
            commands.append(command)
        compiled_cells.append(compiled)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "resume_assurance_compile_report",
        "profile": PROFILE_ID,
        "epoch_id": epoch.get("epoch_id"),
        "epoch_digest": epoch.get("epoch_digest"),
        "input_digest": sha256_json(payload),
        "subject": dict(subject),
        "worker_census_digest": census.get("census_digest"),
        "valid": not diagnostics,
        "diagnostics": diagnostics,
        "cells": compiled_cells,
    }
    plan: dict[str, Any] | None = None
    if not diagnostics:
        plan = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "resume_assurance_executable_plan",
            "profile": PROFILE_ID,
            "epoch_id": epoch.get("epoch_id"),
            "epoch_digest": epoch.get("epoch_digest"),
            "compile_input_digest": report["input_digest"],
            "worker_census_digest": census.get("census_digest"),
            "subject": dict(subject),
            "commands": commands,
            "reused_proofs": reused_proofs,
            "cost_budget_usd": scheduling.get("total_execute_cost_budget_usd"),
            "max_concurrency": scheduling.get("max_concurrency"),
        }
        plan["plan_digest"] = content_digest(plan, "plan_digest")
        report["plan_digest"] = plan["plan_digest"]
    else:
        report["plan_digest"] = None
    report["report_digest"] = content_digest(report, "report_digest")
    return {"report": report, "plan": plan}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("compile input must be a JSON object")
        compiled = compile_resume_plan(payload)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(compiled, sort_keys=True))
    return 0


__all__ = [
    "NATIVE_RESUME_ASSERTION",
    "NATIVE_RESUME_VARIANTS",
    "PROFILE_ID",
    "ProducerRegistration",
    "canonical_json",
    "capability_contract_shape",
    "compile_resume_plan",
    "content_digest",
    "sha256_json",
    "execution_variant_key",
]


if __name__ == "__main__":
    raise SystemExit(main())
