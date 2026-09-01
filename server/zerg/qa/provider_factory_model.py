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
before changing this file. Two tables below (DEPLOYED_RELEASE_LANE_PROFILE,
CREDENTIAL_REQUIREMENT_BY_PROFILE) are facts about clifford's current
deployment verified by hand — they are not derived because the schema cannot
express "this is what a private repo's .env currently overrides to." Do not
grow them without re-verifying by hand and updating the spec — a first draft
of this file collapsed push-CI and weekly-cron into one trigger and mis-stated
a CI-automated scenario as manual-only; both were caught by review, not by
inspection of this file alone.

"Which assertions have a producer" and "which scenario_ids are orphaned" used
to be hand-maintained here too, and both went stale: the generated plan
artifact published false coverage gaps for assertions whose producers had
already shipped. They are now derived from the ProducerRegistration each
producer exports beside its own executable — see PRODUCER_MODULES.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from zerg.qa.codex_native_resume import REGISTRATION as CODEX_NATIVE_RESUME_PRODUCER
from zerg.qa.provider_resume_factory import SCENARIOS as PROVIDER_RESUME_SCENARIOS
from zerg.qa.resume_assurance import ProducerRegistration

# The declared-contract slice (schema resolution, CapabilityAssertion, the
# assertion loader) lives in zerg/services/ because the served capability
# endpoint needs it and zerg/qa/ is excluded from the published wheel. Only
# the public surface is re-exported here, so the factory keeps one import
# surface; anything reaching for the schema-resolution internals should
# import zerg.services.provider_capability_schema directly rather than
# monkeypatching a name that is only an alias of another module's global.
from zerg.services.provider_capability_schema import CapabilityAssertion
from zerg.services.provider_capability_schema import _load_capability_assertions
from zerg.services.provider_capability_schema import load_capability_assertions  # noqa: F401

ROOT = Path(__file__).resolve().parents[3]
MAKEFILE_PATH = ROOT / "Makefile"
WEEKLY_SCHEDULE_PATH = ROOT / "config" / "provider-release-schedule.yml"


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
    "baseline_compare",
    "old_new_release_diff",
    "full_action_suite",
    "parse_ingest_project",
    "interaction_semantics",
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
    *PROVIDER_RESUME_SCENARIOS,
)
LIVE_TOKEN_HARNESS_SCENARIO = "live_token_streaming"

# Release profiles whose deployed dispatcher executes the universal full
# column in addition to the profile-specific strict scenario. Keep this keyed
# by profile rather than provider so adding a provider profile cannot silently
# broaden release behavior.
FULL_COLUMN_RELEASE_PROFILES = frozenset(
    {
        "codex_tool_call_result_v1",
        "claude_real_print_v1",
        "opencode_server_contract_v1",
        "antigravity_hook_inbox_v1",
        "cursor_observed_install_v1",
        "cursor_observed_install_grok_v1",
        "pi_print_v1",
    }
)


class Trigger(StrEnum):
    RELEASE_POLL = "release_poll"  # clifford factory, 900s tick, release lane
    PUSH = "push"  # GitHub Actions, every push/PR (contract-first-ci.yml)
    WEEKLY_CRON = "weekly_cron"  # GitHub Actions, weekly schedule (provider-release-weekly.yml)
    MANUAL = "manual"  # human-run, for scenarios no automated trigger can produce acceptable evidence for


# Compatibility view for the descriptive factory model.  The registration is
# authored beside the executable producer; this module does not restate it.
DIRECT_RESUME_PRODUCERS: tuple[ProducerRegistration, ...] = (CODEX_NATIVE_RESUME_PRODUCER,)


ALL_PROVIDERS = ("codex", "claude", "opencode", "antigravity", "cursor", "pi")

# The release lane's actual deployed profiles per provider. Codex deliberately
# has two profiles: the tool-result lane owns the complete universal column,
# while the Helm lane supplies the strict managed-interrupt proof. The singular
# mapping remains the primary/default profile for older generated consumers.
DEPLOYED_RELEASE_LANE_PROFILES: dict[str, tuple[str, ...]] = {
    "codex": ("codex_tool_call_result_v1", "codex_helm_interrupt_v1"),
    "claude": ("claude_real_print_v1",),
    "opencode": ("opencode_server_contract_v1",),
    "antigravity": ("antigravity_hook_inbox_v1",),
    "cursor": ("cursor_observed_install_v1", "cursor_observed_install_grok_v1"),
    "pi": ("pi_print_v1",),
}
DEPLOYED_RELEASE_LANE_PROFILE: dict[str, str] = {provider: profiles[0] for provider, profiles in DEPLOYED_RELEASE_LANE_PROFILES.items()}

