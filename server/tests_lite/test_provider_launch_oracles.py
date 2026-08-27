from __future__ import annotations

from copy import deepcopy

import pytest

from zerg.qa.provider_launch_oracles import ASSERTION_ID
from zerg.qa.provider_launch_oracles import helm_launch_assertions


def _human(*, resumed: bool, run_id: str) -> dict:
    registration = {
        "provider": "codex",
        "launch_actor": "human_shell",
        "launch_surface": "terminal",
        "session_id": "session-1",
        "run_id": run_id,
    }
    if resumed:
        registration["resume_attempt_id"] = "attempt-1"
    return {
        "registration": registration,
        "canonical": {
            "session_id": "session-1",
            "mode": "helm",
            "working_set": "open",
            "control_head_current": True,
            "control_run_id": run_id,
            "factory_machine_identity": "provider-factory-resume",
            "factory_policy_hidden": True,
            "default_timeline_visible": False,
            "observed_within_seconds": 2.5,
        },
    }


def _cleanup(session_id: str) -> dict:
    return {
        "status": "pass",
        "session_id": session_id,
        "axes": {
            "default_timeline_absent": True,
            "open_absent": True,
            "title_debt_absent": True,
            "workspace_suggestion_absent": True,
            "direct_retrieval_succeeds": True,
            "owned_processes_dead": True,
        },
    }


def _observation() -> dict:
    return {
        "fresh": _human(resumed=False, run_id="run-1"),
        "resumed": _human(resumed=True, run_id="run-2"),
        "automation": {
            "registration": {
                "provider": "codex",
                "launch_actor": "automation",
                "launch_surface": "test",
            },
            "canonical": {"default_timeline_visible": False},
        },
        "same_session_resumed": True,
        "new_run_on_resume": True,
        "provenance_free_observation_rejected": True,
        "cleanup": [_cleanup("session-1"), _cleanup("session-2")],
    }


def test_helm_launch_oracle_requires_complete_live_transaction():
    assert helm_launch_assertions(_observation()) == {ASSERTION_ID: True}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("fresh", "registration", "launch_actor"), None),
        (("fresh", "registration", "launch_surface"), None),
        (("fresh", "canonical", "working_set"), "history"),
        (("fresh", "canonical", "control_head_current"), False),
        (("fresh", "canonical", "factory_machine_identity"), "some-other-machine"),
        (("resumed", "registration", "resume_attempt_id"), None),
        (("automation", "registration", "launch_actor"), None),
        (("automation", "canonical", "default_timeline_visible"), True),
        (("cleanup", 0, "axes", "workspace_suggestion_absent"), False),
    ],
)
def test_helm_launch_oracle_fails_closed_on_missing_or_wrong_fact(path, value):
    observation = deepcopy(_observation())
    target = observation
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert helm_launch_assertions(observation) == {ASSERTION_ID: False}


def test_provenance_free_registration_is_rejected():
    observation = _observation()
    observation["fresh"]["registration"].pop("launch_actor")
    observation["fresh"]["registration"].pop("launch_surface")
    assert helm_launch_assertions(observation) == {ASSERTION_ID: False}
