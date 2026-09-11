from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.pi_console_tool_producer import REGISTRATION as PI_CONSOLE_REGISTRATION
from zerg.qa.pi_console_tool_producer import pi_console_tool_assertions
from zerg.qa.pi_helm_lifecycle import _cleanup_receipt
from zerg.qa.pi_helm_lifecycle import _write_scenario_receipts
from zerg.qa.pi_helm_lifecycle import _native_snapshot
from zerg.qa.pi_helm_lifecycle import _redact_value
from zerg.qa.pi_helm_lifecycle import _retain_source
from zerg.qa.pi_helm_lifecycle import _state_identity
from zerg.qa.pi_helm_lifecycle import pi_helm_lifecycle_assertions
from zerg.qa.pi_native import pi_native_model_evidence
from zerg.qa.pi_native import pi_native_shadow_taxonomy
from zerg.qa.pi_native import pi_transcript_rows


def test_helm_retained_evidence_excludes_live_channel_authority() -> None:
    secret = "ephemeral-control-authority"
    state = {"session_id": "session", "run_id": "run", "channel_token": secret}
    retained = _redact_value({"owner": _state_identity(state), "frame": {"auth_token": secret, "session_id": "session"}}, [])
    assert secret not in json.dumps(retained)
    assert "channel_token" not in retained["owner"]
    assert "auth_token" not in retained["frame"]


def test_helm_authoritative_retention_does_not_truncate_large_source(tmp_path) -> None:
    source = tmp_path / "native.jsonl"
    source.write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    retained = _retain_source(tmp_path / "evidence", source, "native.raw", [], require_complete=True)

    assert retained["retained"] is True
    assert retained["complete"] is True
    assert retained["truncated"] is False
    assert retained["size_exceeds_evidence_bound"] is True
    assert (tmp_path / "evidence" / "source-artifacts" / "native.raw").stat().st_size == source.stat().st_size


def test_helm_auth_failure_is_not_hidden_as_a_convergence_timeout(monkeypatch) -> None:
    from zerg.qa import pi_helm_lifecycle as helm

    def rejected(*args):
        raise helm._RuntimeHostHTTPError(403, "access denied")

    monkeypatch.setattr(helm, "_runtime_snapshot", rejected)
    with pytest.raises(helm._RuntimeHostHTTPError) as error:
        helm._wait_runtime_convergence("https://runtime.invalid", "fixture", "session", "native", "marker", timeout=1)
    assert error.value.status == 403


def test_pi_native_taxonomy_pairs_native_tool_call_and_result() -> None:
    rows = [
        {"type": "assistant", "role": "assistant", "tool_call_id": "call-1"},
        {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1"},
        {"type": "assistant", "role": "assistant", "tool_call_id": "call-without-result"},
    ]
    taxonomy = pi_native_shadow_taxonomy(
        rows,
        {
            "has_header": True,
            "provider_session_id": "session-1",
            "native_shapes": {
                "session": 1,
                "message/assistant/text+thinking+toolCall": 1,
                "message/toolResult": 1,
            },
        },
    )

    assert taxonomy["source"] == "pi_native_session_jsonl"
    assert taxonomy["tool_pairs"] == ["call-1"]
    assert taxonomy["tool_calls_without_results"] == ["call-without-result"]
    assert taxonomy["tool_results_without_calls"] == []


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1", "tool_name": "read"},
            {"type": "assistant", "role": "assistant", "tool_call_id": "call-1", "tool_name": "read"},
        ],
        [
            {"type": "assistant", "role": "assistant", "tool_call_id": "call-1", "tool_name": "read"},
            {"type": "tool_result", "role": "toolresult", "tool_call_id": "call-1", "tool_name": "write"},
        ],
    ],
)
def test_pi_native_taxonomy_rejects_reordered_or_mismatched_tool_pairs(rows) -> None:
    taxonomy = pi_native_shadow_taxonomy(
        rows,
        {"native_shapes": {}, "has_header": True, "provider_session_id": "session-1"},
    )

    assert taxonomy["tool_pairs"] == []
    assert taxonomy["tool_calls_without_results"] == ["call-1"]
    assert taxonomy["tool_results_without_calls"] == ["call-1"]


