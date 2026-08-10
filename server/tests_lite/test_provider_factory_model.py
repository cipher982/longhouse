import pytest

from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import DEPLOYED_RELEASE_LANE_PROFILE
from zerg.qa.provider_factory_model import DEPLOYED_RELEASE_LANE_PROFILES
from zerg.qa.provider_factory_model import KNOWN_PRODUCIBLE_EVIDENCE_BY_ASSERTION
from zerg.qa.provider_factory_model import ORPHANED_CAPABILITY_SCENARIO_IDS
from zerg.qa.provider_factory_model import PUSH_CODEX_COORDINATION_SCENARIO_ID
from zerg.qa.provider_factory_model import load_capability_assertions
from zerg.qa.provider_factory_model import load_facts
from zerg.qa.provider_factory_model import plan_run


@pytest.fixture(scope="module")
def facts():
    return load_facts()


def test_capability_assertions_match_schema_scenario_count(facts) -> None:
    # 13 -> 11 scenarios and 21 -> 19 assertions on 2026-07-31: the two
    # `session.input.steer_active` capability cells were removed. They restated
    # a fact `steer_active_turn` and `operation_evidence` already carry, and
    # their oracle had no caller anywhere, so the assertions could never be
    # satisfied or refuted.
    # 11 -> 16 scenarios and 19 -> 24 assertions on 2026-08-01: every provider
    # gained a `session.activity.turn_boundary` cell. None of the five has an
    # automated producer yet, so they all surface in the manual lane rather than
    # silently reading as covered.
    # 16 -> 25 scenarios and 24 -> 61 assertions on 2026-08-02: ended-Helm
    # Resume added eight supported-provider invariants plus one typed
    # unsupported-provider invariant across the five provider columns.
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert len(scenario_ids) == 25
    # 61 -> 65 on 2026-08-03: native Resume split into clean-exit and
    # process-loss variants for each supported provider.
    assert len(facts.capability_assertions) == 65


def test_orphaned_scenario_ids_are_a_subset_of_schema_scenario_ids(facts) -> None:
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert ORPHANED_CAPABILITY_SCENARIO_IDS <= scenario_ids
    # codex_coordination_awareness_post_compaction has a CI producer; it must
    # not be classified as orphaned even though it isn't release-lane automated.
    assert PUSH_CODEX_COORDINATION_SCENARIO_ID not in ORPHANED_CAPABILITY_SCENARIO_IDS


def test_default_harness_scenarios_has_32_entries(facts) -> None:
    assert len(facts.default_harness_scenarios) == 32
    assert "probe_identity" in facts.default_harness_scenarios
    assert "managed_session_e2e" in facts.default_harness_scenarios
    assert "interaction_semantics" in facts.default_harness_scenarios


def test_push_harness_scenarios_is_the_smaller_ci_set(facts) -> None:
    # Push CI (validate-provider-cli-canaries) runs a 4-scenario subset, not
    # DEFAULT_SCENARIOS — conflating the two was a bug in the first draft of
    # this model, caught by review.
    assert facts.push_harness_scenarios == (
        "adapter_conformance",
        "action_matrix",
        "control_surface",
        "old_new_release_diff",
    )
    assert set(facts.push_harness_scenarios) < set(facts.default_harness_scenarios)


def test_weekly_cron_providers_from_schedule_config(facts) -> None:
    assert set(facts.weekly_cron_providers) == set(ALL_PROVIDERS) - {"antigravity"}


@pytest.mark.parametrize("provider", ["codex", "claude", "opencode"])
def test_release_poll_runs_the_deployed_profile_for_automated_providers(facts, provider: str) -> None:
    cell = plan_run(facts, provider, "staged_release", "release_poll")
    assert cell.status == "runs"
    assert cell.qualification_profile == DEPLOYED_RELEASE_LANE_PROFILE[provider]
    assert cell.qualification_profiles == DEPLOYED_RELEASE_LANE_PROFILES[provider]
    assert cell.scenario_ids


def test_release_poll_never_runs_for_cursor_staged_release(facts) -> None:
    cell = plan_run(facts, "cursor", "staged_release", "release_poll")
    assert cell.status == "never_run"
    assert "observed install" in cell.reason


def test_release_poll_never_runs_for_pi_staged_release(facts) -> None:
    cell = plan_run(facts, "pi", "staged_release", "release_poll")
    assert cell.status == "never_run"
    assert "observed install" in cell.reason


def test_release_poll_runs_for_cursor_observed_install(facts) -> None:
    cell = plan_run(facts, "cursor", "observed_install", "release_poll")
    assert cell.status == "runs"
    assert cell.qualification_profile == "cursor_observed_install_v1"
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.credential_requirement == (
        "CURSOR_API_KEY",
        "CURSOR_MODEL",
        "LONGHOUSE_CLI_BIN",
        "LONGHOUSE_ENGINE_BIN",
    )


