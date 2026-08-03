"""Bridge release qualification profiles through the universal harness.

Phase 2's "bridge/dispatcher design" (docs/specs/provider-factory-coherence.md):
`codex_tool_call_result_v1` and `codex_helm_interrupt_v1` are the two
release-lane profiles whose strict oracles can now be satisfied by a harness
scenario instead of each profile launching its own subprocess. The tool-result
profile also owns the release lane's complete universal Codex column. This
module loads the same staged-release request the release lane's own executors
consume, derives and verifies a real `ProviderBuildRef` (unlike the release
lane's own `run()`, which trusts the request's claimed build identity without
live re-verification), and emits the identical `proof-bundle.json` shape via
each profile's `emit_proof_bundle()` finalizer.

This remains an explicit profile dispatcher rather than a generic fallback.
Each admitted profile names its provider, strict scenario, assertion mapping,
and expected full-column limits. Codex profiles use their existing proof-bundle
finalizers. Claude uses the existing semantic-profile finalizer around one
harness execution, so the full column and real-print assertion share the same
provider call instead of paying for a duplicate legacy canary. OpenCode uses
that same envelope around its no-token server contract, real-tool projection,
and live-token behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zerg.qa import antigravity_hook_qualification
from zerg.qa import claude_real_print_qualification
from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity as identity_bridge
from zerg.qa import codex_tool_call_result
from zerg.qa import cursor_release_identity
from zerg.qa import opencode_server_qualification
from zerg.qa import provider_interaction_semantics as interaction_semantics
from zerg.qa import provider_release_identity
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_build_store import OBSERVED_INSTALL_PROVENANCE
from zerg.qa.provider_build_store import ProviderBuildRef
from zerg.qa.provider_build_store import materialize_staged_provider_build
from zerg.qa.provider_factory_model import DEFAULT_HARNESS_SCENARIOS
from zerg.qa.provider_factory_model import LIVE_TOKEN_HARNESS_SCENARIO
from zerg.qa.universal_agent_harness import HarnessOptions
from zerg.qa.universal_agent_harness import run_harness
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

RequestError = identity_bridge.RequestError
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The strict harness scenario -> ScenarioResult "data" keys it reports under
# strict_oracle, per profile.
_TOOL_CALL_RESULT_STRICT_KEYS = frozenset({"command_execution_completed_with_exact_output", "tool_result_linked_to_final_agent_message"})
_HELM_INTERRUPT_STRICT_KEYS = frozenset(
    {
        "active_managed_turn_observed",
        "interrupt_terminal_cancelled_or_interrupted",
        "managed_bridge_cleanup_completed",
    }
)

# The universal column has a few explicit, typed product limits. They are not
# regressions, and making the factory demand fake parity would erase useful
# contract truth. Everything else in the column must pass. A change to one of
# these rows is a deliberate contract change and should update this gate.
_EXPECTED_CODEX_FULL_COLUMN_LIMITS: dict[str, tuple[str, str | None]] = {
    "interaction_semantics": ("blocked", "interaction_live_policy_missing"),
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "managed_session_e2e": (
        "unsupported_gap",
        "codex_managed_bridge_credentials_missing",
    ),
    "helm_cold_resume": ("unsupported_gap", "codex_cold_resume_canary_missing"),
    "resume_unsupported": ("not_applicable", None),
}

_EXPECTED_CLAUDE_FULL_COLUMN_LIMITS: dict[str, tuple[str, str | None]] = {
    "interaction_semantics": ("blocked", "interaction_live_policy_missing"),
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "run_prompt_once": ("unsupported_gap", "run_prompt_once_not_safe_no_token"),
    "send_receive": ("unsupported_gap", "send_receive_not_safe_no_token"),
    "resume_unsupported": ("not_applicable", None),
}

_EXPECTED_OPENCODE_FULL_COLUMN_LIMITS: dict[str, tuple[str, str | None]] = {
    "interaction_semantics": ("blocked", "interaction_live_policy_missing"),
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "run_prompt_once": ("unsupported_gap", "run_prompt_once_not_safe_no_token"),
    "resume_unsupported": ("not_applicable", None),
}

_EXPECTED_ANTIGRAVITY_FULL_COLUMN_LIMITS: dict[str, tuple[str, str | None]] = {
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "run_prompt_once": ("unsupported_gap", "run_prompt_once_not_safe_no_token"),
    "send_receive": ("unsupported_gap", "send_receive_not_safe_no_token"),
    "helm_cold_resume": ("not_applicable", None),
    "helm_live_reattach": ("not_applicable", None),
    "console_thread_continue": ("not_applicable", None),
    "resume_identity_continuity": ("not_applicable", None),
    "resume_attempt_idempotency": ("not_applicable", None),
    "resume_single_owner": ("not_applicable", None),
    "resume_input_safety": ("not_applicable", None),
    "resume_failure_cleanup": ("not_applicable", None),
}

_EXPECTED_CURSOR_FULL_COLUMN_LIMITS: dict[str, tuple[str, str | None]] = {
    "interaction_semantics": ("blocked", "interaction_live_policy_missing"),
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "run_prompt_once": ("unsupported_gap", "run_prompt_once_not_safe_no_token"),
    "resume_unsupported": ("not_applicable", None),
}

_FULL_COLUMN_LIMITS = {
    "codex": _EXPECTED_CODEX_FULL_COLUMN_LIMITS,
    "claude": _EXPECTED_CLAUDE_FULL_COLUMN_LIMITS,
    "opencode": _EXPECTED_OPENCODE_FULL_COLUMN_LIMITS,
    "antigravity": _EXPECTED_ANTIGRAVITY_FULL_COLUMN_LIMITS,
    "cursor": _EXPECTED_CURSOR_FULL_COLUMN_LIMITS,
}

_LIVE_INTERACTION_ALLOWED_BLOCKED_CODES = frozenset(
    {
        "interaction_acknowledgement_missing",
        "interaction_live_probe_setup_failed",
        "interaction_native_raw_evidence_missing",
        "interaction_probe_setup_failed",
        "interaction_semantics_assertion_failed",
        "missing_isolated_auth",
    }
)

_FULL_COLUMN_ALLOWED_GAP_KINDS = {
    "codex": frozenset({"passed", "not_applicable", "provider_contract_unsupported", "missing_live_canary"}),
    "claude": frozenset({"passed", "no_token_safety_gate", "not_applicable", "missing_live_canary"}),
    "opencode": frozenset(
        {
            "passed",
            "no_token_safety_gate",
            "not_applicable",
            "provider_contract_unsupported",
            "missing_live_canary",
        }
    ),
    "antigravity": frozenset(
        {
            "passed",
            "no_token_safety_gate",
            "not_applicable",
            "provider_contract_unsupported",
        }
    ),
    "cursor": frozenset(
        {
            "passed",
            "no_token_safety_gate",
            "not_applicable",
            "provider_contract_unsupported",
            "missing_live_canary",
        }
    ),
}


@contextmanager
def _managed_package_root(build_ref: ProviderBuildRef):
    """Bind strict Codex identity checks to the materialized build under test."""
    name = codex_tool_call_result.MANAGED_PACKAGE_ROOT_ENV
    previous = os.environ.get(name)
    os.environ[name] = str(build_ref.build_root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _build_provider_build_ref(request: dict[str, Any], provider_bin: Path, *, output_root: Path) -> ProviderBuildRef:
    """Derive, validate, and materialize the staged build's ProviderBuildRef.

    Codex's profiles stage the documented full package at `bin/codex`; Claude
    and OpenCode stage one exact executable. Both roots and entrypoints are
    derived from `provider_bin`, then checked against the request's closure
    digest instead of trusting caller-supplied paths.

    Uses a run-scoped store under `output_root` — this is a per-run integrity
    check, not participation in control-plane's separate, already-working
    persistent build-store ingestion pipeline.
    """
    provider = request["provider"]
    granularity = request["expected_provider_build_granularity"]
    if provider == "codex" and granularity == "full_installed_tree":
        source_root = provider_bin.parent.parent
        entrypoint_relative = "bin/codex"
        codex_helm_interrupt._package_identity(str(source_root), provider_bin)  # noqa: SLF001
    elif provider in {"claude", "opencode", "antigravity"} and granularity == "single_asset":
        source_root = provider_bin.parent
        entrypoint_relative = provider_bin.name
        if tuple(path.name for path in source_root.iterdir()) != (provider_bin.name,):
            raise RequestError(f"{provider} single-asset staged build must contain exactly its provider entrypoint")
    elif provider == "cursor" and granularity == "full_installed_tree":
        source_root = provider_bin.parent
        entrypoint_relative = provider_bin.name
    else:
        raise RequestError(f"unsupported staged build shape for harness qualification: provider={provider!r}, granularity={granularity!r}")
    store_root = output_root / "provider-build-store"
    build_ref = materialize_staged_provider_build(
        provider=provider,
        version=request["expected_provider_version"],
        source_root=source_root,
        entrypoint_relative=entrypoint_relative,
        store_root=store_root,
        closure_granularity=granularity,
        artifact_provenance=(OBSERVED_INSTALL_PROVENANCE if provider == "cursor" else "staged_release"),
    )
    expected_identity = request["expected_provider_build_identity"]
    if expected_identity != f"sha256:{build_ref.closure_digest}":
        raise RequestError("staged build closure digest does not match expected_provider_build_identity")
    return build_ref


def _scenario_result(harness_payload: dict[str, Any], *, provider: str, scenario: str) -> dict[str, Any]:
    matches = [
        result for result in harness_payload.get("results", []) if result.get("provider") == provider and result.get("scenario") == scenario
    ]
    if len(matches) != 1:
        raise RequestError(f"expected exactly one {provider}/{scenario} harness result, got {len(matches)}")
    return matches[0]


def _copy_live_model_evidence(observation: dict[str, Any], scenario_result: Mapping[str, Any]) -> None:
    """Carry the universal harness's native receipt into the release envelope.

    The universal provider adapters produce this envelope for every
    model-backed lane. The release bridge must preserve it verbatim enough for
    the control-plane worker to verify the retained native source artifact;
    otherwise a provider can appear model-backed merely because its scenario
    status was ``pass``.
    """

    data = scenario_result.get("data")
    evidence = data.get("live_model_evidence") if isinstance(data, Mapping) else None
    if isinstance(evidence, Mapping):
        observation["live_model_evidence"] = dict(evidence)


def _reported_version(probe_result: dict[str, Any], expected_version: str) -> tuple[AssertionOutcome, str]:
    if probe_result.get("status") != "pass":
        return AssertionOutcome.INFRASTRUCTURE_ERROR, "unreported"
    version_text = str((probe_result.get("data") or {}).get("version") or "")
    match = identity_bridge._VERSION_LINE.fullmatch(version_text.strip())  # noqa: SLF001
    if match is None:
        return AssertionOutcome.INFRASTRUCTURE_ERROR, "unreported"
    reported = match.group("version")
    outcome = AssertionOutcome.PASS if reported == expected_version else AssertionOutcome.SEMANTIC_FAIL
    return outcome, reported


def _strict_outcomes(strict_result: dict[str, Any], *, required_keys: frozenset[str]) -> dict[str, AssertionOutcome]:
    """Read a scenario's `data.strict_oracle` map, failing closed rather than
    inventing an outcome for a scenario that never actually ran the strict
    check.

    A missing/invalid `strict_oracle` always means BLOCKED, regardless of the
    wrapping `status` -- by construction of both bridged scenarios, it is
    absent in exactly three cases and none of them mean "crashed":
    `codex_tool_call_result_strict`'s STATUS_UNSUPPORTED_GAP (no
    CODEX_API_KEY); `interrupt_cancel`'s own STATUS_BLOCKED (its strict-lane
    preflight, see "Closing the observation gap"); and `interrupt_cancel`'s
    Stage 1 hermetic-dispatch-proof fallback (missing bridge credentials,
    universal_agent_harness.py:_run_codex_interrupt_dispatch_proof), which
    legitimately reports STATUS_PASS/STATUS_FAIL for the hermetic check it
    did run while never attempting the strict one at all. Found and fixed as
    a real bug during the bridge/dispatcher design's equivalence testing:
    the earlier status-allowlisted version of this function misclassified
    that last case as INFRASTRUCTURE_ERROR.
    """
    strict_oracle = (strict_result.get("data") or {}).get("strict_oracle")
    if not isinstance(strict_oracle, dict):
        return dict.fromkeys(required_keys, AssertionOutcome.BLOCKED)
    if set(strict_oracle) != required_keys:
        raise RequestError(f"harness strict_oracle is missing required keys: {sorted(required_keys - set(strict_oracle))}")
    return {key: AssertionOutcome(value) for key, value in strict_oracle.items()}


def _validated_live_interaction_artifacts(
    provider: str,
    data: Mapping[str, Any],
) -> bool:
    """Re-evaluate live evidence from the materialized files named by a result.

    The harness result is a projection and may be hand-assembled by a caller.
    A path string and ``raw_provenance=pass`` are therefore insufficient on
    their own. Re-read both files, bind the JSONL to the observation, and run
    the provider-native oracle again so the full-column gate is grounded in
    the actual artifact bytes.
    """

    observation_path_value = data.get("raw_observation_path")
    events_path_value = data.get("raw_events_path")
    if not isinstance(observation_path_value, str) or not observation_path_value.strip():
        return False
    if not isinstance(events_path_value, str) or not events_path_value.strip():
        return False
    try:
        observation_path = Path(observation_path_value).expanduser().resolve()
        events_path = Path(events_path_value).expanduser().resolve()
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        events_text = events_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(observation, Mapping):
        return False
    if (
        observation.get("provider") != provider
        or observation.get("evidence_class") not in {"live_no_token", "live_token"}
        or observation.get("synthetic") is not False
        or events_text != interaction_semantics.jsonl_events(observation)
    ):
        return False
    try:
        materialized_root = Path(os.path.commonpath((str(observation_path.parent), str(events_path.parent)))).resolve(strict=True)
        independent = interaction_semantics.evaluate_observation(
            provider,
            observation,
            source_root=str(materialized_root),
        )
    except Exception:  # noqa: BLE001 - an invalid evidence file fails closed
        return False
    if independent.get("status") != "pass" or independent.get("provider_status") != "pass":
        return False
    if data.get("evidence_class") != observation.get("evidence_class"):
        return False
    if data.get("provider_status") != independent.get("provider_status"):
        return False
    if data.get("assertions") != independent.get("assertions"):
        return False
    return True


def _full_column_gate(
    harness_payload: dict[str, Any],
    *,
    provider: str = "codex",
    qualification_request_digest: str | None = None,
    interaction_evidence_class: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless a complete provider column has only known limits."""

    try:
        expected_limits = dict(_FULL_COLUMN_LIMITS[provider])
        allowed_gap_kinds = _FULL_COLUMN_ALLOWED_GAP_KINDS[provider]
    except KeyError:
        raise RequestError(f"no full-column gate is registered for provider {provider!r}") from None

    if interaction_evidence_class == "hermetic" and "interaction_semantics" in expected_limits:
        expected_limits["interaction_semantics"] = ("pass", None)

    results = harness_payload.get("results")
    if not isinstance(results, list):
        return {
            "status": "fail",
            "failure_code": "full_column_results_missing",
            "expected_scenarios": list(DEFAULT_HARNESS_SCENARIOS),
        }

    by_scenario: dict[str, list[dict[str, Any]]] = {
        scenario: [
            result
            for result in results
            if isinstance(result, dict) and result.get("provider") == provider and result.get("scenario") == scenario
        ]
        for scenario in DEFAULT_HARNESS_SCENARIOS
    }
    cardinality_errors = {scenario: len(matches) for scenario, matches in by_scenario.items() if len(matches) != 1}
    unexpected_results: list[dict[str, Any]] = []
    interaction_live_pass_provenance = False
    for scenario, matches in by_scenario.items():
        if len(matches) != 1:
            continue
        result = matches[0]
        actual = (result.get("status"), result.get("failure_code"))
        expected = expected_limits.get(scenario, ("pass", None))
        live_interaction_attempted = scenario == "interaction_semantics" and interaction_evidence_class in {
            "live_no_token",
            "live_token",
        }
        live_pass_provenance = False
        if live_interaction_attempted and actual == ("pass", None):
            data = result.get("data")
            assertions = data.get("assertions") if isinstance(data, Mapping) else None
            probe_assertions = (
                [row for row in assertions if isinstance(row, Mapping) and row.get("status") != "not_applicable"]
                if isinstance(assertions, list)
                else []
            )
            live_pass_provenance = (
                isinstance(data, Mapping)
                and data.get("verification_scope") == "provider_native"
                and data.get("provider_status") == "pass"
                and data.get("evidence_class") in {"live_no_token", "live_token"}
                and bool(probe_assertions)
                and any(row.get("probe_id") == "shared_title_boundary" for row in probe_assertions)
                and all(row.get("status") == "pass" for row in probe_assertions)
                and _validated_live_interaction_artifacts(provider, data)
            )
            if live_interaction_attempted:
                interaction_live_pass_provenance = live_pass_provenance
        actual_is_allowed_live_result = live_interaction_attempted and (
            (actual == ("pass", None) and live_pass_provenance)
            or (actual[0] == "blocked" and actual[1] in _LIVE_INTERACTION_ALLOWED_BLOCKED_CODES)
        )
        if actual != expected and not actual_is_allowed_live_result:
            unexpected_results.append(
                {
                    "scenario": scenario,
                    "expected_status": expected[0],
                    "expected_failure_code": expected[1],
                    "actual_status": actual[0],
                    "actual_failure_code": (
                        "interaction_live_provenance_missing"
                        if live_interaction_attempted and actual == ("pass", None) and not live_pass_provenance
                        else actual[1]
                    ),
                }
            )

    coverage = harness_payload.get("provider_execution_coverage_matrix")
    coverage = coverage if isinstance(coverage, dict) else {}
    gap_counts = (coverage.get("provider_coverage_gap_kind_counts") or {}).get(provider, {})
    unexpected_gap_kinds = {str(kind): count for kind, count in gap_counts.items() if kind not in allowed_gap_kinds and count}
    missing_actions = coverage.get("missing_provider_actions")
    coverage_complete = isinstance(missing_actions, list) and not missing_actions
    interaction_matches = by_scenario.get("interaction_semantics", [])
    provider_status = "not_applicable"
    if interaction_evidence_class in {"live_no_token", "live_token"}:
        if len(interaction_matches) != 1:
            provider_status = "fail"
        else:
            interaction_result = interaction_matches[0]
            interaction_actual = (interaction_result.get("status"), interaction_result.get("failure_code"))
            if interaction_actual == ("pass", None):
                provider_status = "pass" if interaction_live_pass_provenance else "fail"
            elif interaction_actual[0] == "blocked":
                # A known setup/auth limitation is a truthful provider result,
                # but it is not native qualification evidence and must never be
                # reported as a provider pass by a release executor.
                provider_status = "blocked"
            else:
                provider_status = "fail"
    interaction_digests: set[str] = set()
    for result in interaction_matches:
        observed_digest = result.get("qualification_request_digest")
        result_data = result.get("data")
        if observed_digest is None and isinstance(result_data, Mapping):
            observed_digest = result_data.get("qualification_request_digest")
        if isinstance(observed_digest, str):
            interaction_digests.add(observed_digest)
    observed_request_digest = next(iter(interaction_digests), None) if len(interaction_digests) == 1 else None
    request_binding_ok = qualification_request_digest is None or observed_request_digest == qualification_request_digest
    passed = not cardinality_errors and not unexpected_results and not unexpected_gap_kinds and coverage_complete and request_binding_ok
    return {
        "status": "pass" if passed else "fail",
        "provider_status": provider_status,
        "provider": provider,
        "failure_code": None if passed else f"{provider}_full_column_regression",
        "expected_scenario_count": len(DEFAULT_HARNESS_SCENARIOS),
        "captured_scenario_count": sum(1 for matches in by_scenario.values() if len(matches) == 1),
        "expected_limits": {
            scenario: {"status": status, "failure_code": failure_code}
            for scenario, (status, failure_code) in sorted(expected_limits.items())
        },
        "cardinality_errors": cardinality_errors,
        "unexpected_results": unexpected_results,
        "coverage_gap_kind_counts": gap_counts,
        "unexpected_coverage_gap_kinds": unexpected_gap_kinds,
        "missing_provider_actions": missing_actions,
        "qualification_request_digest": observed_request_digest,
        "qualification_request_binding": "pass" if request_binding_ok else "mismatch_or_missing",
    }


