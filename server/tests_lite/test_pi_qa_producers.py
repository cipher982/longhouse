from __future__ import annotations

from zerg.qa.pi_console_tool_producer import pi_console_tool_assertions
from zerg.qa.pi_helm_lifecycle import _cleanup_receipt
from zerg.qa.provider_adapters.pi import pi_native_shadow_taxonomy


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