# The two *_steer_rejection scenarios were removed on 2026-07-31 along with the
# `session.input.steer_active` capability cells that required them: the cell
# restated a fact the schema already carries in `steer_active_turn` and
# `operation_evidence`, and its oracle
# (server/zerg/qa/provider_control_oracles.py) had no caller anywhere, test or
# production. An assertion nothing can produce is not a gap in coverage, it is
# a claim with no possible evidence.
#
# Both of these facts used to be hand-maintained tables here. They are now
# derived from the ProducerRegistration each executable producer exports
# beside itself (see PRODUCER_MODULES below), because the hand-maintained
# versions went stale and the generated plan artifact published the staleness
# as false coverage gaps: every one of the seven scenario_ids the old
# ORPHANED_CAPABILITY_SCENARIO_IDS listed had acquired a registered producer,
# and the old evidence table still reported "no registered evidence producer"
# for assertions whose producers had shipped.

# The executable producers that export a ProducerRegistration. Kept as an
# explicit tuple rather than a package scan so importing this module stays
# cheap and one test (test_producer_modules_covers_every_registration) can
# hold it to the directory by AST scan — the private factory's own
# PRODUCER_MODULES desynced silently once for exactly this reason.
PRODUCER_MODULES: tuple[str, ...] = (
    "zerg.qa.antigravity_launch_hook_inbox",
    "zerg.qa.antigravity_resume_policy",
    "zerg.qa.claude_coordination_awareness_create",
    "zerg.qa.claude_coordination_awareness_post_compaction",
    "zerg.qa.claude_coordination_directed_input",
    "zerg.qa.claude_launch_helm_real_print",
    "zerg.qa.claude_native_resume",
    "zerg.qa.claude_turn_boundary_quiescent",
    "zerg.qa.claude_turn_start_real_print",
    "zerg.qa.codex_coordination_native",
    "zerg.qa.codex_helm_launch_visibility",
    "zerg.qa.codex_native_resume",
    "zerg.qa.codex_turn_boundary_native",
    "zerg.qa.console_served_state",
    "zerg.qa.cursor_coordination_producer",
    "zerg.qa.cursor_native_resume",
    "zerg.qa.cursor_turn_boundary_producer",
    "zerg.qa.ios_workspace_selection_source_producer",
    "zerg.qa.opencode_native_resume",
    "zerg.qa.opencode_server_contract_producer",
    "zerg.qa.opencode_turn_boundary_quiescent",
    "zerg.qa.product_console_lifecycle",
    "zerg.qa.provider_console_lifecycle",
    "zerg.qa.provider_generic_resume",
    "zerg.qa.title_dependency_live_producer",
    "zerg.qa.title_dependency_recovery_producer",
    "zerg.qa.workspace_suggestions_live_producer",
)

# contract-first-ci.yml runs `make provider-capability-coordination-proof` on
# every push/PR (job "Produce executable provider capability proof bundle"),
# which calls provider_coordination_scenarios.py's main() with
# PROVIDER_VERSION=hermetic-fixture — codex only (its argparse --provider
# choices are ("codex",)). This is CI-automated hermetic evidence, not a
# manual run: the first draft of this model mis-stated it as manual-only.
PUSH_CODEX_COORDINATION_SCENARIO_ID = "codex_coordination_awareness_post_compaction"

# The one evidence fact no ProducerRegistration can express: the CI make
# target above is a Makefile lane, not a registered producer, and the hermetic
# bundle it emits satisfies no_duplicate_visible_bootstrap (whose schema entry
# accepts hermetic). Its sibling assertion
# coordination_instructions_model_visible_after_compaction is deliberately not
# listed: that CI run hardcodes it to False
# (provider_coordination_scenarios.py:63), and the registered live_token
# producers are what actually satisfy it.
# Union'd into the derived map; every key is checked against the schema at
# load_facts() time so it cannot rot the way the table it replaced did.
NON_REGISTERED_PRODUCIBLE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "no_duplicate_visible_bootstrap": ("hermetic",),
}


def _producer_registrations() -> tuple[ProducerRegistration, ...]:
    """Import each producer module and collect the registration it exports."""
    from importlib import import_module

    out = []
    for module_name in PRODUCER_MODULES:
        registration = getattr(import_module(module_name), "REGISTRATION", None)
        if registration is None:
            raise SystemExit(f"{module_name} is listed in PRODUCER_MODULES but exports no REGISTRATION")
        out.append(registration)
    return tuple(out)