def test_release_poll_runs_for_pi_observed_install(facts) -> None:
    cell = plan_run(facts, "pi", "observed_install", "release_poll")
    assert cell.status == "runs"
    assert cell.qualification_profile == "pi_print_v1"
    assert cell.qualification_profiles == DEPLOYED_RELEASE_LANE_PROFILES["pi"]
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.scenario_ids == ("pi_print",)
    assert cell.credential_requirement == (
        "OPENROUTER_API_KEY",
        "LONGHOUSE_PI_LIVE",
        "LONGHOUSE_PI_QUALIFICATION_MODEL",
    )


def test_release_poll_never_runs_against_generated_fake_provenance(facts) -> None:
    for provider in DEPLOYED_RELEASE_LANE_PROFILE:
        cell = plan_run(facts, provider, "generated_fake", "release_poll")
        assert cell.status == "never_run"


def test_codex_release_poll_runs_both_profiles_and_the_full_staged_column(facts) -> None:
    cell = plan_run(facts, "codex", "staged_release", "release_poll")
    assert cell.qualification_profiles == (
        "codex_tool_call_result_v1",
        "codex_helm_interrupt_v1",
    )
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.credential_requirement == (
        "CODEX_API_KEY",
        "CODEX_AGENTS_TOKEN",
        "CODEX_API_URL",
        "LONGHOUSE_ENGINE_BIN",
    )


def test_claude_release_poll_runs_the_full_staged_column(facts) -> None:
    cell = plan_run(facts, "claude", "staged_release", "release_poll")

    assert cell.qualification_profiles == ("claude_real_print_v1",)
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.credential_requirement == (
        "ANTHROPIC_API_KEY",
        "LONGHOUSE_CLAUDE_QUALIFICATION_LIVE",
        "LONGHOUSE_ENGINE_BIN",
    )


def test_opencode_release_poll_runs_the_full_staged_column(facts) -> None:
    cell = plan_run(facts, "opencode", "staged_release", "release_poll")

    assert cell.qualification_profiles == ("opencode_server_contract_v1",)
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.credential_requirement == ("OPENROUTER_API_KEY",)


def test_antigravity_release_poll_is_manual_maintenance_only(facts) -> None:
    cell = plan_run(facts, "antigravity", "staged_release", "release_poll")

    assert cell.status == "never_run"
    assert "maintenance-tier" in cell.reason


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_push_runs_the_ci_scenario_set_for_every_provider(facts, provider: str) -> None:
    cell = plan_run(facts, provider, "generated_fake", "push")
    assert cell.status == "runs"
    assert cell.harness_scenarios == facts.push_harness_scenarios


def test_push_never_runs_against_staged_release_provenance(facts) -> None:
    cell = plan_run(facts, "codex", "staged_release", "push")
    assert cell.status == "never_run"


def test_push_runs_the_codex_coordination_proof_scenario_only_for_codex(facts) -> None:
    codex_cell = plan_run(facts, "codex", "generated_fake", "push")
    assert codex_cell.scenario_ids == (PUSH_CODEX_COORDINATION_SCENARIO_ID,)
    for provider in ("claude", "opencode", "antigravity", "cursor"):
        cell = plan_run(facts, provider, "generated_fake", "push")
        assert cell.scenario_ids == ()


def test_push_codex_coordination_proof_cannot_satisfy_its_live_token_assertion(facts) -> None:
    # provider_coordination_scenarios.py hardcodes
    # coordination_instructions_model_visible_after_compaction to False and
    # produces hermetic evidence; that assertion's schema entry only accepts
    # live_token, so CI can never satisfy it even though it always runs.
    cell = plan_run(facts, "codex", "generated_fake", "push")
    statuses = {s.assertion_id: s for s in cell.assertion_status}
    assert statuses["no_duplicate_visible_bootstrap"].satisfiable is True
    assert statuses["coordination_instructions_model_visible_after_compaction"].satisfiable is False


@pytest.mark.parametrize("provider", ["codex", "claude", "opencode", "cursor"])
def test_weekly_cron_runs_the_full_default_scenario_set_for_scheduled_providers(facts, provider: str) -> None:
    cell = plan_run(facts, provider, "generated_fake", "weekly_cron")
    assert cell.status == "runs"
    assert cell.harness_scenarios == facts.default_harness_scenarios
    resume_statuses = [
        status
        for status in cell.assertion_status
        if status.scenario_id
        in {
            "helm_cold_resume",
            "helm_live_reattach",
            "console_thread_continue",
            "resume_identity_continuity",
            "resume_attempt_idempotency",
            "resume_single_owner",
            "resume_input_safety",
            "resume_failure_cleanup",
            "resume_unsupported",
        }
    ]
    assert resume_statuses
    native = [status for status in resume_statuses if status.assertion_id == "native_provider_resume_proven"]
    assert len(native) == 2
    assert {status.variant for status in native} == {"clean_exit", "process_loss"}
    assert all(status.minimum_scenario_revision == 2 for status in native)
    assert all(not status.satisfiable for status in native)
    assert all(status.satisfiable for status in resume_statuses if status.assertion_id != "native_provider_resume_proven")


