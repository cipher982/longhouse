"""Provider factory planning model.

Phase 1 of docs/specs/provider-factory-coherence.md: the relationships among
capability assertion, qualification profile, harness scenario, build
provenance, evidence class, and credential policy, made concrete enough to
derive "what runs" without re-deriving it by hand each time.

Two-step contract, deliberately split: `load_facts()` does all I/O (parses
the schema YAML, regex-parses the Makefile's push-CI scenario override, reads
config/provider-release-schedule.yml's weekly provider set, imports
provider_qualification._PROFILES). `plan_run(facts, provider, build_provenance,
trigger)` then performs no I/O at all — it is a pure lookup over the
`ProviderFactoryFacts` snapshot `load_facts()` produced, plus the
hand-verified constant tables below. Call `load_facts()` once per process and
reuse it; `plan_run` does not cache or re-read anything.

Read docs/specs/provider-factory-coherence.md's "Phase 1 model" section
before changing this file. Several tables below (ORPHANED_CAPABILITY_SCENARIO_IDS,
DEPLOYED_RELEASE_LANE_PROFILE, KNOWN_PRODUCIBLE_EVIDENCE_BY_ASSERTION,
CREDENTIAL_REQUIREMENT_BY_PROFILE) are facts about the codebase and clifford's
current deployment verified by hand — they are not derived from the schema
because the schema cannot express "nothing calls this," "this is what a
private repo's .env currently overrides to," or "this code path hardcodes a
False regardless of what it observes." That is precisely the
duplication-of-authority problem this epic exists to fix; Phase 2+ narrows it
further. Do not grow these tables without re-verifying by hand and updating
the spec — a first draft of this file collapsed push-CI and weekly-cron into
one trigger and mis-stated a CI-automated scenario as manual-only; both were
caught by review, not by inspection of this file alone.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
MAKEFILE_PATH = ROOT / "Makefile"
WEEKLY_SCHEDULE_PATH = ROOT / "config" / "provider-release-schedule.yml"


def _resolve_schema_path() -> Path:
    """Where schemas/managed_providers.yml actually lives, in either of the
    two environments this module runs in.

    Found live in production (2026-07-29): this repo's local/CI checkout has
    server/zerg/qa/<this file>, four levels below the repo root, so
    `ROOT = parents[3]` lands on the repo root and `ROOT / "schemas" / ...`
    is correct there. The deployed Runtime Host image is not a full repo
    checkout -- docker/runtime.dockerfile copies only server/'s *contents*
    into /app (so this file lives at /app/zerg/qa/..., one level shallower),
    which makes the exact same `parents[3]` arithmetic land on `/` by
    accident, and schemas/ was never copied there at all -- a real
    FileNotFoundError the first live call to this endpoint hit. Explicit
    candidates instead of relying on that directory-depth coincidence to
    keep meaning "the repo root" in two structurally different layouts.
    """
    candidates = _schema_path_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("schemas/managed_providers.yml not found at any known location: " + ", ".join(str(c) for c in candidates))


def _schema_path_candidates() -> tuple[Path, ...]:
    return (
        ROOT / "schemas" / "managed_providers.yml",  # local dev / CI checkout
        Path("/schemas/managed_providers.yml"),  # deployed runtime image (docker/runtime.dockerfile)
    )


class BuildProvenance(StrEnum):
    GENERATED_FAKE = "generated_fake"
    STAGED_RELEASE = "staged_release"
    OBSERVED_INSTALL = "observed_install"


# Canonical home for the weekly-cron/full-column harness scenario set.
# Previously duplicated: this module AST-parsed it out of the smoke wrapper's
# own DEFAULT_SCENARIOS constant, which is exactly the hand-duplication
# pattern the epic exists to fix. The wrapper now imports this constant
# instead of declaring its own copy (docs/specs/provider-factory-coherence.md,
# Phase 2's "assign the smoke wrapper's fate" — this is the "thin caller"
# direction for the one piece of it that was pure schema-derivable data; the
# wrapper's execution mechanics stay wrapper-owned).
DEFAULT_HARNESS_SCENARIOS: tuple[str, ...] = (
    "probe_identity",
    "adapter_conformance",
    "collect_raw_evidence",
    "action_matrix",
    "control_surface",
    "full_action_suite",
    "baseline_compare",
    "old_new_release_diff",
    "parse_ingest_project",
    "db_ingest_project",
    "session_projection",
    "timeline_projection",
    "run_prompt_once",
    "launch_managed_session",
    "send_receive",
    "pause_request_detect",
    "tail_output",
    "runtime_phase",
    "transcript_binding",
    "multi_turn_continuity",
    "crash_timeout_cleanup",
    "managed_session_e2e",
)
LIVE_TOKEN_HARNESS_SCENARIO = "live_token_streaming"


class Trigger(StrEnum):
    RELEASE_POLL = "release_poll"  # clifford factory, 900s tick, release lane
    PUSH = "push"  # GitHub Actions, every push/PR (contract-first-ci.yml)
    WEEKLY_CRON = "weekly_cron"  # GitHub Actions, weekly schedule (provider-release-weekly.yml)
    MANUAL = "manual"  # human-run, for scenarios no automated trigger can produce acceptable evidence for


ALL_PROVIDERS = ("codex", "claude", "opencode", "antigravity", "cursor")

# The release lane's actual deployed default profile per provider, as
# currently running on clifford. Verified against the live /health payload
# and control-plane/provider_factory/registry.py's PROVIDER_REGISTRY
# construction (2026-07-28). Cursor has no entry: registered_lane("cursor")
# raises ValueError, there is no cursor_lane().
DEPLOYED_RELEASE_LANE_PROFILE: dict[str, str] = {
    "codex": "codex_tool_call_result_v1",
    "claude": "claude_real_print_v1",
    "opencode": "opencode_server_contract_v1",
    "antigravity": "antigravity_hook_inbox_v1",
}

# Schema scenario_ids with zero producer of any kind, verified by hand:
# - no non-test importer of the corresponding oracle function
#   (awareness_create_assertions, directed_input_assertions,
#   unsupported_steer_assertions)
# - provider_control_oracles.py (the oracle_source for both *_steer_rejection
#   scenarios) has no caller anywhere, test or production
# - no dynamic dispatcher anywhere reads a schema oracle_source string and
#   invokes it; every reference is a hardcoded string used for manifest
#   generation or digest-hashing only
# codex_coordination_awareness_post_compaction is deliberately absent from
# this set: it has an automated CI producer (see PUSH_CODEX_COORDINATION_SCENARIO_ID
# below) — it is not orphaned, it just doesn't run on the release lane.
ORPHANED_CAPABILITY_SCENARIO_IDS: frozenset[str] = frozenset(
    {
        "codex_coordination_awareness_create",
        "codex_coordination_directed_input",
        "claude_coordination_awareness_create",
        "claude_coordination_awareness_post_compaction",
        "claude_coordination_directed_input",
        "antigravity_steer_rejection",
        "cursor_coordination_awareness_create",
        "cursor_coordination_directed_input",
        "cursor_steer_rejection",
    }
)

# contract-first-ci.yml runs `make provider-capability-coordination-proof` on
# every push/PR (job "Produce executable provider capability proof bundle"),
# which calls provider_coordination_scenarios.py's main() with
# PROVIDER_VERSION=hermetic-fixture — codex only (its argparse --provider
# choices are ("codex",)). This is CI-automated hermetic evidence, not a
# manual run: the first draft of this model mis-stated it as manual-only.
PUSH_CODEX_COORDINATION_SCENARIO_ID = "codex_coordination_awareness_post_compaction"

# What evidence class each known producer can actually generate, per
# assertion_id — hand-verified against the oracle modules (see the spec's
# Phase 1 model and its provider-by-provider notes). An empty tuple means no
# producer that exists today can ever satisfy this assertion, which is a
# stronger and more specific statement than "no producer" (see antigravity's
# real_print_injection_observed: the module refuses to run a real `agy
# --print` at all, permanently, because Antigravity has no isolated
# profile/data-root — see antigravity_hook_qualification.py's comment).
# codex_coordination_awareness_post_compaction's own two assertions split:
# the CI-automated hermetic run hardcodes
# coordination_instructions_model_visible_after_compaction to False
# (provider_coordination_scenarios.py:63) and that assertion's schema entry
# only accepts live_token anyway, so CI can never satisfy it; the sibling
# assertion no_duplicate_visible_bootstrap accepts hermetic and CI's run does
# satisfy it.
KNOWN_PRODUCIBLE_EVIDENCE_BY_ASSERTION: dict[str, tuple[str, ...]] = {
    # claude_real_print_v1 (release lane)
    "claude_cli_channel_contract_preserved": ("live_no_token",),
    "real_print_marker_returned": ("live_no_token", "live_token"),  # live_token only with explicit credentials
    # opencode_server_contract_v1 (release lane)
    "serve_session_contract_preserved": ("live_no_token",),
    "process_restart_reattach_preserved": ("live_no_token",),
    # antigravity_hook_inbox_v1 (release lane)
    "hook_inbox_contract_preserved": ("live_no_token",),
    "real_print_injection_observed": (),  # permanently blocked, see comment above
    # codex_tool_call_result_v1 (release lane)
    "exact_executable_identity_observed": ("live_no_token", "live_token"),
    "reported_version_matches_expected": ("live_no_token", "live_token"),
    "command_execution_completed_with_exact_output": ("live_token",),  # requires CODEX_API_KEY
    "tool_result_linked_to_final_agent_message": ("live_token",),  # requires CODEX_API_KEY
    # codex coordination proof (push trigger, CI, hermetic, codex-only)
    "no_duplicate_visible_bootstrap": ("hermetic",),
    "coordination_instructions_model_visible_after_compaction": (),  # hardcoded False by the hermetic producer; needs live_token
}

# Credentials each release-lane profile's v2 bridge is allowed to receive
# (control-plane/provider_factory/core.py's allowed_qualification_environment).
# Only codex is known precisely today; the other three profiles' credential
# handling lives inside their own oracle modules rather than a control-plane
# allowlist, and is not restated here.
CREDENTIAL_REQUIREMENT_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "codex_tool_call_result_v1": ("CODEX_API_KEY",),
    "codex_release_identity_v1": ("CODEX_API_KEY",),
    "codex_helm_interrupt_v1": ("CODEX_AGENTS_TOKEN", "CODEX_API_KEY", "CODEX_API_URL", "LONGHOUSE_ENGINE_BIN"),
}


@dataclass(frozen=True)
class CapabilityAssertion:
    scenario_id: str
    assertion_id: str
    provider: str
    capability: str
    oracle_source: str
    acceptable_evidence: tuple[str, ...]
    max_age_seconds: int


@dataclass(frozen=True)
class AssertionStatus:
    assertion_id: str
    scenario_id: str
    acceptable_evidence: tuple[str, ...]
    producible_evidence: tuple[str, ...]
    satisfiable: bool


@dataclass(frozen=True)
class PlanCell:
    provider: str
    build_provenance: str
    trigger: str
    status: str  # "runs" | "never_run"
    reason: str
    qualification_profile: str | None = None
    scenario_ids: tuple[str, ...] = ()
    harness_scenarios: tuple[str, ...] = ()
    credential_requirement: tuple[str, ...] = ()
    assertion_status: tuple[AssertionStatus, ...] = ()


@dataclass(frozen=True)
class ProviderFactoryFacts:
    capability_assertions: tuple[CapabilityAssertion, ...]
    default_harness_scenarios: tuple[str, ...]
    push_harness_scenarios: tuple[str, ...]
    weekly_cron_providers: tuple[str, ...]


def _load_schema() -> dict:
    schema_path = _resolve_schema_path()
    payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise SystemExit(f"{schema_path} must contain a YAML mapping with a top-level 'providers' list")
    return payload


def _iter_capabilities(node: object, prefix: str = "") -> list[tuple[str, dict]]:
    """Walk the schema's nested capability tree, yielding (dotted_key, entry)
    for every dict that declares `required_assertions`."""
    out: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if "required_assertions" in node:
            out.append((prefix, node))
        for key, value in node.items():
            if key == "required_assertions":
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            out.extend(_iter_capabilities(value, child_prefix))
    return out


def _load_capability_assertions() -> tuple[CapabilityAssertion, ...]:
    schema = _load_schema()
    out: list[CapabilityAssertion] = []
    for provider_entry in schema["providers"]:
        provider = provider_entry["provider"]
        capabilities = provider_entry.get("capabilities") or {}
        for capability, capability_entry in _iter_capabilities(capabilities):
            for assertion in capability_entry.get("required_assertions") or []:
                out.append(
                    CapabilityAssertion(
                        scenario_id=assertion["scenario_id"],
                        assertion_id=assertion["id"],
                        provider=provider,
                        capability=capability,
                        oracle_source=assertion["oracle_source"],
                        acceptable_evidence=tuple(assertion.get("acceptable_evidence") or ()),
                        max_age_seconds=int(assertion["max_age_seconds"]),
                    )
                )
    return tuple(out)


def _load_default_harness_scenarios() -> tuple[str, ...]:
    """The weekly-cron/full-column scenario set. A plain lookup now — see
    DEFAULT_HARNESS_SCENARIOS above for why this is no longer an I/O read."""
    return DEFAULT_HARNESS_SCENARIOS


def _load_push_harness_scenarios() -> tuple[str, ...]:
    """The scenario set contract-first-ci.yml's push/PR job actually runs.

    `make validate-provider-cli-canaries` -> `provider-release-proof-universal-smoke
    UNIVERSAL_SCENARIO="..."` (Makefile) is a *different, smaller* set than
    DEFAULT_SCENARIOS; conflating the two was the first bug review caught in
    this file. Parsed by regex, not imported, because it is Makefile syntax.
    """
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"provider-release-proof-universal-smoke UNIVERSAL_SCENARIO=\"([^\"]+)\"",
        text,
    )
    if match is None:
        raise SystemExit(f"push-CI UNIVERSAL_SCENARIO override not found in {MAKEFILE_PATH}")
    return tuple(match.group(1).split())


def _load_weekly_cron_providers() -> tuple[str, ...]:
    """Providers with `weekly_unconditional: true` in the release schedule.

    provider-release-weekly.yml's matrix job reads this file via
    scripts/qa/provider-release-schedule.py; parsed directly here to avoid
    depending on that script's own YAML shape assumptions twice.
    """
    payload = yaml.safe_load(WEEKLY_SCHEDULE_PATH.read_text(encoding="utf-8"))
    providers = payload.get("providers") or []
    return tuple(row["provider"] for row in providers if row.get("weekly_unconditional") is True)


def load_capability_assertions() -> tuple[CapabilityAssertion, ...]:
    """Public, narrow entry point for callers that only need the declared
    capability -> assertion mapping (schemas/managed_providers.yml), not the
    rest of `load_facts()`'s I/O (Makefile parsing, the weekly release
    schedule). Used by the live capability-projection endpoint
    (zerg/routers/provider_capability_proofs.py) -- a Runtime Host serving
    real traffic has no reason to depend on the Makefile or the weekly-cron
    schedule file being present just to answer "what does the contract
    declare," and `load_facts()`'s other three I/O calls have their own,
    separate real-environment assumptions this endpoint should not inherit
    silently."""
    return _load_capability_assertions()


def load_facts() -> ProviderFactoryFacts:
    """Perform all I/O once. Pass the result to `plan_run`."""
    return ProviderFactoryFacts(
        capability_assertions=_load_capability_assertions(),
        default_harness_scenarios=_load_default_harness_scenarios(),
        push_harness_scenarios=_load_push_harness_scenarios(),
        weekly_cron_providers=_load_weekly_cron_providers(),
    )


def _release_lane_scenario_id(provider: str, profile: str) -> str:
    """The profile's own SCENARIO_ID, read off the module that implements it.

    provider_qualification._PROFILES maps (provider, profile) -> a bound
    `run` function; each module declares its own SCENARIO_ID constant (see
    e.g. codex_tool_call_result.py). This is an identity-proof scenario_id
    and, except for the three profiles where it happens to equal a schema
    scenario_id (see the spec's Phase 1 model), is not the same vocabulary as
    capability_assertions().
    """
    from zerg.qa.provider_qualification import _PROFILES as release_lane_profiles

    run_fn = release_lane_profiles[(provider, profile)]
    module = sys.modules[run_fn.__module__]
    return module.SCENARIO_ID


def _assertion_statuses(assertions: tuple[CapabilityAssertion, ...]) -> tuple[AssertionStatus, ...]:
    out = []
    for assertion in assertions:
        producible = KNOWN_PRODUCIBLE_EVIDENCE_BY_ASSERTION.get(assertion.assertion_id, ())
        satisfiable = bool(set(assertion.acceptable_evidence) & set(producible))
        out.append(
            AssertionStatus(
                assertion_id=assertion.assertion_id,
                scenario_id=assertion.scenario_id,
                acceptable_evidence=assertion.acceptable_evidence,
                producible_evidence=producible,
                satisfiable=satisfiable,
            )
        )
    return tuple(out)


def plan_run(facts: ProviderFactoryFacts, provider: str, build_provenance: str, trigger: str) -> PlanCell:
    if provider not in ALL_PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    BuildProvenance(build_provenance)  # raises ValueError if not a recognized value
    Trigger(trigger)

    if trigger == Trigger.RELEASE_POLL:
        if build_provenance != BuildProvenance.STAGED_RELEASE:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason="the release lane only runs against staged upstream releases",
            )
        profile = DEPLOYED_RELEASE_LANE_PROFILE.get(provider)
        if profile is None:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason=f"{provider} has no registered release lane (no *_lane() in provider_factory/registry.py)",
            )
        scenario_id = _release_lane_scenario_id(provider, profile)
        relevant_assertions = tuple(a for a in facts.capability_assertions if a.provider == provider and a.scenario_id == scenario_id)
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="runs",
            reason=f"deployed release-lane default for {provider}",
            qualification_profile=profile,
            scenario_ids=(scenario_id,),
            credential_requirement=CREDENTIAL_REQUIREMENT_BY_PROFILE.get(profile, ()),
            assertion_status=_assertion_statuses(relevant_assertions),
        )

    if trigger == Trigger.PUSH:
        if build_provenance != BuildProvenance.GENERATED_FAKE:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason="push CI only runs against generated fake binaries",
            )
        scenario_ids = (PUSH_CODEX_COORDINATION_SCENARIO_ID,) if provider == "codex" else ()
        relevant_assertions = tuple(a for a in facts.capability_assertions if a.provider == provider and a.scenario_id in scenario_ids)
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="runs",
            reason="push-CI harness smoke (validate-provider-cli-canaries), plus the codex coordination proof job if applicable",
            harness_scenarios=facts.push_harness_scenarios,
            scenario_ids=scenario_ids,
            assertion_status=_assertion_statuses(relevant_assertions),
        )

    if trigger == Trigger.WEEKLY_CRON:
        if build_provenance != BuildProvenance.GENERATED_FAKE:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason="the weekly matrix only runs against generated fake binaries (scheduled_evidence: generated_fake_unconditional_full_column)",
            )
        if provider not in facts.weekly_cron_providers:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason=f"{provider} is not weekly_unconditional in config/provider-release-schedule.yml",
            )
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="runs",
            reason="weekly full-column smoke (provider-release-weekly.yml, DEFAULT_SCENARIOS)",
            harness_scenarios=facts.default_harness_scenarios,
        )

    # Trigger.MANUAL: a human runs a specific capability-proof scenario by
    # hand. Only assertions no automated trigger can satisfy are meaningfully
    # "manual" — everything else already has a producer above.
    manual_assertions = tuple(
        a
        for a in facts.capability_assertions
        if a.provider == provider
        and a.scenario_id not in ORPHANED_CAPABILITY_SCENARIO_IDS
        and not any(status.satisfiable for status in _assertion_statuses((a,)) if status.assertion_id == a.assertion_id)
    )
    orphaned_for_provider = tuple(
        a.scenario_id for a in facts.capability_assertions if a.provider == provider and a.scenario_id in ORPHANED_CAPABILITY_SCENARIO_IDS
    )
    if not manual_assertions and not orphaned_for_provider:
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="never_run",
            reason=f"every capability-proof assertion declared for {provider} is already satisfiable by an automated trigger",
        )
    if not manual_assertions:
        # Only orphaned scenario_ids remain: no oracle input producer exists
        # at all, manual or otherwise. A human could theoretically write one,
        # but nothing in the codebase does today.
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="never_run",
            reason=f"remaining capability-proof scenario_ids for {provider} are orphaned: {sorted(set(orphaned_for_provider))}",
        )
    return PlanCell(
        provider=provider,
        build_provenance=build_provenance,
        trigger=trigger,
        status="runs",
        reason="has at least one capability-proof assertion no automated trigger can satisfy",
        scenario_ids=tuple(sorted({a.scenario_id for a in manual_assertions})),
        assertion_status=_assertion_statuses(manual_assertions),
    )
