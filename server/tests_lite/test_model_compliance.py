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


def test_a_producer_that_stops_on_the_models_reply_carries_its_claim_on_the_failure_artifact() -> None:
    from zerg.qa.model_compliance import ModelDeviation
    from zerg.qa.model_compliance import failure_claim

    deviation = ModelDeviation(
        "reply_carried_another_marker", "the turn completed and its reply carries another marker", run_completed=True, provider_replied=True
    )

    assert str(deviation) == "reply_carried_another_marker: the turn completed and its reply carries another marker"
    claim = {
        "model_noncompliance": {
            "reason": "reply_carried_another_marker",
            "contract_evidence": {"run_completed": True, "provider_replied": True},
        }
    }
    assert failure_claim(deviation) == claim
    # Raised from it explicitly, the claim travels; an unrelated error carries none.
    try:
        raise RuntimeError("producer stopped") from deviation
    except RuntimeError as wrapped:
        assert failure_claim(wrapped) == claim
    assert failure_claim(RuntimeError("timed out")) == {} and failure_claim(None) == {}
    # The same contract as an observation's entry: a deviation without a proven fact cannot be built.
    with pytest.raises(ValueError):
        ModelDeviation("reply_carried_another_marker", "no fact")