def run_codex_tool_call_result(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = codex_tool_call_result._load_request(request_path)  # noqa: SLF001
    provider_bin, pre_execution_identity, runner_sha = identity_bridge._preflight(  # noqa: SLF001
        request, output_root, _REPO_ROOT
    )
    generated_at = identity_bridge._now()  # noqa: SLF001

    try:
        build_ref = _build_provider_build_ref(request, provider_bin, output_root=output_root)
    except RequestError as exc:
        outcomes = dict.fromkeys(codex_tool_call_result.ASSERTIONS, AssertionOutcome.BLOCKED)
        return codex_tool_call_result.emit_proof_bundle(
            request=request,
            output_root=output_root,
            executable_identity=pre_execution_identity,
            runner_sha=runner_sha,
            generated_at=generated_at,
            provider_version="unreported",
            outcomes=outcomes,
            execution={"status": "blocked", "reason": "provider_build_ref_invalid", "error": str(exc)},
            observation={"provider_bin": str(provider_bin), "blocked_reason": str(exc)},
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
        )

    with _managed_package_root(build_ref):
        harness_payload = run_harness(
            HarnessOptions(
                providers=("codex",),
                scenarios=(*DEFAULT_HARNESS_SCENARIOS, "codex_tool_call_result_strict"),
                evidence_root=output_root / "harness-evidence",
                provider_bins={"codex": provider_bin},
                provider_builds={"codex": build_ref},
                qualification_request=request,
            )
        )
    probe_result = _scenario_result(harness_payload, provider="codex", scenario="probe_identity")
    strict_result = _scenario_result(harness_payload, provider="codex", scenario="codex_tool_call_result_strict")
    full_column_gate = _full_column_gate(
        harness_payload,
        qualification_request_digest=request.get("semantic_digest"),
        interaction_evidence_class=(request.get("scenario_evidence") or {}).get("interaction_semantics"),
    )

    post_execution_identity = identity_bridge._sha256_file(  # noqa: SLF001
        provider_bin
    )
    identity_outcome = AssertionOutcome.PASS if post_execution_identity == pre_execution_identity else AssertionOutcome.INFRASTRUCTURE_ERROR
    version_outcome, reported_version = _reported_version(probe_result, request["expected_provider_version"])
    strict_outcomes = _strict_outcomes(strict_result, required_keys=_TOOL_CALL_RESULT_STRICT_KEYS)

    outcomes = {
        "exact_executable_identity_observed": identity_outcome,
        "reported_version_matches_expected": version_outcome,
        **strict_outcomes,
    }
    ran_strict_check = strict_result.get("status") in {"pass", "fail"}
    evidence_class = EvidenceClass.LIVE_TOKEN if ran_strict_check else EvidenceClass.LIVE_NO_TOKEN
    if AssertionOutcome.INFRASTRUCTURE_ERROR in outcomes.values() or full_column_gate["status"] != "pass":
        execution_status = "infrastructure_error"
    elif full_column_gate.get("provider_status") == "blocked":
        execution_status = "blocked"
    elif full_column_gate.get("provider_status") == "fail":
        execution_status = "infrastructure_error"
    else:
        execution_status = "completed"

    observation = {
        "provider_bin": str(provider_bin),
        "pre_execution_identity": pre_execution_identity,
        "post_execution_identity": post_execution_identity,
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        "probe_identity": probe_result,
        "codex_tool_call_result_strict": strict_result,
    }
    _copy_live_model_evidence(observation, strict_result)
    return codex_tool_call_result.emit_proof_bundle(
        request=request,
        output_root=output_root,
        executable_identity=pre_execution_identity,
        runner_sha=runner_sha,
        generated_at=generated_at,
        provider_version=reported_version,
        outcomes=outcomes,
        execution={"status": execution_status},
        observation=observation,
        evidence_class=evidence_class,
    )


def run_codex_helm_interrupt(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = codex_helm_interrupt._load_request(request_path)  # noqa: SLF001
    provider_bin, pre_execution_identity, runner_sha = identity_bridge._preflight(  # noqa: SLF001
        request, output_root, _REPO_ROOT
    )

    try:
        build_ref = _build_provider_build_ref(request, provider_bin, output_root=output_root)
    except RequestError as exc:
        outcomes = dict.fromkeys(codex_helm_interrupt.ASSERTIONS, AssertionOutcome.BLOCKED)
        return codex_helm_interrupt.emit_proof_bundle(
            request=request,
            output_root=output_root,
            provider_identity=pre_execution_identity,
            provider_version="unreported",
            engine_identity=None,
            runner_sha=runner_sha,
            outcomes=outcomes,
            evidence_class=EvidenceClass.LIVE_NO_TOKEN,
            execution={"status": "blocked", "reason": "provider_build_ref_invalid", "error": str(exc)},
            observation={"provider_bin": str(provider_bin), "blocked_reason": str(exc)},
        )

    with _managed_package_root(build_ref):
        harness_payload = run_harness(
            HarnessOptions(
                providers=("codex",),
                scenarios=("probe_identity", "interrupt_cancel"),
                evidence_root=output_root / "harness-evidence",
                provider_bins={"codex": provider_bin},
                provider_builds={"codex": build_ref},
                qualification_request=request,
            )
        )
    probe_result = _scenario_result(harness_payload, provider="codex", scenario="probe_identity")
    interrupt_result = _scenario_result(harness_payload, provider="codex", scenario="interrupt_cancel")

    post_execution_identity = identity_bridge._sha256_file(  # noqa: SLF001
        provider_bin
    )
    identity_outcome = AssertionOutcome.PASS if post_execution_identity == pre_execution_identity else AssertionOutcome.INFRASTRUCTURE_ERROR
    version_outcome, reported_version = _reported_version(probe_result, request["expected_provider_version"])
    interrupt_data = interrupt_result.get("data") or {}
    # interrupt_cancel's own two-stage preflight (docs/specs/provider-factory-coherence.md,
    # "Closing the observation gap") can legitimately end this scenario in a
    # BLOCKED status (strict-lane inputs missing) or the hermetic dispatch
    # fallback (bridge credentials missing) without ever reaching the strict
    # oracle at all -- both must fail closed here, not be treated as "pass."
    strict_outcomes = _strict_outcomes(interrupt_result, required_keys=_HELM_INTERRUPT_STRICT_KEYS)
    engine_identity = interrupt_data.get("engine_identity")

    outcomes = {
        "active_managed_turn_observed": strict_outcomes["active_managed_turn_observed"],
        "interrupt_terminal_cancelled_or_interrupted": strict_outcomes["interrupt_terminal_cancelled_or_interrupted"],
        "managed_bridge_cleanup_completed": strict_outcomes["managed_bridge_cleanup_completed"],
    }
    # identity_outcome/version_outcome inform evidence_class/execution_status
    # below but codex_helm_interrupt.ASSERTIONS does not include them as
    # separate assertion ids (see codex_helm_interrupt.py:31-35) -- unlike
    # codex_tool_call_result, this profile only judges the three interrupt
    # outcomes.
    ran_strict_check = interrupt_data.get("strict_oracle") is not None
    evidence_class = EvidenceClass.LIVE_TOKEN if ran_strict_check else EvidenceClass.LIVE_NO_TOKEN
    infra_error = (
        identity_outcome != AssertionOutcome.PASS
        or version_outcome == AssertionOutcome.INFRASTRUCTURE_ERROR
        or AssertionOutcome.INFRASTRUCTURE_ERROR in outcomes.values()
        or (ran_strict_check and engine_identity is None)
    )
    if infra_error:
        outcomes = dict.fromkeys(codex_helm_interrupt.ASSERTIONS, AssertionOutcome.INFRASTRUCTURE_ERROR)
        execution_status = "infrastructure_error"
    elif all(outcome == AssertionOutcome.BLOCKED for outcome in outcomes.values()):
        # Matches the legacy release lane's own convention
        # (codex_helm_interrupt.run(): execution={"status": "blocked", ...}
        # when _required_environment() reports missing inputs) -- found via
        # the Phase 3 equivalence oracle (scenario_equivalence.py) comparing
        # real captures of both paths: this bridge reported "completed" for
        # a run where the strict check never attempted anything at all,
        # which would mislead a dashboard/alert distinguishing "ran, proved
        # nothing" from "didn't run."
        execution_status = "blocked"
    else:
        execution_status = "completed"

    observation = {
        "provider_bin": str(provider_bin),
        "pre_execution_identity": pre_execution_identity,
        "post_execution_identity": post_execution_identity,
        "provider_build": build_ref.to_evidence(),
        "probe_identity": probe_result,
        "interrupt_cancel": interrupt_result,
        "reported_version": reported_version,
    }
    return codex_helm_interrupt.emit_proof_bundle(
        request=request,
        output_root=output_root,
        provider_identity=pre_execution_identity,
        provider_version=reported_version,
        engine_identity=engine_identity,
        runner_sha=runner_sha,
        outcomes=outcomes,
        evidence_class=evidence_class,
        execution={"status": execution_status},
        observation=observation,
    )


def _claude_full_column_executor(
    binary: Path,
    evidence_root: Path,
    *,
    request: dict[str, Any],
) -> tuple[dict[str, Any], tuple[semantic.SemanticAssertion, ...], tuple[str, ...]]:
    build_ref = _build_provider_build_ref(request, binary, output_root=evidence_root)
    harness_payload = run_harness(
        HarnessOptions(
            providers=("claude",),
            scenarios=(*DEFAULT_HARNESS_SCENARIOS, LIVE_TOKEN_HARNESS_SCENARIO),
            evidence_root=evidence_root / "harness-evidence",
            provider_bins={"claude": binary},
            provider_builds={"claude": build_ref},
            qualification_request=request,
        )
    )
    launch_result = _scenario_result(
        harness_payload,
        provider="claude",
        scenario="launch_managed_session",
    )
    live_result = _scenario_result(
        harness_payload,
        provider="claude",
        scenario=LIVE_TOKEN_HARNESS_SCENARIO,
    )
    full_column_gate = _full_column_gate(
        harness_payload,
        provider="claude",
        qualification_request_digest=request.get("semantic_digest"),
        interaction_evidence_class=(request.get("scenario_evidence") or {}).get("interaction_semantics"),
    )

    no_token_verdict = {
        "pass": "green",
        "blocked": "yellow",
        "unsupported_gap": "yellow",
    }.get(str(launch_result.get("status")), "red")
    assertions = claude_real_print_qualification.claude_real_print_oracle(
        no_token_verdict=no_token_verdict,
        live_enabled=True,
        live_status=str(live_result.get("status")),
    )
    if full_column_gate["status"] != "pass" or full_column_gate.get("provider_status") == "fail":
        assertions = tuple(
            semantic.SemanticAssertion(
                assertion.assertion_id,
                AssertionOutcome.INFRASTRUCTURE_ERROR,
                assertion.evidence_class,
            )
            for assertion in assertions
        )
        status = "infrastructure_error"
    elif full_column_gate.get("provider_status") == "blocked":
        status = "blocked"
    elif AssertionOutcome.SEMANTIC_FAIL in {assertion.outcome for assertion in assertions}:
        status = "fail"
    elif AssertionOutcome.BLOCKED in {assertion.outcome for assertion in assertions}:
        status = "blocked"
    else:
        status = "pass"

    secrets = tuple(
        value for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if (value := str(os.environ.get(name) or "").strip())
    )
    observation = {
        "status": status,
        "provider_bin": str(binary),
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        "launch_managed_session": launch_result,
        LIVE_TOKEN_HARNESS_SCENARIO: live_result,
        "provider_execution_coverage_matrix_path": harness_payload.get("provider_execution_coverage_matrix_path"),
    }
    _copy_live_model_evidence(observation, live_result)
    return observation, assertions, secrets


def run_claude_real_print(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = provider_release_identity.load_request(
        request_path,
        provider="claude",
        profile=claude_real_print_qualification.PROFILE,
        version_grammar=claude_real_print_qualification._PROFILE.version_grammar,  # noqa: SLF001
    )

    def execute(binary: Path, evidence_root: Path):
        return _claude_full_column_executor(binary, evidence_root, request=request)

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=claude_real_print_qualification._PROFILE,  # noqa: SLF001
        assertion_ids=claude_real_print_qualification.ASSERTIONS,
        executor=execute,
        oracle_source=Path(semantic_oracles.__file__),
    )


def _opencode_server_canaries(managed_result: dict[str, Any], *, evidence_root: Path) -> dict[str, Any]:
    artifact_value = (managed_result.get("data") or {}).get("provider_live_artifact_path")
    if not isinstance(artifact_value, str) or not artifact_value.strip():
        raise RequestError("OpenCode managed_session_e2e did not report its provider-live artifact")
    artifact_path = Path(artifact_value).expanduser().resolve(strict=True)
    allowed_root = (evidence_root / "harness-evidence").resolve(strict=True)
    if not artifact_path.is_relative_to(allowed_root):
        raise RequestError("OpenCode provider-live artifact escaped the harness evidence root")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestError(f"invalid OpenCode provider-live artifact: {exc}") from exc
    if not isinstance(artifact, dict) or not isinstance(artifact.get("canaries"), dict):
        raise RequestError("OpenCode provider-live artifact has no canary mapping")
    return dict(artifact["canaries"])


def _opencode_full_column_executor(
    binary: Path,
    evidence_root: Path,
    *,
    request: dict[str, Any],
) -> tuple[dict[str, Any], tuple[semantic.SemanticAssertion, ...], tuple[str, ...]]:
    build_ref = _build_provider_build_ref(request, binary, output_root=evidence_root)
    harness_payload = run_harness(
        HarnessOptions(
            providers=("opencode",),
            scenarios=(
                *DEFAULT_HARNESS_SCENARIOS,
                "tool_call_result",
                LIVE_TOKEN_HARNESS_SCENARIO,
            ),
            evidence_root=evidence_root / "harness-evidence",
            provider_bins={"opencode": binary},
            provider_builds={"opencode": build_ref},
            qualification_request=request,
        )
    )
    managed_result = _scenario_result(
        harness_payload,
        provider="opencode",
        scenario="managed_session_e2e",
    )
    tool_result = _scenario_result(
        harness_payload,
        provider="opencode",
        scenario="tool_call_result",
    )
    live_result = _scenario_result(
        harness_payload,
        provider="opencode",
        scenario=LIVE_TOKEN_HARNESS_SCENARIO,
    )
    full_column_gate = _full_column_gate(
        harness_payload,
        provider="opencode",
        qualification_request_digest=request.get("semantic_digest"),
        interaction_evidence_class=(request.get("scenario_evidence") or {}).get("interaction_semantics"),
    )
    canaries = _opencode_server_canaries(managed_result, evidence_root=evidence_root)
    assertions = opencode_server_qualification.opencode_server_contract_oracle(canaries)

    release_gate_failures = {
        name: result.get("status")
        for name, result in {
            "full_column": full_column_gate,
            "tool_call_result": tool_result,
            LIVE_TOKEN_HARNESS_SCENARIO: live_result,
        }.items()
        if result.get("status") != "pass"
    }
    provider_gate_failed = full_column_gate.get("provider_status") == "fail"
    provider_gate_blocked = full_column_gate.get("provider_status") == "blocked"
    if release_gate_failures or provider_gate_failed:
        assertions = tuple(
            semantic.SemanticAssertion(
                assertion.assertion_id,
                AssertionOutcome.INFRASTRUCTURE_ERROR,
                assertion.evidence_class,
            )
            for assertion in assertions
        )
        status = "infrastructure_error"
    elif provider_gate_blocked:
        status = "blocked"
    elif AssertionOutcome.INFRASTRUCTURE_ERROR in {assertion.outcome for assertion in assertions}:
        status = "infrastructure_error"
    elif AssertionOutcome.SEMANTIC_FAIL in {assertion.outcome for assertion in assertions}:
        status = "fail"
    elif AssertionOutcome.BLOCKED in {assertion.outcome for assertion in assertions}:
        status = "blocked"
    else:
        status = "pass"

    secrets = tuple(value for name in ("OPENROUTER_API_KEY",) if (value := str(os.environ.get(name) or "").strip()))
    observation = {
        "status": status,
        "provider_bin": str(binary),
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        "managed_session_e2e": managed_result,
        "tool_call_result": tool_result,
        LIVE_TOKEN_HARNESS_SCENARIO: live_result,
        "release_gate_failures": release_gate_failures,
        "provider_execution_coverage_matrix_path": harness_payload.get("provider_execution_coverage_matrix_path"),
    }
    _copy_live_model_evidence(observation, live_result)
    return observation, assertions, secrets


def run_opencode_server_contract(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = provider_release_identity.load_request(
        request_path,
        provider="opencode",
        profile=opencode_server_qualification.PROFILE,
        version_grammar=opencode_server_qualification._PROFILE.version_grammar,  # noqa: SLF001
    )

    def execute(binary: Path, evidence_root: Path):
        return _opencode_full_column_executor(binary, evidence_root, request=request)

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=opencode_server_qualification._PROFILE,  # noqa: SLF001
        assertion_ids=opencode_server_qualification.ASSERTIONS,
        executor=execute,
        oracle_source=Path(semantic_oracles.__file__),
    )


def _antigravity_hook_canaries(launch_result: dict[str, Any], *, evidence_root: Path) -> dict[str, Any]:
    artifact_value = (launch_result.get("data") or {}).get("provider_live_artifact_path")
    if not isinstance(artifact_value, str) or not artifact_value.strip():
        raise RequestError("Antigravity launch_managed_session did not report its provider-live artifact")
    artifact_path = Path(artifact_value).expanduser().resolve(strict=True)
    allowed_root = (evidence_root / "harness-evidence").resolve(strict=True)
    if not artifact_path.is_relative_to(allowed_root):
        raise RequestError("Antigravity provider-live artifact escaped the harness evidence root")
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestError(f"invalid Antigravity provider-live artifact: {exc}") from exc
    if not isinstance(artifact, dict) or not isinstance(artifact.get("canaries"), dict):
        raise RequestError("Antigravity provider-live artifact has no canary mapping")
    return dict(artifact["canaries"])


def _antigravity_full_column_executor(
    binary: Path,
    evidence_root: Path,
    *,
    request: dict[str, Any],
) -> tuple[dict[str, Any], tuple[semantic.SemanticAssertion, ...], tuple[str, ...]]:
    build_ref = _build_provider_build_ref(request, binary, output_root=evidence_root)
    harness_payload = run_harness(
        HarnessOptions(
            providers=("antigravity",),
            scenarios=DEFAULT_HARNESS_SCENARIOS,
            evidence_root=evidence_root / "harness-evidence",
            provider_bins={"antigravity": binary},
            provider_builds={"antigravity": build_ref},
            qualification_request=request,
        )
    )
    launch_result = _scenario_result(
        harness_payload,
        provider="antigravity",
        scenario="launch_managed_session",
    )
    managed_result = _scenario_result(
        harness_payload,
        provider="antigravity",
        scenario="managed_session_e2e",
    )
    full_column_gate = _full_column_gate(
        harness_payload,
        provider="antigravity",
        qualification_request_digest=request.get("semantic_digest"),
        interaction_evidence_class=(request.get("scenario_evidence") or {}).get("interaction_semantics"),
    )
    canaries = _antigravity_hook_canaries(launch_result, evidence_root=evidence_root)
    assertions = antigravity_hook_qualification.antigravity_hook_inbox_oracle(canaries)

    if full_column_gate["status"] != "pass":
        assertions = tuple(
            semantic.SemanticAssertion(
                assertion.assertion_id,
                AssertionOutcome.INFRASTRUCTURE_ERROR,
                assertion.evidence_class,
            )
            for assertion in assertions
        )
        status = "infrastructure_error"
    elif AssertionOutcome.INFRASTRUCTURE_ERROR in {assertion.outcome for assertion in assertions}:
        status = "infrastructure_error"
    elif AssertionOutcome.SEMANTIC_FAIL in {assertion.outcome for assertion in assertions}:
        status = "fail"
    elif AssertionOutcome.BLOCKED in {assertion.outcome for assertion in assertions}:
        status = "blocked"
    else:
        status = "pass"

    observation = {
        "status": status,
        "provider_bin": str(binary),
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        "launch_managed_session": launch_result,
        "managed_session_e2e": managed_result,
        "producer_boundary": "unwatched_worker_required",
        "provider_execution_coverage_matrix_path": harness_payload.get("provider_execution_coverage_matrix_path"),
    }
    return observation, assertions, ()


def run_antigravity_hook_inbox(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = provider_release_identity.load_request(
        request_path,
        provider="antigravity",
        profile=antigravity_hook_qualification.PROFILE,
        version_grammar=antigravity_hook_qualification._PROFILE.version_grammar,  # noqa: SLF001
    )

    def execute(binary: Path, evidence_root: Path):
        return _antigravity_full_column_executor(binary, evidence_root, request=request)

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=antigravity_hook_qualification._PROFILE,  # noqa: SLF001
        assertion_ids=antigravity_hook_qualification.ASSERTIONS,
        executor=execute,
        oracle_source=Path(semantic_oracles.__file__),
    )


def _cursor_observed_install_executor(
    binary: Path,
    evidence_root: Path,
    *,
    request: dict[str, Any],
) -> tuple[dict[str, Any], tuple[semantic.SemanticAssertion, ...], tuple[str, ...]]:
    """Run Cursor's full universal column against the immutable install tree.

    Cursor has no public release feed that can supply a staged release asset.
    The private factory therefore acquires an explicit official install tree,
    runs Gate 0, and hands this bridge the exact tree plus its Gate 0 artifact.
    The bridge still re-materializes that tree into the public build store and
    binds every harness result to the same qualification request.
    """
    gate0_path = str(os.environ.get("LONGHOUSE_CURSOR_GATE0_ARTIFACT") or "").strip()
    gate0 = None
    if gate0_path:
        candidate = Path(gate0_path).expanduser().resolve()
        run_root = evidence_root.resolve().parents[1]
        if not candidate.is_relative_to(run_root) or candidate.name != "gate0.json":
            raise RequestError("Cursor Gate 0 artifact is outside the qualification invocation")
        try:
            gate0_payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RequestError(f"Cursor Gate 0 artifact is unreadable: {exc}") from exc
        if not isinstance(gate0_payload, dict):
            raise RequestError("Cursor Gate 0 artifact is not an object")
        gate0 = {
            "artifact_path": str(candidate.relative_to(run_root)),
            "artifact_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "status": gate0_payload.get("status"),
            "failure_code": gate0_payload.get("failure_code"),
            "native_evidence": gate0_payload.get("native_evidence"),
        }
    build_ref = _build_provider_build_ref(request, binary, output_root=evidence_root)
    harness_payload = run_harness(
        HarnessOptions(
            providers=("cursor",),
            scenarios=(*DEFAULT_HARNESS_SCENARIOS, LIVE_TOKEN_HARNESS_SCENARIO),
            evidence_root=evidence_root / "harness-evidence",
            provider_bins={"cursor": binary},
            provider_builds={"cursor": build_ref},
            qualification_request=request,
        )
    )
    full_column_gate = _full_column_gate(
        harness_payload,
        provider="cursor",
        qualification_request_digest=request.get("semantic_digest"),
        interaction_evidence_class=(request.get("scenario_evidence") or {}).get("interaction_semantics"),
    )
    live_result = _scenario_result(
        harness_payload,
        provider="cursor",
        scenario=LIVE_TOKEN_HARNESS_SCENARIO,
    )
    interaction_result = _scenario_result(
        harness_payload,
        provider="cursor",
        scenario="interaction_semantics",
    )
    gate_status = full_column_gate.get("status")
    provider_status = full_column_gate.get("provider_status")
    gate0_passed = gate0 is not None and gate0.get("status") == "passed"
    if not gate0_passed or gate_status != "pass" or provider_status in {"fail", "blocked"} or live_result.get("status") != "pass":
        assertion_outcome = (
            AssertionOutcome.BLOCKED
            if provider_status == "blocked" or live_result.get("status") == "blocked"
            else AssertionOutcome.INFRASTRUCTURE_ERROR
        )
        status = "blocked" if assertion_outcome == AssertionOutcome.BLOCKED else "infrastructure_error"
    else:
        assertion_outcome = AssertionOutcome.PASS
        status = "pass"
    interaction_evidence_class = str((request.get("scenario_evidence") or {}).get("interaction_semantics") or "live_token")
    evidence_class = {
        "hermetic": EvidenceClass.HERMETIC,
        "live_no_token": EvidenceClass.LIVE_NO_TOKEN,
        "live_token": EvidenceClass.LIVE_TOKEN,
    }.get(interaction_evidence_class, EvidenceClass.LIVE_TOKEN)
    assertion = semantic.SemanticAssertion(
        "cursor_observed_install_contract_preserved",
        assertion_outcome,
        evidence_class,
    )
    observation = {
        "status": status,
        "provider_bin": str(binary),
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        LIVE_TOKEN_HARNESS_SCENARIO: live_result,
        "provider_execution_coverage_matrix_path": harness_payload.get("provider_execution_coverage_matrix_path"),
        "cursor_gate0": gate0,
    }
    # Cursor's native stream probe is the authoritative source for model,
    # token usage, and subscription accounting. Gate 0 proves the wider
    # managed-session surface, while this envelope proves one bounded
    # model-backed request without copying a daily Cursor profile.
    _copy_live_model_evidence(observation, interaction_result)
    secrets = tuple(value for name in ("CURSOR_API_KEY",) if (value := str(os.environ.get(name) or "").strip()))
    return observation, (assertion,), secrets


def run_cursor_observed_install(request_path: Path, output_root: Path) -> dict[str, Any]:
    request = provider_release_identity.load_request(
        request_path,
        provider="cursor",
        profile=cursor_release_identity.OBSERVED_INSTALL_PROFILE,
        version_grammar=cursor_release_identity._OBSERVED_INSTALL_PROFILE.version_grammar,  # noqa: SLF001
    )

    def execute(binary: Path, evidence_root: Path):
        return _cursor_observed_install_executor(binary, evidence_root, request=request)

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=cursor_release_identity._OBSERVED_INSTALL_PROFILE,  # noqa: SLF001
        assertion_ids=cursor_release_identity.OBSERVED_INSTALL_ASSERTIONS,
        executor=execute,
        oracle_source=Path(semantic_oracles.__file__),
    )


# Deliberately only these provider/profile pairs. This is not a fallback for
# arbitrary release profiles; every admission carries provider-specific proof
# mapping and fail-closed limits.
_PROFILES = {
    ("codex", codex_tool_call_result.PROFILE): run_codex_tool_call_result,
    ("codex", codex_helm_interrupt.PROFILE): run_codex_helm_interrupt,
    ("claude", claude_real_print_qualification.PROFILE): run_claude_real_print,
    ("opencode", opencode_server_qualification.PROFILE): run_opencode_server_contract,
    ("antigravity", antigravity_hook_qualification.PROFILE): run_antigravity_hook_inbox,
    ("cursor", cursor_release_identity.OBSERVED_INSTALL_PROFILE): run_cursor_observed_install,
}


def _profile_key(request_path: Path) -> tuple[str, str]:
    try:
        payload: Any = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RequestError(f"invalid request JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RequestError("request must be an object")
    provider = payload.get("provider")
    profile = payload.get("profile")
    if not isinstance(provider, str) or not isinstance(profile, str):
        raise RequestError("provider and profile must be strings")
    return provider, profile


def run(request_path: Path, output_root: Path) -> dict[str, Any]:
    key = _profile_key(request_path)
    runner = _PROFILES.get(key)
    if runner is None:
        raise RequestError("unsupported provider/profile for the harness bridge")
    return runner(request_path, output_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.request, args.output_root)
    except RequestError as exc:
        if args.json:
            print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
