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


def directed_input_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    return {
        "directed_input_persisted": observation.get("input_persisted") is True,
        "provider_input_receipt_linked": observation.get("input_receipt_linked") is True,
        "attributed_input_visible": (observation.get("input_visible") is True and bool(observation.get("source_session_id"))),
    }
