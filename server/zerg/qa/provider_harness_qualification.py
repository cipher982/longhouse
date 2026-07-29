"""Bridge Codex release qualification through the universal harness.

Phase 2's "bridge/dispatcher design" (docs/specs/provider-factory-coherence.md):
`codex_tool_call_result_v1` and `codex_helm_interrupt_v1` are the two
release-lane profiles whose strict oracles can now be satisfied by a harness
scenario instead of each profile launching its own subprocess. The tool-result
profile also owns the release lane's complete 22-scenario Codex column. This
module loads the same staged-release request the release lane's own executors
consume, derives and verifies a real `ProviderBuildRef` (unlike the release
lane's own `run()`, which trusts the request's claimed build identity without
live re-verification), and emits the identical `proof-bundle.json` shape via
each profile's `emit_proof_bundle()` finalizer.

Deliberately not a general harness bridge: only these two (provider, profile)
pairs are supported. Neither harness scenario alone produces its profile's
full assertion set — `probe_identity` runs alongside the strict scenario in
every call, supplying `exact_executable_identity_observed` (computed here,
from pre/post hash agreement, same as the release lane) and
`reported_version_matches_expected` (parsed from `probe_identity`'s reported
`--version` output).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zerg.qa import codex_helm_interrupt
from zerg.qa import codex_release_identity as identity_bridge
from zerg.qa import codex_tool_call_result
from zerg.qa.provider_build_store import ProviderBuildRef
from zerg.qa.provider_build_store import materialize_staged_provider_build
from zerg.qa.provider_factory_model import DEFAULT_HARNESS_SCENARIOS
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
    "action_matrix": ("blocked", None),
    "control_surface": ("blocked", None),
    "full_action_suite": ("blocked", "full_action_suite_has_explicit_gaps"),
    "managed_session_e2e": (
        "unsupported_gap",
        "codex_managed_bridge_credentials_missing",
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

    `codex_tool_call_result_v1` and `codex_helm_interrupt_v1` both stage the
    managed Codex package (control-plane/provider_factory/core.py:623-643:
    `uses_provider_package` is unconditionally True for both), always at
    entrypoint `bin/codex` — so `source_root`/`entrypoint_relative` are
    derivable from `provider_bin`'s own path, not free-floating unknowns. The
    derivation is validated, not trusted:
    `codex_helm_interrupt._package_identity` already checks the exact managed
    Codex package member set and raises if the layout doesn't match.

    Uses a run-scoped store under `output_root` — this is a per-run integrity
    check, not participation in control-plane's separate, already-working
    persistent build-store ingestion pipeline.
    """
    granularity = request["expected_provider_build_granularity"]
    if granularity != "full_installed_tree":
        raise RequestError(f"provider_harness_qualification only supports full_installed_tree staged builds, got {granularity!r}")
    source_root = provider_bin.parent.parent
    codex_helm_interrupt._package_identity(str(source_root), provider_bin)  # noqa: SLF001
    store_root = output_root / "provider-build-store"
    build_ref = materialize_staged_provider_build(
        provider="codex",
        version=request["expected_provider_version"],
        source_root=source_root,
        entrypoint_relative="bin/codex",
        store_root=store_root,
        closure_granularity="full_installed_tree",
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


def _full_column_gate(harness_payload: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless the complete Codex column has only known limits."""

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
            if isinstance(result, dict) and result.get("provider") == "codex" and result.get("scenario") == scenario
        ]
        for scenario in DEFAULT_HARNESS_SCENARIOS
    }
    cardinality_errors = {scenario: len(matches) for scenario, matches in by_scenario.items() if len(matches) != 1}
    unexpected_results: list[dict[str, Any]] = []
    for scenario, matches in by_scenario.items():
        if len(matches) != 1:
            continue
        result = matches[0]
        actual = (result.get("status"), result.get("failure_code"))
        expected = _EXPECTED_CODEX_FULL_COLUMN_LIMITS.get(scenario, ("pass", None))
        if actual != expected:
            unexpected_results.append(
                {
                    "scenario": scenario,
                    "expected_status": expected[0],
                    "expected_failure_code": expected[1],
                    "actual_status": actual[0],
                    "actual_failure_code": actual[1],
                }
            )

    coverage = harness_payload.get("provider_execution_coverage_matrix")
    coverage = coverage if isinstance(coverage, dict) else {}
    gap_counts = (coverage.get("provider_coverage_gap_kind_counts") or {}).get("codex", {})
    allowed_gap_kinds = {"passed", "provider_contract_unsupported"}
    unexpected_gap_kinds = {str(kind): count for kind, count in gap_counts.items() if kind not in allowed_gap_kinds and count}
    missing_actions = coverage.get("missing_provider_actions")
    coverage_complete = isinstance(missing_actions, list) and not missing_actions
    passed = not cardinality_errors and not unexpected_results and not unexpected_gap_kinds and coverage_complete
    return {
        "status": "pass" if passed else "fail",
        "failure_code": None if passed else "codex_full_column_regression",
        "expected_scenario_count": len(DEFAULT_HARNESS_SCENARIOS),
        "captured_scenario_count": sum(1 for matches in by_scenario.values() if len(matches) == 1),
        "expected_limits": {
            scenario: {"status": status, "failure_code": failure_code}
            for scenario, (status, failure_code) in sorted(_EXPECTED_CODEX_FULL_COLUMN_LIMITS.items())
        },
        "cardinality_errors": cardinality_errors,
        "unexpected_results": unexpected_results,
        "coverage_gap_kind_counts": gap_counts,
        "unexpected_coverage_gap_kinds": unexpected_gap_kinds,
        "missing_provider_actions": missing_actions,
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
            )
        )
    probe_result = _scenario_result(harness_payload, provider="codex", scenario="probe_identity")
    strict_result = _scenario_result(harness_payload, provider="codex", scenario="codex_tool_call_result_strict")
    full_column_gate = _full_column_gate(harness_payload)

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
    execution_status = (
        "completed"
        if AssertionOutcome.INFRASTRUCTURE_ERROR not in outcomes.values() and full_column_gate["status"] == "pass"
        else "infrastructure_error"
    )

    observation = {
        "provider_bin": str(provider_bin),
        "pre_execution_identity": pre_execution_identity,
        "post_execution_identity": post_execution_identity,
        "provider_build": build_ref.to_evidence(),
        "full_column_gate": full_column_gate,
        "probe_identity": probe_result,
        "codex_tool_call_result_strict": strict_result,
    }
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


# Deliberately exactly these two (provider, profile) pairs — this is not a
# general harness bridge. See module docstring and
# docs/specs/provider-factory-coherence.md, "The bridge/dispatcher design".
_PROFILES = {
    ("codex", codex_tool_call_result.PROFILE): run_codex_tool_call_result,
    ("codex", codex_helm_interrupt.PROFILE): run_codex_helm_interrupt,
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
