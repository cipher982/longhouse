"""Deterministic harness tests for proactive Oikos autonomy journeys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg.services.oikos_autonomy_journeys import AutonomyJourneyResult
from zerg.services.oikos_autonomy_journeys import OikosAutonomyJourneyRunner
from zerg.services.oikos_autonomy_journeys import baseline_shadow_decider
from zerg.services.oikos_autonomy_journeys import load_autonomy_journey_cases
from zerg.services.oikos_autonomy_journeys import run_autonomy_journeys

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "oikos_autonomy_journeys.yml"


def _load_cases():
    return load_autonomy_journey_cases(FIXTURE_PATH)


def test_load_autonomy_journey_cases_reads_expected_fixture():
    cases = _load_cases()

    assert len(cases) == 4
    assert [case.id for case in cases] == [
        "completed_nothing_left",
        "completed_obvious_follow_up",
        "blocked_human_fork",
        "periodic_sweep_idle_noop",
    ]
    assert cases[1].artifacts[0].path == "/tmp/failing-tests.log"


@pytest.mark.asyncio
async def test_runner_persists_artifacts_and_matches_expected_outcomes(tmp_path):
    runner = OikosAutonomyJourneyRunner(
        artifact_root=tmp_path,
        decider=baseline_shadow_decider,
    )

    for case in _load_cases():
        result: AutonomyJourneyResult = await runner.run_case(case)

        assert result.decision.decision == case.expected.decision
        assert len(result.decision.proposed_actions) == case.expected.action_count

        forbidden = {action.kind for action in result.decision.proposed_actions}
        for forbidden_action in case.expected.forbidden_actions:
            assert forbidden_action not in forbidden

        assert result.run_dir.exists()
        assert result.manifest_path.exists()
        assert result.context_path.exists()
        assert result.decision_path.exists()

        manifest = json.loads(result.manifest_path.read_text())
        context = json.loads(result.context_path.read_text())
        decision = json.loads(result.decision_path.read_text())

        assert manifest["case_id"] == case.id
        assert context["trigger"]["type"] == case.trigger.type
        assert context["primary_session"]["session_id"] == case.primary_session.session_id
        assert decision["decision"] == case.expected.decision
        assert decision["summary"]


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
    ]
    assert all(result.run_dir.parent == tmp_path for result in results)
    assert all(result.context_path.exists() for result in results)
