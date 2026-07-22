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
        "no_duplicate_visible_bootstrap": observation.get("visible_bootstrap_count") in {None, 0, 1},
    }


def message_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "directed_message_persisted_and_delivered": (
            observation.get("message_persisted") is True and observation.get("message_delivered") is True
        ),
        "attributed_message_visible": (observation.get("message_visible") is True and bool(observation.get("source_session_id"))),
    }