def test_pi_console_tool_oracle_requires_complete_generic_lifecycle() -> None:
    assert pi_console_tool_assertions({"pi_tool_enabled": True})["pi_console_tool_enabled"] is False


def test_pi_console_tool_oracle_requires_context_recall() -> None:
    observation = {
        "adapter_dispatch_started": True,
        "qualification_model_bound": True,
        "stock_provider_response_bound": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "no_orphan_provider_processes": True,
        "pi_tool_enabled": True,
        "continuation_context_recalled": False,
    }

    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is False
    observation["continuation_context_recalled"] = True
    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is True


def test_pi_console_tool_oracle_rejects_unpaired_native_tool_evidence() -> None:
    observation = {
        "adapter_dispatch_started": True,
        "qualification_model_bound": True,
        "stock_provider_response_bound": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "no_orphan_provider_processes": True,
        "pi_tool_enabled": False,
    }
    assert pi_console_tool_assertions(observation)["pi_console_tool_enabled"] is False


def _write_native_tool_session(
    path,
    *,
    result_call_id: str | None = "call-1",
    include_result: bool = True,
    assistant_final_id: str = "assistant-final-1",
) -> None:
    events = [
        {"type": "session", "id": "native-session-1"},
        {
            "type": "message",
            "id": "assistant-tool-1",
            "message": {
                "role": "assistant",
                "model": "fixture-model",
                "content": [{"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "proof.txt"}}],
                "stopReason": "toolUse",
            },
        },
    ]
    if include_result:
        events.append(
            {
                "type": "message",
                "id": "tool-result-1",
                "message": {
                    "role": "toolResult",
                    "toolCallId": result_call_id,
                    "toolName": "read",
                    "content": [{"type": "text", "text": "PI_CONSOLE_PROOF"}],
                    "isError": False,
                },
            }
        )
    events.append(
        {
            "type": "message",
            "id": assistant_final_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "LH_MARKER"}], "stopReason": "stop"},
        }
    )
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _native_tool_receipt_inputs(native_source):
    identity = {
        "provider": "pi",
        "session_id": "session-1",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "prompt_digest": "sha256:" + "a" * 64,
        "provider_thread_id": "native-session-1",
    }
    return {
        "native_source": native_source,
        "provider_response_source": native_source,
        "inspection": {
            "linked_tool_call_ids": ["call-1"],
            "output_marker_observed": True,
            "native_shapes": {"tool_execution_start": 1, "tool_execution_end": 1},
        },
        "dispatch": {"status": "pass", **identity},
        "binding": {
            "status": "pass",
            **identity,
            "marker": "LH_MARKER",
            "provider_response_source_kind": "stdout_path",
            "provider_response_source_sha256": "sha256:" + "b" * 64,
            "provider_response_marker_count": 1,
            "bound_assistant_event_id": "event-1",
            "bound_assistant_event_origin": "durable",
        },
        "binary_receipt": {"provider": "pi", "path": "/opt/pi", "sha256": "sha256:" + "c" * 64, "version": "0.85.1"},
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "process_stop_verified": True,
            "source_retention_verified": True,
        },
        "marker": "LH_MARKER",
    }


def test_pi_native_tool_receipt_binds_actual_call_result_session_and_response(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source)

    receipt = lifecycle._pi_native_tool_receipt(
        **_native_tool_receipt_inputs(native_source),
        native_retained_path="native.jsonl",
        provider_response_retained_path="native.jsonl",
    )

    assert receipt["status"] == "pass"
    assert receipt["provider_session_id"] == "native-session-1"
    assert receipt["native_model"] == "fixture-model"
    assert receipt["tool_call"]["id"] == "call-1"
    assert receipt["tool_call"]["name"] == "read"
    assert receipt["tool_call"]["arguments"] == {"path": "proof.txt"}
    assert receipt["tool_call"]["native_message_id"] == "assistant-tool-1"
    assert receipt["tool_result"]["id"] == "tool-result-1"
    assert receipt["tool_result"]["tool_call_id"] == "call-1"
    assert receipt["tool_result"]["result"] == [{"type": "text", "text": "PI_CONSOLE_PROOF"}]
    assert receipt["provider_response"]["native_message_id"] == "assistant-final-1"
    assert receipt["provider_response"]["retained_source_path"] == str(native_source)
    assert receipt["provider_response"]["retained_path"] == "native.jsonl"
    assert receipt["native_source"]["retained_path"] == "native.jsonl"
    assert receipt["linkage"] == {
        "live_inspection_confirmed": True,
        "native_session_matches_provider_thread": True,
        "native_response_follows_tool_result": True,
        "provider_response_bound": True,
        "native_projected_assistant_linkage": True,
        "native_message_id_preserved": True,
        "projected_assistant_event_id_preserved": True,
        "runtime_identity_matches": True,
        "cleanup_pass": True,
    }


