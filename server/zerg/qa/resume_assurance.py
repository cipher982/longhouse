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
from dataclasses import asdict
from dataclasses import dataclass
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
    executable: bool = True

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
    if artifact.get("acquisition_method") not in registration.get("acquisition_methods", []):
        failures.append("acquisition_method")
    available_bindings = set(census.get("credential_binding_ids", []))
    if not set(registration.get("credential_binding_ids", [])).issubset(available_bindings):
        failures.append("credential_binding")
    if registration.get("sandbox_policy") != census.get("sandbox_policy"):
        failures.append("sandbox_policy")
    if registration.get("network_policy") != census.get("network_policy"):
        failures.append("network_policy")
    if registration.get("executable") is not True or not registration.get("executable_module"):
        failures.append("executable")
    if registration.get("scenario_id") != cell.get("scenario_id"):
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


def compile_resume_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Compile one deterministic report and, when valid, an executable plan."""

    diagnostics: list[dict[str, Any]] = []
    epoch = payload.get("accepted_epoch") if isinstance(payload.get("accepted_epoch"), Mapping) else {}
    census = payload.get("worker_census") if isinstance(payload.get("worker_census"), Mapping) else {}
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

    compiled_cells: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    for selected in sorted(expected_cells, key=_cell_sort_key):
        key = _cell_key(selected)
        cell = current_by_key.get(key)
        cell_diagnostics: list[dict[str, Any]] = []
        eligible: list[Mapping[str, Any]] = []
        if cell is None:
            cell_diagnostics.append(_diagnostic("selected_cell_missing", "selected Resume cell is absent", cell=selected))
        else:
            for item in census_producers:
                if not isinstance(item, Mapping) or not isinstance(item.get("registration"), Mapping):
                    continue
                registration = item["registration"]
                failures = _producer_supports_cell(registration, cell, census, subject)
                if not failures:
                    eligible.append(item)
            if not eligible:
                cell_diagnostics.append(_diagnostic("eligible_producer_missing", "no deployed producer can satisfy the cell", cell=cell))
            elif len(eligible) > 1:
                cell_diagnostics.append(
                    _diagnostic("eligible_producer_ambiguous", "more than one producer can satisfy the cell", cell=cell)
                )
        diagnostics.extend(cell_diagnostics)
        compiled: dict[str, Any] = {
            "cell": dict(selected),
            "valid": not cell_diagnostics,
            "diagnostics": cell_diagnostics,
            "producer_id": None,
        }
        if not cell_diagnostics:
            producer = eligible[0]
            registration = producer["registration"]
            compiled["producer_id"] = registration["producer_id"]
            command = {
                "producer_id": registration["producer_id"],
                "producer_revision": registration["producer_revision"],
                "scenario_id": registration["scenario_id"],
                "scenario_revision": registration["scenario_revision"],
                "assertion_id": selected["assertion_id"],
                "variant": selected["variant"],
                "module": registration["executable_module"],
                "oracle_source": registration["oracle_source"],
                "oracle_entrypoint": registration["oracle_entrypoint"],
                "evidence_class": sorted(set(registration["evidence_classes"]).intersection(cell["acceptable_evidence"]))[0],
                "required_activity": list(registration["observed_activity"]),
                "required_artifacts": list(registration["required_artifacts"]),
                "required_cleanup": list(registration["required_cleanup"]),
                "credential_binding_ids": list(registration["credential_binding_ids"]),
                "provider_artifact": dict(subject["provider_artifact"]),
                "longhouse_source_sha": subject["longhouse_source_sha"],
                "worker_id": census.get("worker_id"),
                "worker_platform": census.get("platform"),
                "worker_architecture": census.get("architecture"),
                "sandbox_policy": registration["sandbox_policy"],
                "network_policy": registration["network_policy"],
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
        }
        plan["plan_digest"] = content_digest(plan, "plan_digest")
        report["plan_digest"] = plan["plan_digest"]
    else:
        report["plan_digest"] = None
    report["report_digest"] = content_digest(report, "report_digest")
    return {"report": report, "plan": plan}


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
]
