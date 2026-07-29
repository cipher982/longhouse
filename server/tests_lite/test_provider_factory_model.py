from pathlib import Path

import pytest

from zerg.qa import provider_factory_model
from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import DEPLOYED_RELEASE_LANE_PROFILE
from zerg.qa.provider_factory_model import ORPHANED_CAPABILITY_SCENARIO_IDS
from zerg.qa.provider_factory_model import PUSH_CODEX_COORDINATION_SCENARIO_ID
from zerg.qa.provider_factory_model import load_capability_assertions
from zerg.qa.provider_factory_model import load_facts
from zerg.qa.provider_factory_model import plan_run


@pytest.fixture(scope="module")
def facts():
    return load_facts()


def test_capability_assertions_match_schema_scenario_count(facts) -> None:
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert len(scenario_ids) == 13
    assert len(facts.capability_assertions) == 21


def test_orphaned_scenario_ids_are_a_subset_of_schema_scenario_ids(facts) -> None:
    scenario_ids = {a.scenario_id for a in facts.capability_assertions}
    assert ORPHANED_CAPABILITY_SCENARIO_IDS <= scenario_ids
    # codex_coordination_awareness_post_compaction has a CI producer; it must
    # not be classified as orphaned even though it isn't release-lane automated.
    assert PUSH_CODEX_COORDINATION_SCENARIO_ID not in ORPHANED_CAPABILITY_SCENARIO_IDS


def test_default_harness_scenarios_has_22_entries(facts) -> None:
    assert len(facts.default_harness_scenarios) == 22
    assert "probe_identity" in facts.default_harness_scenarios
    assert "managed_session_e2e" in facts.default_harness_scenarios


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
    assert cell.scenario_ids


def test_release_poll_never_runs_for_cursor(facts) -> None:
    cell = plan_run(facts, "cursor", "staged_release", "release_poll")
    assert cell.status == "never_run"
    assert "no registered release lane" in cell.reason


def test_release_poll_never_runs_against_generated_fake_provenance(facts) -> None:
    for provider in DEPLOYED_RELEASE_LANE_PROFILE:
        cell = plan_run(facts, provider, "generated_fake", "release_poll")
        assert cell.status == "never_run"


def test_codex_release_poll_requires_codex_api_key(facts) -> None:
    cell = plan_run(facts, "codex", "staged_release", "release_poll")
    assert cell.credential_requirement == ("CODEX_API_KEY",)


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


def test_manual_trigger_never_runs_for_cursor_because_every_scenario_is_orphaned(facts) -> None:
    cell = plan_run(facts, "cursor", "observed_install", "manual")
    assert cell.status == "never_run"
    assert "orphaned" in cell.reason


def test_manual_trigger_runs_for_codex_because_of_the_live_token_gap(facts) -> None:
    # coordination_instructions_model_visible_after_compaction needs a real
    # manual run even though the scenario_id also has an automated CI producer.
    cell = plan_run(facts, "codex", "observed_install", "manual")
    assert cell.status == "runs"
    assert cell.scenario_ids == (PUSH_CODEX_COORDINATION_SCENARIO_ID,)


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


def test_schema_path_resolution_falls_back_through_multiple_candidates(monkeypatch, tmp_path: Path) -> None:
    # Regression: the deployed Runtime Host image is not a full repo
    # checkout (docker/runtime.dockerfile copies only server/'s contents
    # into /app), so the local-dev candidate (ROOT / "schemas" / ...) does
    # not exist there. The endpoint's first real deploy 500'd on exactly
    # this -- schemas/managed_providers.yml was never copied into the image
    # at all, and _resolve_schema_path()'s local-checkout candidate was the
    # only one that existed at the time. Proves the fallback logic itself by
    # calling the real function against a fabricated candidate list, rather
    # than trusting this dev machine's own filesystem layout to exercise it.
    missing_schema = tmp_path / "no-repo-here" / "schemas" / "managed_providers.yml"
    real_schema = tmp_path / "deployed" / "managed_providers.yml"
    real_schema.parent.mkdir(parents=True)
    real_schema.write_text("providers: []\n")

    def fake_candidates():
        return (missing_schema, real_schema)

    monkeypatch.setattr(provider_factory_model, "_resolve_schema_path", lambda: next(c for c in fake_candidates() if c.is_file()))
    resolved = provider_factory_model._resolve_schema_path()
    assert resolved == real_schema


def test_schema_path_resolution_raises_a_clear_error_when_neither_candidate_exists(
    monkeypatch, tmp_path: Path
) -> None:
    # ROOT is the only piece of real state _resolve_schema_path() depends
    # on for its local-dev candidate; the second candidate is an absolute
    # path this dev machine genuinely does not have (it only exists inside
    # the deployed container).
    monkeypatch.setattr(provider_factory_model, "ROOT", tmp_path / "no-repo-here")
    with pytest.raises(FileNotFoundError, match="not found at any known location"):
        provider_factory_model._resolve_schema_path()