def _derive_producible_evidence(
    registrations: tuple[ProducerRegistration, ...],
    assertion_ids: frozenset[str],
) -> dict[str, tuple[str, ...]]:
    """assertion_id -> every evidence class some registered producer emits."""
    derived: dict[str, set[str]] = {}
    for registration in registrations:
        for assertion_id, _variant in registration.assertion_cells:
            derived.setdefault(assertion_id, set()).update(registration.evidence_classes)
    unknown = sorted(set(NON_REGISTERED_PRODUCIBLE_EVIDENCE) - assertion_ids)
    if unknown:
        raise SystemExit(f"NON_REGISTERED_PRODUCIBLE_EVIDENCE names assertions the schema does not declare: {unknown}")
    for assertion_id, evidence in NON_REGISTERED_PRODUCIBLE_EVIDENCE.items():
        derived.setdefault(assertion_id, set()).update(evidence)
    return {assertion_id: tuple(sorted(evidence)) for assertion_id, evidence in derived.items()}


def _derive_orphaned_scenario_ids(
    registrations: tuple[ProducerRegistration, ...],
    assertions: tuple[CapabilityAssertion, ...],
) -> frozenset[str]:
    """Schema scenario_ids no registered producer covers.

    A producer covers its own `scenario_id` plus, for the provider-neutral
    ones, every id in `scenario_ids`.
    """
    produced: set[str] = set()
    for registration in registrations:
        produced.add(registration.scenario_id)
        produced.update(registration.scenario_ids)
    return frozenset({assertion.scenario_id for assertion in assertions} - produced)


CREDENTIAL_REQUIREMENT_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "codex_tool_call_result_v1": ("CODEX_API_KEY",),
    "codex_release_identity_v1": ("CODEX_API_KEY",),
    "codex_helm_interrupt_v1": ("CODEX_AGENTS_TOKEN", "CODEX_API_KEY", "CODEX_API_URL", "LONGHOUSE_ENGINE_BIN"),
    "claude_real_print_v1": ("ANTHROPIC_API_KEY", "LONGHOUSE_CLAUDE_QUALIFICATION_LIVE", "LONGHOUSE_ENGINE_BIN"),
    "opencode_server_contract_v1": ("OPENROUTER_API_KEY",),
    "cursor_observed_install_v1": ("CURSOR_API_KEY", "CURSOR_MODEL", "LONGHOUSE_CLI_BIN", "LONGHOUSE_ENGINE_BIN"),
    "cursor_observed_install_grok_v1": ("CURSOR_API_KEY", "CURSOR_MODEL", "LONGHOUSE_CLI_BIN", "LONGHOUSE_ENGINE_BIN"),
    "pi_print_v1": ("OPENROUTER_API_KEY", "LONGHOUSE_PI_LIVE", "LONGHOUSE_PI_QUALIFICATION_MODEL"),
}


@dataclass(frozen=True)
class AssertionStatus:
    assertion_id: str
    variant: str | None
    scenario_id: str
    minimum_scenario_revision: int
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
    qualification_profiles: tuple[str, ...] = ()
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
    # Derived from the producer registrations, not hand-maintained. Carried on
    # the snapshot so `plan_run` keeps its no-I/O contract: importing the
    # producer modules is I/O, and it happens once, in `load_facts`.
    producible_evidence_by_assertion: Mapping[str, tuple[str, ...]]
    orphaned_scenario_ids: frozenset[str]


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