@pytest.mark.parametrize(
    ("result_call_id", "include_result"),
    [("call-other", True), (None, False)],
)
def test_pi_native_tool_receipt_rejects_missing_or_mismatched_native_pair(tmp_path, result_call_id, include_result) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source, result_call_id=result_call_id, include_result=include_result)

    receipt = lifecycle._pi_native_tool_receipt(**_native_tool_receipt_inputs(native_source))

    assert receipt["status"] == "fail"
    assert "native_tool_call_result_pair_missing" in receipt["failure_reasons"]


def test_pi_native_tool_receipt_accepts_native_projected_id_equality(tmp_path) -> None:
    native_source = tmp_path / "native.jsonl"
    _write_native_tool_session(native_source, assistant_final_id="event-1")

    receipt = lifecycle._pi_native_tool_receipt(**_native_tool_receipt_inputs(native_source))

    assert receipt["status"] == "pass"
    assert "native_projected_assistant_id_collision" not in receipt["failure_reasons"]
    assert receipt["linkage"]["native_projected_assistant_linkage"] is True


def test_pi_console_contract_revision_advances_with_native_receipt() -> None:
    registration = PI_CONSOLE_REGISTRATION.to_dict()

    assert registration["producer_revision"] == 4
    assert registration["scenario_revision"] == 4
    assert "native_tool_receipt" in registration["required_artifacts"]


def test_pi_helm_cleanup_oracle_does_not_accept_missing_process_identity() -> None:
    receipt = _cleanup_receipt(
        [
            {"label": "launcher", "pid": -1, "pgid": -1, "birth_matches": False},
            {"label": "provider", "pid": -1, "pgid": -1, "birth_matches": False},
        ]
    )
    assert receipt["status"] == "fail"
    assert receipt["provider_process_dead"] is False
    assert receipt["no_orphan_provider_processes"] is False


def test_pi_native_snapshot_contract_keeps_metadata_and_taxonomy_distinct(tmp_path) -> None:
    native = tmp_path / "session.jsonl"
    native.write_text(
        '{"type":"session","id":"native-session"}\n'
        '{"type":"message","id":"reply","message":{"role":"assistant","model":"fixture-model","content":"reply"}}\n',
        encoding="utf-8",
    )

    rows, metadata, taxonomy = _native_snapshot(native)

    assert rows
    assert metadata["provider_session_id"] == "native-session"
    assert metadata["source_end_offset"] == native.stat().st_size
    assert taxonomy["provider_session_id"] == "native-session"
    assert taxonomy["source"] == "pi_native_session_jsonl"


def test_pi_native_parser_and_taxonomy_account_for_every_tool_call_in_one_message(tmp_path) -> None:
    native = tmp_path / "multi-tool.jsonl"
    events = [
        {"type": "session", "id": "native-session"},
        {
            "type": "message",
            "id": "assistant-tools",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "call-read", "name": "read", "arguments": {"path": "a"}},
                    {"type": "toolCall", "id": "call-write", "name": "write", "arguments": {"path": "b"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "result-read",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-read",
                "toolName": "read",
                "content": [{"type": "text", "text": "read result"}],
            },
        },
        {
            "type": "message",
            "id": "result-write",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-write",
                "toolName": "write",
                "content": [{"type": "text", "text": "write result"}],
            },
        },
    ]
    native.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    rows, _session_id, metadata = pi_transcript_rows(native)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)

    assistant = next(row for row in rows if row.get("type") == "assistant")
    assert assistant["tool_call_ids"] == ["call-read", "call-write"]
    assert assistant["tool_call_count"] == 2
    assert taxonomy["tool_call_ids"] == ["call-read", "call-write"]
    assert taxonomy["tool_result_ids"] == ["call-read", "call-write"]
    assert taxonomy["tool_pairs"] == ["call-read", "call-write"]
    assert taxonomy["tool_calls_without_results"] == []
    assert taxonomy["tool_results_without_calls"] == []
    assert taxonomy["shadow_classes"]["transcript:assistant_tool"] == 1
    assert taxonomy["shadow_classes"]["provider_tool:result"] == 2
    assert [fact["call_arguments"] for fact in taxonomy["tool_pair_facts"]] == [{"path": "a"}, {"path": "b"}]


