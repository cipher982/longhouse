"""Provider-independent postcondition oracles for coordination scenarios."""

from __future__ import annotations

from collections.abc import Mapping


def awareness_create_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "coordination_instructions_model_visible": observation.get("coordination_instructions_model_visible") is True,
    }


def awareness_post_compaction_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "coordination_instructions_model_visible_after_compaction": (
            observation.get("coordination_instructions_model_visible_after_compaction") is True
        ),
        "no_duplicate_visible_bootstrap": (
            observation.get("visible_bootstrap_count") is not None and observation.get("visible_bootstrap_count") in {0, 1}
        ),
    }


def directed_input_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "directed_input_persisted": observation.get("input_persisted") is True,
        "provider_input_receipt_linked": observation.get("input_receipt_linked") is True,
        "attributed_input_visible": (observation.get("input_visible") is True and bool(observation.get("source_session_id"))),
    }


JUDGE_REVISION = 1
JUDGE = "zerg.qa.provider_coordination_oracles:judge"

# The scenario a result records says which oracle decides it. ``scenario_id`` is the extra input beyond the observation:
# every coordination producer writes it into its result.json, and every provider's scenario ends with its kind.
_ORACLE_BY_SCENARIO_KIND = {
    "_awareness_create": awareness_create_assertions,
    "_awareness_post_compaction": awareness_post_compaction_assertions,
    "_directed_input": directed_input_assertions,
}


def judge(evidence: Mapping[str, object]) -> dict[str, bool]:
    """Pure: the assertions a recorded coordination cell reached, from its ``scenario_id`` and ``observation``.

    An unknown scenario raises (the replay files that as an error) and never judges as a pass.
    """

    scenario_id = evidence.get("scenario_id")
    observation = evidence.get("observation")
    if not isinstance(scenario_id, str) or not isinstance(observation, Mapping):
        raise ValueError("a coordination result needs a scenario_id and an observation")
    for kind, oracle in _ORACLE_BY_SCENARIO_KIND.items():
        if scenario_id.endswith(kind):
            return oracle(observation)
    raise KeyError(scenario_id)
