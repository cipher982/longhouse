from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_interaction_semantics import evaluate_observation
from zerg.qa.provider_interaction_semantics import generated_fake_observation
from zerg.qa.provider_interaction_semantics import raw_event_digest
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.provider_interaction_semantics import INTERACTION_DURABLE_USER_MESSAGE
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL
from zerg.services.provider_interaction_semantics import INTERACTION_LOCAL_CONTROL_OUTPUT
from zerg.services.provider_interaction_semantics import classify_provider_interaction
from zerg.services.provider_interaction_semantics import interaction_context_key_parts
from zerg.services.provider_interaction_semantics import seed_provider_interaction_sequence_context
from zerg.services.provider_interaction_semantics import semantic_event_included
from zerg.services.provider_interaction_semantics import title_eligible_provider_event


def test_every_factory_provider_declares_interaction_probes() -> None:
    contracts = {contract.provider: contract for contract in all_managed_provider_contracts()}

    assert set(ALL_PROVIDERS) <= set(contracts)
    assert all(contract.interaction_probes for contract in contracts.values())
    assert all(
        len({probe.probe_id for probe in contract.interaction_probes}) == len(contract.interaction_probes)
        for contract in contracts.values()
    )


def test_captured_interaction_fixtures_carry_provenance_sidecars() -> None:
    fixture_root = Path(__file__).parent / "fixtures/provider_interactions"
    fixtures = sorted(fixture_root.glob("*.jsonl"))
    assert fixtures

    for fixture in fixtures:
        metadata_path = fixture.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text())
        source = metadata["source_artifact"]
        assert metadata["fixture"] == fixture.name
        assert metadata["evidence_class"] == "captured_provider_shape"
        assert len(source["source_file_sha256"]) == 64
        assert source["source_line_range"][0] <= source["source_line_range"][1]
        assert source["source_line_sha256"]
        assert all(len(value) == 64 for value in source["source_line_sha256"])
        assert source["native_event_uuids"]
        versions = {json.loads(line)["version"] for line in fixture.read_text().splitlines()}
        assert versions == {metadata["provider_version"]}


@pytest.mark.parametrize("provider", ALL_PROVIDERS)
def test_generated_observation_passes_for_every_provider(provider: str) -> None:
    observation = generated_fake_observation(provider)
    result = evaluate_observation(provider, observation)

    assert result["status"] == "pass"
    assert result["provider_status"] == "not_applicable"
    assert result["verification_scope"] == "semantic_engine"
    assert result["probe_count"] == len(observation["probes"])
    assert all(row["status"] in {"pass", "not_applicable"} for row in result["assertions"])


def test_live_policy_disabled_provider_is_not_applicable_without_boundary_rows() -> None:
    observation = generated_fake_observation("antigravity")
    observation.update({"evidence_class": "live_no_token", "synthetic": False, "raw_events": []})

    result = evaluate_observation("antigravity", observation)

    assert result["status"] == "not_applicable"
    assert result["provider_status"] == "not_applicable"


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


def test_live_observation_does_not_infer_negative_turn_or_state_claims() -> None:
    observation = generated_fake_observation("claude")
    observation["evidence_class"] = "live_no_token"
    observation["synthetic"] = False
    effort = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
    effort["raw_events"] = observation["raw_events"][:2]

    result = evaluate_observation("claude", observation)

    row = next(row for row in result["assertions"] if row["probe_id"] == "claude_effort_command")
    assert row["status"] == "blocked"
    assert row["failure_code"] == "interaction_post_state_evidence_missing"
    assert row["evidence_basis"]["raw_provenance"] == "blocked"


