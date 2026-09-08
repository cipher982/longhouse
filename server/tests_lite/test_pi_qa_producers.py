from __future__ import annotations

import json

from zerg.qa.pi_console_tool_producer import pi_console_tool_assertions
from zerg.qa.pi_helm_lifecycle import _cleanup_receipt
from zerg.qa.pi_native import pi_native_model_evidence
from zerg.qa.pi_native import pi_native_shadow_taxonomy


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


def test_pi_console_tool_oracle_requires_complete_generic_lifecycle() -> None:
    assert pi_console_tool_assertions({"pi_tool_enabled": True})["pi_console_tool_enabled"] is False


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