def test_weekly_cron_does_not_run_antigravity_maintenance_lane(facts) -> None:
    cell = plan_run(facts, "antigravity", "generated_fake", "weekly_cron")
    assert cell.status == "never_run"
    assert "not weekly_unconditional" in cell.reason


def test_weekly_cron_never_runs_against_staged_release_provenance(facts) -> None:
    cell = plan_run(facts, "codex", "staged_release", "weekly_cron")
    assert cell.status == "never_run"


def test_manual_trigger_runs_full_column_for_cursor_observed_install(facts) -> None:
    cell = plan_run(facts, "cursor", "observed_install", "manual")
    assert cell.status == "runs"
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert "Gate 0" in cell.reason


def test_manual_trigger_never_runs_for_codex_without_an_evidence_producer(facts) -> None:
    # coordination_instructions_model_visible_after_compaction needs a real
    # manual run even though the scenario_id also has an automated CI producer.
    cell = plan_run(facts, "codex", "observed_install", "manual")
    assert cell.status == "never_run"
    assert "no registered evidence producer" in cell.reason
    assert "helm_cold_resume" in cell.scenario_ids


@pytest.mark.parametrize("provider", ("codex", "claude", "opencode", "cursor"))
def test_native_resume_has_no_legacy_eligible_evidence_producer(facts, provider: str) -> None:
    native = [
        assertion
        for assertion in facts.capability_assertions
        if assertion.provider == provider and assertion.assertion_id == "native_provider_resume_proven"
    ]

    assert {assertion.variant for assertion in native} == {"clean_exit", "process_loss"}
    assert all(assertion.acceptable_evidence == ("live_token",) for assertion in native)
    assert all(assertion.minimum_scenario_revision == 2 for assertion in native)
    assert "native_provider_resume_proven" not in KNOWN_PRODUCIBLE_EVIDENCE_BY_ASSERTION


def test_codex_direct_resume_registration_is_authored_beside_executable() -> None:
    from zerg.qa.provider_factory_model import DIRECT_RESUME_PRODUCERS

    assert len(DIRECT_RESUME_PRODUCERS) == 1
    producer = DIRECT_RESUME_PRODUCERS[0]
    assert producer.providers == ("codex",)
    assert producer.assertion_cells == (
        ("native_provider_resume_proven", "clean_exit"),
        ("native_provider_resume_proven", "process_loss"),
    )
    assert producer.evidence_classes == ("live_token",)
    assert producer.scenario_revision == 4
    assert producer.executable is True
    assert producer.executable_module == "zerg.qa.codex_native_resume"


def test_manual_trigger_never_runs_for_antigravity_without_an_evidence_producer(facts) -> None:
    cell = plan_run(facts, "antigravity", "observed_install", "manual")
    assert cell.status == "never_run"
    assert "no registered evidence producer" in cell.reason
    assert "antigravity_hook_inbox" in cell.scenario_ids


@pytest.mark.parametrize("build_provenance", ("generated_fake", "staged_release"))
def test_manual_trigger_never_advertises_non_observed_provider_provenance(facts, build_provenance: str) -> None:
    cell = plan_run(facts, "codex", build_provenance, "manual")

    assert cell.status == "never_run"
    assert "observed provider install" in cell.reason


def test_plan_run_rejects_unknown_provider(facts) -> None:
    with pytest.raises(ValueError):
        plan_run(facts, "not-a-provider", "staged_release", "release_poll")


def test_plan_run_rejects_unknown_build_provenance(facts) -> None:
    with pytest.raises(ValueError):
        plan_run(facts, "codex", "not-a-provenance", "release_poll")


def test_plan_run_rejects_unknown_trigger(facts) -> None:
    with pytest.raises(ValueError):
        plan_run(facts, "codex", "staged_release", "not-a-trigger")


def test_load_capability_assertions_matches_load_facts(facts) -> None:
    # The narrow public entry point (used by the live capability-projection
    # endpoint, which has no reason to depend on load_facts()'s Makefile/
    # weekly-schedule I/O) must return exactly the same data load_facts()
    # does for the same field.
    assert load_capability_assertions() == facts.capability_assertions
