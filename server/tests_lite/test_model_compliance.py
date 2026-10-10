from __future__ import annotations

import pytest

from zerg.qa.model_compliance import attach
from zerg.qa.model_compliance import noncompliance_entry


def test_entry_records_the_reason_and_the_proven_facts() -> None:
    entry = noncompliance_entry(
        "steer_answered_then_original_task_continued", steer_delivered_in_target_turn=True, steer_preceded_continuation=True
    )

    assert entry == {
        "reason": "steer_answered_then_original_task_continued",
        "contract_evidence": {"steer_delivered_in_target_turn": True, "steer_preceded_continuation": True},
    }


@pytest.mark.parametrize("reason", ["", "Steer_Answered", "steer answered", "steer-answered", "_leading", "trailing_"])
def test_reason_must_be_snake_case(reason: str) -> None:
    with pytest.raises(ValueError):
        noncompliance_entry(reason, fact=True)


def test_at_least_one_fact_is_required() -> None:
    with pytest.raises(ValueError):
        noncompliance_entry("steer_answered")


@pytest.mark.parametrize("value", [False, 1, "True", None])
def test_every_fact_must_be_literal_true(value: object) -> None:
    with pytest.raises(ValueError):
        noncompliance_entry("steer_answered", fact=value, other=True)  # type: ignore[arg-type]


def test_attach_sets_the_entry_under_the_assertion_id_and_keeps_others() -> None:
    observation: dict = {"lifecycle": {}}
    first = noncompliance_entry("tool_used", tool_invoked=True)
    second = noncompliance_entry("steer_answered", steer_delivered=True)

    attach(observation, "assertion_a", first)
    attach(observation, "assertion_b", second)

    assert observation["lifecycle"] == {}
    assert observation["model_noncompliance"] == {"assertion_a": first, "assertion_b": second}


def test_attach_refuses_a_hand_built_entry_that_breaks_the_contract() -> None:
    observation: dict = {}

    with pytest.raises(ValueError):
        attach(observation, "assertion_a", {"reason": "tool_used", "contract_evidence": {"tool_invoked": False}})
    with pytest.raises(ValueError):
        attach(observation, "assertion_a", {"reason": "tool_used", "contract_evidence": {}})

    assert "model_noncompliance" not in observation
