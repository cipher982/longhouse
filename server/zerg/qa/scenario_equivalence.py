"""Semantic equivalence comparison for harness scenario results.

Phase 3 of the provider-factory-coherence epic
(docs/specs/provider-factory-coherence.md): "Build the fixture corpus and
semantic comparison: explicit outcomes, commands and arguments, transcript
projections, capability booleans, exit status, assertion identities,
checksums for non-JSON artifacts. Structural fingerprints stay schema-drift
diagnostics."

This module is the comparison half only -- a pure function judging whether
two `ScenarioResult.to_json()`-shaped dicts (a baseline capture and a
candidate capture of "the same" scenario, e.g. before/after a refactor, or
the legacy release-lane path vs the harness-backed bridge) are semantically
equivalent. It exists to guard the second half of Phase 3, not yet started:
splitting the harness's single adapter class into one `AgentHarnessAdapter`
implementation per provider. A regression in that split should show up here
before it ever reaches a real provider column.

Deliberately value-based, not schema-based: two results can use different
JSON shapes (a release-lane proof-bundle-derived payload vs a harness
ScenarioResult payload) and still be judged equivalent, because this checks
the semantic fields the spec names, not structural identity. Fields that are
expected to legitimately differ between two separate runs of the same
scenario (timestamps, invocation ids, raw content hashes of ordinary
non-identity data) are intentionally not compared for equality -- only
presence/shape, so a run that silently stops producing a field is still
caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Mapping

_COMMAND_KEYS = ("command", "argv")
_EXIT_STATUS_KEYS = ("returncode", "exit_code")
_OUTCOME_MAP_KEYS = ("strict_oracle", "assertions")
_IDENTITY_DIGEST_KEYS = (
    "raw_evidence_digest",
    "executable_identity",
    "pre_execution_identity",
    "post_execution_identity",
    "engine_identity",
    "package_identity",
)


@dataclass(frozen=True)
class EquivalenceMismatch:
    field: str
    reason: str
    baseline: Any = None
    candidate: Any = None


@dataclass(frozen=True)
class EquivalenceReport:
    equivalent: bool
    mismatches: tuple[EquivalenceMismatch, ...] = field(default_factory=tuple)


def _data(result: Mapping[str, Any]) -> Mapping[str, Any]:
    data = result.get("data")
    return data if isinstance(data, Mapping) else {}


def _compare_status(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    baseline_status = baseline.get("status")
    candidate_status = candidate.get("status")
    if baseline_status != candidate_status:
        return [EquivalenceMismatch("status", "explicit outcome differs", baseline=baseline_status, candidate=candidate_status)]
    return []


def _compare_outcome_maps(baseline_data: Mapping[str, Any], candidate_data: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    mismatches: list[EquivalenceMismatch] = []
    for key in _OUTCOME_MAP_KEYS:
        baseline_map = baseline_data.get(key)
        candidate_map = candidate_data.get(key)
        if baseline_map is None and candidate_map is None:
            continue
        if not isinstance(baseline_map, Mapping) or not isinstance(candidate_map, Mapping):
            mismatches.append(
                EquivalenceMismatch(
                    f"data.{key}", "present on one side only or not a mapping", baseline=baseline_map, candidate=candidate_map
                )
            )
            continue
        # Assertion identities: the key sets themselves must match exactly --
        # a scenario that silently drops or renames an assertion is exactly
        # the kind of regression this oracle exists to catch.
        if set(baseline_map) != set(candidate_map):
            mismatches.append(
                EquivalenceMismatch(
                    f"data.{key} assertion identities",
                    "assertion id sets differ",
                    baseline=sorted(baseline_map),
                    candidate=sorted(candidate_map),
                )
            )
            continue
        for assertion_id in sorted(baseline_map):
            if baseline_map[assertion_id] != candidate_map[assertion_id]:
                mismatches.append(
                    EquivalenceMismatch(
                        f"data.{key}.{assertion_id}",
                        "explicit outcome differs",
                        baseline=baseline_map[assertion_id],
                        candidate=candidate_map[assertion_id],
                    )
                )
    return mismatches


def _compare_commands(baseline_data: Mapping[str, Any], candidate_data: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    mismatches: list[EquivalenceMismatch] = []
    for key in _COMMAND_KEYS:
        baseline_value = baseline_data.get(key)
        candidate_value = candidate_data.get(key)
        if baseline_value is None and candidate_value is None:
            continue
        if baseline_value != candidate_value:
            mismatches.append(
                EquivalenceMismatch(f"data.{key}", "command/arguments differ", baseline=baseline_value, candidate=candidate_value)
            )
    return mismatches


def _compare_exit_status(baseline_data: Mapping[str, Any], candidate_data: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    mismatches: list[EquivalenceMismatch] = []
    for key in _EXIT_STATUS_KEYS:
        baseline_value = baseline_data.get(key)
        candidate_value = candidate_data.get(key)
        if baseline_value is None and candidate_value is None:
            continue
        if baseline_value != candidate_value:
            mismatches.append(EquivalenceMismatch(f"data.{key}", "exit status differs", baseline=baseline_value, candidate=candidate_value))
    return mismatches


def _compare_capability_booleans(baseline_data: Mapping[str, Any], candidate_data: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    baseline_evidence = baseline_data.get("operation_evidence")
    candidate_evidence = candidate_data.get("operation_evidence")
    if baseline_evidence is None and candidate_evidence is None:
        return []
    if not isinstance(baseline_evidence, Mapping) or not isinstance(candidate_evidence, Mapping):
        return [
            EquivalenceMismatch(
                "data.operation_evidence",
                "present on one side only or not a mapping",
                baseline=baseline_evidence,
                candidate=candidate_evidence,
            )
        ]
    mismatches: list[EquivalenceMismatch] = []
    if set(baseline_evidence) != set(candidate_evidence):
        mismatches.append(
            EquivalenceMismatch(
                "data.operation_evidence operations",
                "operation id sets differ",
                baseline=sorted(baseline_evidence),
                candidate=sorted(candidate_evidence),
            )
        )
        return mismatches
    for operation in sorted(baseline_evidence):
        baseline_status = (baseline_evidence[operation] or {}).get("status") if isinstance(baseline_evidence[operation], Mapping) else None
        candidate_status = (
            (candidate_evidence[operation] or {}).get("status") if isinstance(candidate_evidence[operation], Mapping) else None
        )
        if baseline_status != candidate_status:
            mismatches.append(
                EquivalenceMismatch(
                    f"data.operation_evidence.{operation}.status",
                    "capability status differs",
                    baseline=baseline_status,
                    candidate=candidate_status,
                )
            )
    return mismatches


def _compare_identity_digest_shape(baseline_data: Mapping[str, Any], candidate_data: Mapping[str, Any]) -> list[EquivalenceMismatch]:
    """Checksums for non-JSON artifacts: two separate runs legitimately
    produce different digest *values* (different binaries, different raw
    bytes), so this checks presence/shape, not equality -- a field that
    silently stops being populated is the regression this guards against."""
    mismatches: list[EquivalenceMismatch] = []
    for key in _IDENTITY_DIGEST_KEYS:
        baseline_present = key in baseline_data and baseline_data[key] is not None
        candidate_present = key in candidate_data and candidate_data[key] is not None
        if baseline_present != candidate_present:
            mismatches.append(
                EquivalenceMismatch(
                    f"data.{key}",
                    "digest field present on one side only",
                    baseline=baseline_data.get(key),
                    candidate=candidate_data.get(key),
                )
            )
    return mismatches


def compare_scenario_results(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> EquivalenceReport:
    """Compare two `ScenarioResult.to_json()`-shaped dicts for semantic
    equivalence across the fields docs/specs/provider-factory-coherence.md's
    Phase 3 names. Pure, no I/O -- callers own loading fixtures from disk or
    a live run."""
    baseline_data = _data(baseline)
    candidate_data = _data(candidate)
    mismatches = [
        *_compare_status(baseline, candidate),
        *_compare_outcome_maps(baseline_data, candidate_data),
        *_compare_commands(baseline_data, candidate_data),
        *_compare_exit_status(baseline_data, candidate_data),
        *_compare_capability_booleans(baseline_data, candidate_data),
        *_compare_identity_digest_shape(baseline_data, candidate_data),
    ]
    return EquivalenceReport(equivalent=not mismatches, mismatches=tuple(mismatches))