def load_facts() -> ProviderFactoryFacts:
    """Perform all I/O once. Pass the result to `plan_run`."""
    capability_assertions = _load_capability_assertions()
    registrations = _producer_registrations()
    return ProviderFactoryFacts(
        capability_assertions=capability_assertions,
        default_harness_scenarios=_load_default_harness_scenarios(),
        push_harness_scenarios=_load_push_harness_scenarios(),
        weekly_cron_providers=_load_weekly_cron_providers(),
        producible_evidence_by_assertion=_derive_producible_evidence(
            registrations,
            frozenset(assertion.assertion_id for assertion in capability_assertions),
        ),
        orphaned_scenario_ids=_derive_orphaned_scenario_ids(registrations, capability_assertions),
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
    scenario_id = getattr(run_fn, "SCENARIO_ID", None)
    if isinstance(scenario_id, str):
        return scenario_id
    module = sys.modules[run_fn.__module__]
    return module.SCENARIO_ID


def _assertion_statuses(facts: ProviderFactoryFacts, assertions: tuple[CapabilityAssertion, ...]) -> tuple[AssertionStatus, ...]:
    out = []
    for assertion in assertions:
        producible = facts.producible_evidence_by_assertion.get(assertion.assertion_id, ())
        satisfiable = bool(set(assertion.acceptable_evidence) & set(producible))
        out.append(
            AssertionStatus(
                assertion_id=assertion.assertion_id,
                variant=assertion.variant,
                scenario_id=assertion.scenario_id,
                minimum_scenario_revision=assertion.minimum_scenario_revision,
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
        expected_provenance = BuildProvenance.OBSERVED_INSTALL if provider in {"cursor", "pi"} else BuildProvenance.STAGED_RELEASE
        if build_provenance != expected_provenance:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason=(
                    "Cursor's release lane only runs against its pinned observed install"
                    if provider == "cursor"
                    else (
                        "Pi's release lane only runs against its pinned observed install"
                        if provider == "pi"
                        else "the release lane only runs against staged upstream releases"
                    )
                ),
            )
        profiles = DEPLOYED_RELEASE_LANE_PROFILES.get(provider)
        if profiles is None:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason=f"{provider} has no registered release lane (no *_lane() in provider_factory/registry.py)",
            )
        scenario_ids = tuple(_release_lane_scenario_id(provider, profile) for profile in profiles)
        runs_full_column = any(profile in FULL_COLUMN_RELEASE_PROFILES for profile in profiles)
        produced_scenarios = set(scenario_ids)
        if runs_full_column:
            produced_scenarios.update(facts.default_harness_scenarios)
        relevant_assertions = tuple(
            a for a in facts.capability_assertions if a.provider == provider and a.scenario_id in produced_scenarios
        )
        credential_requirement = tuple(
            dict.fromkeys(requirement for profile in profiles for requirement in CREDENTIAL_REQUIREMENT_BY_PROFILE.get(profile, ()))
        )
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="runs",
            reason=f"deployed release-lane profiles for {provider}",
            qualification_profile=profiles[0],
            qualification_profiles=profiles,
            scenario_ids=scenario_ids,
            harness_scenarios=(facts.default_harness_scenarios if runs_full_column else ()),
            credential_requirement=credential_requirement,
            assertion_status=_assertion_statuses(facts, relevant_assertions),
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
            assertion_status=_assertion_statuses(facts, relevant_assertions),
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
            assertion_status=_assertion_statuses(
                facts,
                tuple(
                    assertion
                    for assertion in facts.capability_assertions
                    if assertion.provider == provider and assertion.scenario_id in facts.default_harness_scenarios
                ),
            ),
        )

    # Cursor has no upstream release feed. Its real-binary lane is an exact,
    # explicit snapshot of the observed install plus a matching live Gate 0
    # artifact; it still runs the same complete universal column.
    if provider == "cursor":
        if build_provenance != BuildProvenance.OBSERVED_INSTALL:
            return PlanCell(
                provider=provider,
                build_provenance=build_provenance,
                trigger=trigger,
                status="never_run",
                reason="Cursor manual qualification only runs against an explicit observed install",
            )
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="runs",
            reason="exact observed-install snapshot with matching live Cursor Gate 0 evidence",
            harness_scenarios=facts.default_harness_scenarios,
            assertion_status=_assertion_statuses(
                facts,
                tuple(
                    assertion
                    for assertion in facts.capability_assertions
                    if assertion.provider == provider and assertion.scenario_id in facts.default_harness_scenarios
                ),
            ),
        )

    # Trigger.MANUAL: a human runs a specific capability-proof scenario by
    # hand. Only assertions no automated trigger can satisfy are meaningfully
    # "manual" — everything else already has a producer above.
    if build_provenance != BuildProvenance.OBSERVED_INSTALL:
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="never_run",
            reason="manual qualification requires an explicit observed provider install",
        )
    manual_assertions = tuple(
        a
        for a in facts.capability_assertions
        if a.provider == provider
        and a.scenario_id not in facts.orphaned_scenario_ids
        and not any(status.satisfiable for status in _assertion_statuses(facts, (a,)) if status.assertion_id == a.assertion_id)
    )
    orphaned_for_provider = tuple(
        a.scenario_id for a in facts.capability_assertions if a.provider == provider and a.scenario_id in facts.orphaned_scenario_ids
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
    manual_statuses = _assertion_statuses(facts, manual_assertions)
    if not any(status.satisfiable for status in manual_statuses):
        return PlanCell(
            provider=provider,
            build_provenance=build_provenance,
            trigger=trigger,
            status="never_run",
            reason=(
                f"remaining capability-proof assertions for {provider} have no registered evidence producer: "
                f"{sorted(status.assertion_id + (':' + status.variant if status.variant else '') for status in manual_statuses)}"
            ),
            scenario_ids=tuple(sorted({a.scenario_id for a in manual_assertions})),
            assertion_status=manual_statuses,
        )
    return PlanCell(
        provider=provider,
        build_provenance=build_provenance,
        trigger=trigger,
        status="runs",
        reason="has at least one capability-proof assertion no automated trigger can satisfy",
        scenario_ids=tuple(sorted({a.scenario_id for a in manual_assertions})),
        assertion_status=manual_statuses,
    )