def test_live_observation_uses_raw_capture_for_turn_and_state_assertions(tmp_path: Path) -> None:
    observation = generated_fake_observation("claude")
    observation["evidence_class"] = "live_no_token"
    observation["synthetic"] = False
    observation["native_source_root"] = str(tmp_path)
    effort = next(row for row in observation["probes"] if row["probe_id"] == "claude_effort_command")
    effort["capture_complete"] = True
    effort["post_interaction_quiescent"] = True
    effort["raw_events"] = observation["raw_events"][:2]
    lines = [
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for event in effort["raw_events"]
    ]
    source_path = tmp_path / "history.jsonl"
    source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source_bytes = source_path.read_bytes()
    source_rows = [
        {
            "source_path": str(source_path),
            "source_offset": sum(len(line.encode("utf-8")) + 1 for line in lines[:index]),
            "line": lines[index],
            "line_sha256": hashlib.sha256(
                lines[index].encode("utf-8")
            ).hexdigest(),
            "event_sha256": raw_event_digest(event),
            "source_binding": "file_bytes_at_offset",
            "source_file_bytes": len(source_bytes),
            "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        for index, event in enumerate(effort["raw_events"])
    ]
    effort["native_source_rows"] = source_rows
    effort["capture_receipt"] = {
        "stable_snapshots": 3,
        "stable_seconds": 1.5,
        "raw_event_count": len(source_rows),
        "window_sha256": hashlib.sha256(
            "".join(source["event_sha256"] for source in source_rows).encode("ascii")
        ).hexdigest(),
    }

    result = evaluate_observation("claude", observation)

    row = next(row for row in result["assertions"] if row["probe_id"] == "claude_effort_command")
    assert row["status"] == "pass"
    assert row["assertions"]["expected_model_turn"] is True
    assert row["assertions"]["expected_state_change"] is True
    assert row["evidence_basis"]["raw_output_markers"] == "raw_events"
    assert row["evidence_basis"]["raw_provenance"] == "pass"


def test_live_observation_does_not_accept_normalized_semantics_from_provider_rows() -> None:
    observation = generated_fake_observation("codex")
    observation["evidence_class"] = "live_no_token"
    observation["synthetic"] = False
    probe = next(row for row in observation["probes"] if row["raw_events"])

    result = evaluate_observation("codex", observation)

    row = next(row for row in result["assertions"] if row["probe_id"] == probe["probe_id"])
    assert row["status"] == "blocked"
    assert row["semantic_events"][0]["interaction_kind"] == INTERACTION_DURABLE_USER_MESSAGE


def test_raw_provider_fields_cannot_override_parser_semantics() -> None:
    raw = {
        "type": "user",
        "message": {"role": "user", "content": "Build the feature"},
        "longhouse_interaction_kind": INTERACTION_LOCAL_CONTROL,
        "longhouse_changes_provider_state": True,
    }

    semantics = classify_provider_interaction(
        "codex",
        role="user",
        content_text="Build the feature",
        raw_json=raw,
    )

    assert semantics["interaction_kind"] == "durable_user_message"
    assert semantics["title_eligible"] is True


def test_claude_composite_interaction_context_key_stays_within_storage_bound() -> None:
    key = classify_provider_interaction(
        "claude",
        role="user",
        content_text="<command-name>/effort</command-name>",
        raw_json={"promptId": "p" * 10_000, "uuid": "u" * 10_000},
    )["interaction_context_key"]

    assert isinstance(key, str)
    assert len(key.encode("utf-8")) <= 255
    assert all(part for part in interaction_context_key_parts(key))


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
    raw_command = {"type": "user", "isMeta": True, "message": {"role": "user", "content": command}}
    raw_output = {"type": "user", "isMeta": True, "message": {"role": "user", "content": output}}

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


def test_captured_claude_effort_jsonl_classifies_real_command_rows() -> None:
    fixture = Path(__file__).parent / "fixtures/provider_interactions/claude-2.1.219-effort.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    sequence_context: dict[str, object] = {}

    semantics = [
        classify_provider_interaction(
            "claude",
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=row,
            sequence_context=sequence_context,
        )
        for row in rows
    ]

    assert [row["interaction_kind"] for row in semantics] == [
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        "durable_user_message",
    ]
    assert [row["title_eligible"] for row in semantics] == [False, False, False, True]


def test_captured_claude_effort_without_prompt_id_uses_uuid_chain() -> None:
    fixture = Path(__file__).parent / "fixtures/provider_interactions/claude-2.1.92-effort-no-prompt-id.jsonl"
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    sequence_context: dict[str, object] = {}

    semantics = [
        classify_provider_interaction(
            "claude",
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=row,
            sequence_context=sequence_context,
        )
        for row in rows
    ]

    assert [row["interaction_kind"] for row in semantics] == [
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        "durable_user_message",
    ]
    assert [row["title_eligible"] for row in semantics] == [False, False, False, True]
    assert [row["interaction_context_key"] for row in semantics[:3]] == [
        "uuid:claude-caveat-1",
        "uuid:claude-command-1",
        "uuid:claude-output-1",
    ]


def test_unknown_slash_text_remains_a_real_user_message() -> None:
    content = "/custom-command-that-provider-may-send-to-model"

    semantics = classify_provider_interaction("codex", role="user", content_text=content)

    assert semantics["counts_as_user_message"] is True
    assert title_eligible_provider_event("codex", role="user", content_text=content) is True


def test_claude_prompt_quoting_local_command_markup_remains_a_real_message() -> None:
    quoted = "Explain the literal tag <command-name>/effort</command-name> in this prompt"
    raw = {"type": "user", "message": {"role": "user", "content": quoted}}

    semantics = classify_provider_interaction("claude", role="user", content_text=quoted, raw_json=raw)

    assert semantics["interaction_kind"] == "durable_user_message"
    assert semantics["title_eligible"] is True


def test_exact_claude_command_markup_without_sequence_evidence_remains_a_real_message() -> None:
    content = "<command-name>/effort</command-name><command-message>effort</command-message><command-args>high</command-args>"
    raw = {"type": "user", "promptId": "ordinary-prompt", "message": {"role": "user", "content": content}}

    semantics = classify_provider_interaction("claude", role="user", content_text=content, raw_json=raw)

    assert semantics["interaction_kind"] == "durable_user_message"
    assert semantics["title_eligible"] is True


def test_oversized_claude_prompt_id_uses_stable_bounded_sequence_key() -> None:
    prompt_id = "p" * 512
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }
    command = {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    context: dict[str, object] = {}
    classify_provider_interaction(
        "claude",
        role="user",
        content_text=caveat["message"]["content"],
        raw_json=caveat,
        sequence_context=context,
    )
    semantics = classify_provider_interaction(
        "claude",
        role="user",
        content_text=command["message"]["content"],
        raw_json=command,
        sequence_context=context,
    )

    key = semantics["interaction_context_key"]
    assert semantics["interaction_kind"] == INTERACTION_LOCAL_CONTROL
    assert isinstance(key, str)
    assert len(key.encode("utf-8")) <= 255
    assert key.startswith("sha256:")


def test_claude_context_key_preserves_prompt_and_uuid_fallbacks() -> None:
    raw = {
        "type": "user",
        "isMeta": True,
        "promptId": "prompt-effort-1",
        "uuid": "caveat-uuid-1",
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }

    semantics = classify_provider_interaction(
        "claude",
        role="user",
        content_text=raw["message"]["content"],
        raw_json=raw,
    )

    assert interaction_context_key_parts(semantics["interaction_context_key"]) == (
        "prompt-effort-1",
        "uuid:caveat-uuid-1",
    )


def test_claude_prompt_id_lengths_that_fit_legacy_storage_still_match_split_rows() -> None:
    prompt_id = "p" * 128
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": prompt_id,
        "uuid": "caveat-128",
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }
    command = {
        "type": "user",
        "promptId": prompt_id,
        "uuid": "command-128",
        "parentUuid": "caveat-128",
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    context: dict[str, object] = {}
    classify_provider_interaction(
        "claude",
        role="user",
        content_text=caveat["message"]["content"],
        raw_json=caveat,
        sequence_context=context,
    )
    result = classify_provider_interaction(
        "claude",
        role="user",
        content_text=command["message"]["content"],
        raw_json=command,
        sequence_context=context,
    )

    assert result["interaction_kind"] == INTERACTION_LOCAL_CONTROL


def test_claude_valid_uuid_parent_wins_when_prompt_id_is_unmatched() -> None:
    caveat = {
        "type": "user",
        "isMeta": True,
        "promptId": "known-prompt",
        "uuid": "caveat-parent",
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }
    child = {
        "type": "user",
        "promptId": "different-prompt",
        "uuid": "command-child",
        "parentUuid": "caveat-parent",
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    context: dict[str, object] = {}
    classify_provider_interaction(
        "claude",
        role="user",
        content_text=caveat["message"]["content"],
        raw_json=caveat,
        sequence_context=context,
    )
    result = classify_provider_interaction(
        "claude",
        role="user",
        content_text=child["message"]["content"],
        raw_json=child,
        sequence_context=context,
    )

    assert result["interaction_kind"] == INTERACTION_LOCAL_CONTROL


def test_storage_complete_window_closes_fully_reversed_claude_uuid_chain() -> None:
    caveat = {
        "type": "user",
        "isMeta": True,
        "uuid": "caveat-reversed",
        "message": {"role": "user", "content": "<local-command-caveat>native</local-command-caveat>"},
    }
    command = {
        "type": "user",
        "uuid": "command-reversed",
        "parentUuid": "caveat-reversed",
        "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
    }
    output = {
        "type": "user",
        "uuid": "output-reversed",
        "parentUuid": "command-reversed",
        "message": {"role": "user", "content": "<local-command-stdout>Set effort</local-command-stdout>"},
    }
    rows = [output, command, caveat]
    context: dict[str, object] = {}
    seed_provider_interaction_sequence_context("claude", rows, context)
    semantics = [
        classify_provider_interaction(
            "claude",
            role=row["message"]["role"],
            content_text=row["message"]["content"],
            raw_json=row,
            sequence_context=context,
        )
        for row in rows
    ]

    assert [row["interaction_kind"] for row in semantics] == [
        INTERACTION_LOCAL_CONTROL_OUTPUT,
        INTERACTION_LOCAL_CONTROL,
        INTERACTION_LOCAL_CONTROL,
    ]


def test_semantic_boundary_preserves_non_user_rows_and_drops_only_proven_controls() -> None:
    command = "<command-name>/effort</command-name><command-args>high</command-args>"
    raw_command = {
        "type": "user",
        "isMeta": True,
        "message": {"role": "user", "content": command},
    }

    assert semantic_event_included("claude", role="user", content_text=command, raw_json=raw_command) is False
    assert semantic_event_included("claude", role="assistant", content_text=command) is True
    assert semantic_event_included("codex", role="user", content_text="/custom-command") is True
