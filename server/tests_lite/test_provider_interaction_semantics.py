from __future__ import annotations

import json

import pytest

from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_interaction_semantics import evaluate_observation
from zerg.qa.provider_interaction_semantics import generated_fake_observation
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL_OUTPUT
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import title_eligible_provider_event


def test_every_factory_provider_declares_interaction_probes() -> None:
    contracts = {contract.provider: contract for contract in all_managed_provider_contracts()}

    assert set(ALL_PROVIDERS) <= set(contracts)
    assert all(contract.interaction_probes for contract in contracts.values())
    assert all(
        len({probe.probe_id for probe in contract.interaction_probes}) == len(contract.interaction_probes)
        for contract in contracts.values()
    )


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_generated_observation_passes_for_every_provider(provider: str) -> None:
    observation = generated_fake_observation(provider)
    result = evaluate_observation(provider, observation)

    assert result["status"] == "pass"
    assert result["probe_count"] == len(observation["probes"])
    assert all(row["status"] in {"pass", "not_applicable"} for row in result["assertions"])


def test_missing_non_policy_probe_is_blocked() -> None:
    observation = generated_fake_observation("claude")
    observation["probes"] = [row for row in observation["probes"] if row["probe_id"] != "claude_effort_command"]

    result = evaluate_observation("claude", observation)

    assert result["status"] == "blocked"
    missing = next(row for row in result["assertions"] if row["probe_id"] == "claude_effort_command")
    assert missing["failure_code"] == "interaction_probe_not_observed"


def test_probe_requires_declared_raw_markers() -> None:
    observation = generated_fake_observation("claude")
    effort = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
    effort["raw_events"] = [
        {
            "type": "user",
            "role": "user",
            "content_text": "<command-name>/effort</command-name>",
            "longhouse_interaction_kind": INTERACTION_LOCAL_CONTROL,
            "longhouse_changes_provider_state": True,
        },
        {
            "type": "user",
            "role": "user",
            "content_text": "<local-command-stdout>Set effort</local-command-stdout>",
            "longhouse_interaction_kind": INTERACTION_LOCAL_CONTROL_OUTPUT,
            "longhouse_changes_provider_state": False,
        },
    ]

    result = evaluate_observation("claude", observation)

    row = next(row for row in result["assertions"] if row["probe_id"] == "claude_effort_command")
    assert result["status"] == "fail"
    assert row["assertions"]["raw_markers_present"] is False


def test_claude_native_local_command_rows_are_not_user_or_title_events() -> None:
    command = "\n".join(
        (
            "<local-command-caveat>Caveat: /effort is a local command.</local-command-caveat>",
            "<command-name>/effort</command-name>",
            "<command-message>effort</command-message>",
            "<command-args>high</command-args>",
        )
    )
    output = "<local-command-stdout>Set effort level to high</local-command-stdout>"
    raw_command = {"type": "user", "message": {"role": "user", "content": command}}
    raw_output = {"type": "user", "message": {"role": "user", "content": output}}

    command_semantics = classify_provider_interaction(
        "claude", role="user", content_text=command, raw_json=json.dumps(raw_command)
    )
    output_semantics = classify_provider_interaction(
        "claude", role="user", content_text=output, raw_json=raw_output
    )

    assert command_semantics["interaction_kind"] == INTERACTION_LOCAL_CONTROL
    assert command_semantics["counts_as_user_message"] is False
    assert command_semantics["title_eligible"] is False
    assert command_semantics["starts_model_turn"] is False
    assert output_semantics["interaction_kind"] == INTERACTION_LOCAL_CONTROL_OUTPUT
    assert output_semantics["counts_as_user_message"] is False
    assert output_semantics["title_eligible"] is False


def test_unknown_slash_text_remains_a_real_user_message() -> None:
    content = "/custom-command-that-provider-may-send-to-model"

    semantics = classify_provider_interaction("codex", role="user", content_text=content)

    assert semantics["counts_as_user_message"] is True
    assert title_eligible_provider_event("codex", role="user", content_text=content) is True