def test_pi_native_thinking_plus_text_fixture_preserves_both_blocks(tmp_path) -> None:
    native = tmp_path / "thinking-text.jsonl"
    native.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "native-session"}),
                json.dumps(
                    {
                        "type": "message",
                        "id": "assistant-thinking-text",
                        "message": {
                            "role": "assistant",
                            "model": "fixture-model",
                            "provider": "fixture-provider",
                            "content": [
                                {"type": "thinking", "thinking": "reason first"},
                                {"type": "text", "text": "final answer"},
                            ],
                            "stopReason": "stop",
                            "usage": {"output": 1},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows, _session_id, metadata = pi_transcript_rows(native)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)
    assistant = next(row for row in rows if row.get("type") == "assistant")

    assert assistant["text"] == "final answer"
    assert assistant["thinking"] == ["reason first"]
    assert metadata["native_shapes"]["message/assistant/text+thinking"] == 1
    assert taxonomy["shadow_classes"]["transcript:assistant_reasoning"] == 1


def test_pi_helm_receipts_round_trip_runtime_binding_nesting(tmp_path) -> None:
    args = SimpleNamespace(provider_version="0.85.1", provider_bin="/opt/pi")
    binding = {"one_session": True, "one_thread": True, "provider_session_bound": True}
    observations = {
        "provider_binary_sha256": "sha256:" + "a" * 64,
        "helm_registration_receipt": {"status": "pass"},
        "native_session_receipt": {"native_file": {"present": True}},
        "native_taxonomy_receipt": {"status": "pass"},
        "control_receipts": {"send": {"accepted": True}},
        "reload_rebind_receipt": {
            "status": "pass",
            "runtime": {"binding": binding, "served_controls": True},
        },
        "session_replacement_receipt": {
            "status": "pass",
            "binding": {"legacy_top_level": True},
            "runtime": {"served_controls": True},
        },
        "replacement_binding": binding,
        "cold_resume_receipt": {"status": "pass"},
        "stale_owner_receipt": {"status": "pass"},
    }

    _write_scenario_receipts(tmp_path, args, observations, [])

    reload_receipt = json.loads((tmp_path / "reload-rebind-receipt.json").read_text(encoding="utf-8"))
    replacement_receipt = json.loads((tmp_path / "session-replacement-receipt.json").read_text(encoding="utf-8"))
    assert reload_receipt["runtime"]["binding"] == binding
    assert replacement_receipt["runtime"]["binding"] == binding
    assert "binding" not in replacement_receipt


def test_pi_helm_observation_booleans_cannot_override_failed_cleanup() -> None:
    observation = {
        "abort_native": True,
        "terminate_owned": True,
        "cleanup": {
            "status": "fail",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 1,
        },
    }

    assertions = pi_helm_lifecycle_assertions(observation)

    assert assertions["pi_helm_abort_native"] is False
    assert assertions["pi_helm_terminate_owned"] is False


def test_pi_helm_cleanup_stop_and_scratch_failures_block_admission() -> None:
    observation = {
        "abort_native": True,
        "terminate_owned": True,
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "shipper_stop_verified": False,
            "scratch_removed": False,
            "cleanup_errors": ["shipper stop failed"],
        },
    }

    assertions = pi_helm_lifecycle_assertions(observation)

    assert assertions["pi_helm_abort_native"] is False
    assert assertions["pi_helm_terminate_owned"] is False


def test_pi_helm_steer_oracle_requires_active_state_before_native_control() -> None:
    observation = {
        "steer_active": True,
        "control_receipts": {
            "steer": {
                "accepted": True,
                "native": {"assistant_marker_rows": 1},
                "runtime": {
                    "active_turn_observed": False,
                    "active_state": {"phase": "idle"},
                },
            }
        },
    }

    assert pi_helm_lifecycle_assertions(observation)["pi_helm_steer_active"] is False
    observation["control_receipts"]["steer"]["runtime"] = {
        "active_turn_observed": True,
        "active_state": {"phase": "running"},
    }
    assert pi_helm_lifecycle_assertions(observation)["pi_helm_steer_active"] is True


def test_pi_helm_follow_up_oracle_requires_active_native_input_delivery() -> None:
    observation = {
        "follow_up_native": True,
        "control_receipts": {
            "follow_up": {
                "accepted": True,
                "command": {
                    "method": "POST",
                    "text": "After this turn, reply with PI_HELM_FOLLOW_fixture.",
                },
                "native": {
                    "marker": "PI_HELM_FOLLOW_fixture",
                    "user_marker_rows": 1,
                    "user_marker_occurrences": 1,
                    "follow_up_delivered": True,
                },
                "runtime": {"active_turn_observed": True},
            }
        },
    }

    assert pi_helm_lifecycle_assertions(observation)["pi_helm_follow_up_native"] is False
    observation["control_receipts"]["follow_up"]["command"]["path"] = "/api/agents/sessions/session/send-live"
    assert pi_helm_lifecycle_assertions(observation)["pi_helm_follow_up_native"] is True

    observation["control_receipts"]["follow_up"]["native"]["user_marker_rows"] = 0
    assert pi_helm_lifecycle_assertions(observation)["pi_helm_follow_up_native"] is False


def test_pi_helm_cold_resume_oracle_requires_remembered_context_evidence() -> None:
    observation = {
        "cold_resume_exact_file": True,
        "cold_resume_receipt": {
            "context_evidence": {
                "phrase": "PI_HELM_CONTEXT_fixture",
                "source": "pre_termination_replacement_turn",
                "resume_prompt": "Reply with the context you remember followed by PI_HELM_RESUME_fixture.",
                "pre_termination": {
                    "source": "pre_termination_replacement_turn",
                    "prompt": "Remember PI_HELM_CONTEXT_fixture.",
                    "native_user_marker_rows": 1,
                    "native_user_marker_occurrences": 1,
                },
                "post_resume_native": {
                    "marker": "PI_HELM_CONTEXT_fixture",
                    "assistant_marker_rows": 1,
                    "user_marker_rows": 0,
                    "user_marker_occurrences": 0,
                },
                "post_resume_runtime": {
                    "marker": "PI_HELM_CONTEXT_fixture",
                    "assistant_marker_count": 1,
                    "binding": {"one_session": True, "one_thread": True, "provider_session_bound": True},
                },
            }
        },
    }

    assert pi_helm_lifecycle_assertions(observation)["pi_helm_cold_resume_exact_file"] is True
    observation["cold_resume_receipt"]["context_evidence"]["source"] = "proof_file"
    assert pi_helm_lifecycle_assertions(observation)["pi_helm_cold_resume_exact_file"] is False


def test_pi_accounting_includes_tool_rounds_but_never_hides_a_failed_tail(tmp_path) -> None:
    transcript = tmp_path / "native.jsonl"
    events = [
        {"type": "session", "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        {
            "type": "message",
            "id": "tool-round",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "read-1", "name": "read", "arguments": {}}],
                "stopReason": "toolUse",
                "model": "fixture-model",
                "usage": {"input": 8, "output": 2, "cost": {"total": 0.2}},
            },
        },
        {
            "type": "message",
            "id": "final-reply",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The tool finished."}],
                "stopReason": "stop",
                "model": "fixture-model",
                "usage": {"input": 12, "output": 3, "cost": {"total": 0.3}},
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    evidence = pi_native_model_evidence(transcript, source_canary="fixture", api_key_configured=True)
    assert evidence is not None
    assert evidence["result_event"]["usage"]["input"] == 20
    assert evidence["result_event"]["usage"]["output"] == 5
    assert evidence["result_event"]["total_cost_usd"] == 0.5

    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": "failed-next-turn",
                    "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "provider failed"},
                }
            )
            + "\n"
        )
    assert pi_native_model_evidence(transcript, source_canary="fixture", api_key_configured=True) is None
