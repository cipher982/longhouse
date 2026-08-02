import pytest

from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import DEPLOYED_RELEASE_LANE_PROFILE
from zerg.qa.provider_factory_model import DEPLOYED_RELEASE_LANE_PROFILES
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
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert len(scenario_ids) == 16
    assert len(facts.capability_assertions) == 24


def test_orphaned_scenario_ids_are_a_subset_of_schema_scenario_ids(facts) -> None:
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert ORPHANED_CAPABILITY_SCENARIO_IDS <= scenario_ids
    # codex_coordination_awareness_post_compaction has a CI producer; it must
    # not be classified as orphaned even though it isn't release-lane automated.
    assert PUSH_CODEX_COORDINATION_SCENARIO_ID not in ORPHANED_CAPABILITY_SCENARIO_IDS


def test_default_harness_scenarios_has_23_entries(facts) -> None:
    assert len(facts.default_harness_scenarios) == 23
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
    assert set(facts.weekly_cron_providers) == set(ALL_PROVIDERS)


@pytest.mark.parametrize("provider", ["codex", "claude", "opencode", "antigravity"])
def test_release_poll_runs_the_deployed_profile_for_automated_providers(facts, provider: str) -> None:
    cell = plan_run(facts, provider, "staged_release", "release_poll")
    assert cell.status == "runs"
    assert cell.qualification_profile == DEPLOYED_RELEASE_LANE_PROFILE[provider]
    assert cell.qualification_profiles == DEPLOYED_RELEASE_LANE_PROFILES[provider]
    assert cell.scenario_ids


def test_release_poll_never_runs_for_cursor(facts) -> None:
    cell = plan_run(facts, "cursor", "staged_release", "release_poll")
    assert cell.status == "never_run"
    assert "no registered release lane" in cell.reason


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


def test_antigravity_release_poll_runs_the_full_staged_column_without_live_credentials(facts) -> None:
    cell = plan_run(facts, "antigravity", "staged_release", "release_poll")

    assert cell.qualification_profiles == ("antigravity_hook_inbox_v1",)
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert cell.credential_requirement == ()


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


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_weekly_cron_runs_the_full_default_scenario_set_for_every_provider(facts, provider: str) -> None:
    cell = plan_run(facts, provider, "generated_fake", "weekly_cron")
    assert cell.status == "runs"
    assert cell.harness_scenarios == facts.default_harness_scenarios


def test_weekly_cron_never_runs_against_staged_release_provenance(facts) -> None:
    cell = plan_run(facts, "codex", "staged_release", "weekly_cron")
    assert cell.status == "never_run"


def test_manual_trigger_runs_full_column_for_cursor_observed_install(facts) -> None:
    cell = plan_run(facts, "cursor", "observed_install", "manual")
    assert cell.status == "runs"
    assert cell.harness_scenarios == facts.default_harness_scenarios
    assert "Gate 0" in cell.reason


def test_manual_trigger_runs_for_codex_because_of_the_live_token_gap(facts) -> None:
    # coordination_instructions_model_visible_after_compaction needs a real
    # manual run even though the scenario_id also has an automated CI producer.
    cell = plan_run(facts, "codex", "observed_install", "manual")
    assert cell.status == "runs"
    assert cell.scenario_ids == (PUSH_CODEX_COORDINATION_SCENARIO_ID, "codex_turn_boundary_quiescent")


def test_manual_trigger_runs_for_antigravity_because_live_print_is_permanently_blocked(facts) -> None:
    cell = plan_run(facts, "antigravity", "observed_install", "manual")
    assert cell.status == "runs"
    assert "antigravity_hook_inbox" in cell.scenario_ids


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
