"""Deterministic harness tests for proactive Oikos autonomy journeys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.services.oikos_autonomy_journeys import AutonomyJourneyResult
from zerg.services.oikos_autonomy_journeys import OikosAutonomyJourneyRunner
from zerg.services.oikos_autonomy_journeys import _safe_parent
from zerg.services.oikos_autonomy_journeys import baseline_shadow_decider
from zerg.services.oikos_autonomy_journeys import load_autonomy_journey_cases
from zerg.services.oikos_autonomy_journeys import run_autonomy_journeys
from zerg.session_loop_mode import SessionLoopMode

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "oikos_autonomy_journeys.yml"


def _load_cases():
    return load_autonomy_journey_cases(FIXTURE_PATH)


def test_safe_parent_falls_back_for_packaged_runtime_paths():
    runtime_path = Path("/app/zerg/services/oikos_autonomy_journeys.py")

    assert _safe_parent(runtime_path, 2, runtime_path.parent) == Path("/app")
    assert _safe_parent(runtime_path, 5, Path("/app")) == Path("/app")


def test_load_autonomy_journey_cases_reads_expected_fixture():
    cases = _load_cases()

    assert len(cases) == 15
    assert [case.id for case in cases] == [
        "completed_nothing_left",
        "completed_obvious_follow_up",
        "blocked_human_fork",
        "periodic_sweep_idle_noop",
        "needs_user_low_priority",
        "duplicate_blocked_wakeup_noop",
        "blocked_bounded_follow_up",
        "competing_sessions_prioritize_human_fork",
        "assist_bounded_follow_up_suggest",
        "manual_bounded_follow_up_ignore",
        "manual_human_fork_ignore",
        "risky_follow_up_requires_escalation",
        "autopilot_continue_cap_requires_handoff",
        "sleeping_on_demand_runner_blocks_follow_up",
        "explicit_refusal_requires_escalation",
    ]
    assert cases[1].artifacts[0].path == "/tmp/failing-tests.log"
    assert cases[1].primary_session.loop_mode == SessionLoopMode.AUTOPILOT
    assert cases[8].primary_session.loop_mode == SessionLoopMode.ASSIST
    assert cases[10].primary_session.loop_mode == SessionLoopMode.MANUAL


@pytest.mark.asyncio
async def test_runner_persists_artifacts_and_matches_expected_outcomes(tmp_path):
    runner = OikosAutonomyJourneyRunner(
        artifact_root=tmp_path,
        decider=baseline_shadow_decider,
    )
    decisions_by_case: dict[str, dict] = {}

    for case in _load_cases():
        result: AutonomyJourneyResult = await runner.run_case(case)

        assert result.decision.decision == case.expected.decision
        assert len(result.decision.proposed_actions) == case.expected.action_count

        required = {action.kind for action in result.decision.proposed_actions}
        for required_action in case.expected.required_actions:
            assert required_action in required

        forbidden = {action.kind for action in result.decision.proposed_actions}
        for forbidden_action in case.expected.forbidden_actions:
            assert forbidden_action not in forbidden

        assert result.run_dir.exists()
        assert result.manifest_path.exists()
        assert result.context_path.exists()
        assert result.decision_path.exists()
        assert result.assertions_path.exists()

        manifest = json.loads(result.manifest_path.read_text())
        context = json.loads(result.context_path.read_text())
        decision = json.loads(result.decision_path.read_text())
        assertions = json.loads(result.assertions_path.read_text())

        assert manifest["case_id"] == case.id
        assert manifest["assertion_count"] == len(assertions)
        assert manifest["assertions_passed"] is True
        assert context["trigger"]["type"] == case.trigger.type
        assert context["primary_session"]["session_id"] == case.primary_session.session_id
        assert decision["decision"] == case.expected.decision
        assert decision["needs_human"] == case.expected.needs_human
        assert decision["summary"]
        assert decision["mode_capability"]
        assert decision["mode_summary"]
        assert decision["execution_state"]
        assert isinstance(decision["blocked_reasons"], list)
        assert assertions
        assert all(assertion["passed"] for assertion in assertions)
        decisions_by_case[case.id] = decision

    assert decisions_by_case["assist_bounded_follow_up_suggest"]["mode_capability"] == "notify_only"
    assert decisions_by_case["assist_bounded_follow_up_suggest"]["execution_state"] == "awaiting_user_approval"
    assert decisions_by_case["completed_obvious_follow_up"]["mode_capability"] == "bounded_autonomy"
    assert decisions_by_case["completed_obvious_follow_up"]["execution_state"] == "would_auto_continue"
    assert decisions_by_case["risky_follow_up_requires_escalation"]["blocked_reasons"] == [
        "Risky or explicitly declined next step requires direct approval."
    ]


@pytest.mark.asyncio
async def test_runner_builds_compact_context_packet(tmp_path):
    case = _load_cases()[2]
    runner = OikosAutonomyJourneyRunner(
        artifact_root=tmp_path,
        decider=baseline_shadow_decider,
    )

    packet = runner.build_context_packet(case)

    assert packet.case_id == "blocked_human_fork"
    assert packet.trigger.type == "session_blocked"
    assert packet.primary_session.blocked_reason == "Needs user product decision about autonomy strategy."
    assert packet.primary_session.loop_mode == SessionLoopMode.ASSIST
    assert packet.policy.shadow_mode is True
    assert packet.policy.allow_continue is True


@pytest.mark.asyncio
async def test_run_autonomy_journeys_executes_fixture_file_into_stable_root(tmp_path):
    results = await run_autonomy_journeys(
        fixture_path=FIXTURE_PATH,
        artifact_root=tmp_path,
    )

    assert [result.case_id for result in results] == [
        "completed_nothing_left",
        "completed_obvious_follow_up",
        "blocked_human_fork",
        "periodic_sweep_idle_noop",
        "needs_user_low_priority",
        "duplicate_blocked_wakeup_noop",
        "blocked_bounded_follow_up",
        "competing_sessions_prioritize_human_fork",
        "assist_bounded_follow_up_suggest",
        "manual_bounded_follow_up_ignore",
        "manual_human_fork_ignore",
        "risky_follow_up_requires_escalation",
        "autopilot_continue_cap_requires_handoff",
        "sleeping_on_demand_runner_blocks_follow_up",
        "explicit_refusal_requires_escalation",
    ]
    assert all(result.run_dir.parent == tmp_path for result in results)
    assert all(result.context_path.exists() for result in results)
    assert all(result.assertions_path.exists() for result in results)
    assert all(all(assertion.passed for assertion in result.assertions) for result in results)
