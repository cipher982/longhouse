from __future__ import annotations
import json


from zerg.qa.omp_console_producer import ASSERTION_ID as CONSOLE_ASSERTION
from zerg.qa.omp_console_producer import REGISTRATION as CONSOLE_REGISTRATION
from zerg.qa.omp_console_producer import omp_console_assertions
from zerg.qa.omp_helm_lifecycle import ASSERTIONS as HELM_ASSERTIONS
from zerg.qa.omp_helm_lifecycle import REGISTRATION as HELM_REGISTRATION
from zerg.qa.omp_helm_lifecycle import _native_settlement
from zerg.qa.omp_helm_lifecycle import _exact_session_retirement
from zerg.qa.omp_helm_lifecycle import omp_helm_lifecycle_assertions
from zerg.qa.provider_qualification import _PROFILES


def test_omp_qualification_producers_are_registered_on_their_own_contracts() -> None:
    assert CONSOLE_REGISTRATION.producer_id == "omp.console_lifecycle.v1"
    assert CONSOLE_REGISTRATION.producer_revision == 4
    assert CONSOLE_REGISTRATION.providers == ("omp",)
    assert CONSOLE_REGISTRATION.scenario_id == "omp_console_lifecycle"
    assert CONSOLE_REGISTRATION.scenario_revision == 4
    assert "console_continuation_receipt" in CONSOLE_REGISTRATION.required_artifacts
    assert HELM_REGISTRATION.producer_id == "omp.helm_lifecycle.v1"
    assert HELM_REGISTRATION.producer_revision == 4
    assert HELM_REGISTRATION.scenario_revision == 4
    assert HELM_REGISTRATION.providers == ("omp",)
    assert HELM_REGISTRATION.scenario_id == "omp_helm_lifecycle"
    assert ("omp", "omp_print_v1") in _PROFILES
    assert ("omp", "omp_helm_v1") in _PROFILES

def test_omp_helm_settlement_requires_terminal_channel_evidence(tmp_path) -> None:
    session_file = tmp_path / "omp.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "id": "native-1"}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    state = {
        "status": "ready",
        "phase": "running",
        "native_session_id": "native-1",
        "session_file": str(session_file),
        "agent_end_observed": True,
        "agent_end_is_terminal": True,
        "agent_end_will_continue": False,
        "updated_at": "2026-09-10T00:00:00Z",
    }
    assert _native_settlement(session_file, channel_state=state, native_session_id="native-1")["status"] == "fail"
    state["phase"] = "idle"
    assert _native_settlement(session_file, channel_state=state, native_session_id="native-1")["status"] == "pass"



def test_omp_console_settlement_and_context_recall_are_required() -> None:
    observation = {
        "runtime_host_turn_dispatch": True,
        "exact_session_thread_run_binding": True,
        "transcript_converged_exactly_once": True,
        "omp_continuation_context_recalled": True,
        "omp_settlement": {
            "agent_end_terminal": True,
            "agent_end_evidence_shape": True,
            "provider_response_source_bound": True,
            "provider_response_source_kind": "stdout_path",
            "stream_drained": True,
            "native_archive_bound": True,
            "native_session_id_bound": True,
            "native_terminal_after_assistant": True,
            "native_marker_count": 1,
            "native_tool_evidence_complete": True,
            "malformed_source": False,
        },
        "no_orphan_provider_processes": True,
        "interrupt_contract_preserved": True,
        "post_interrupt_sendable": True,
        "canary_session_hidden": True,
    }

    assert omp_console_assertions(observation) == {CONSOLE_ASSERTION: True}
    observation["omp_continuation_context_recalled"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False
    observation["omp_continuation_context_recalled"] = True
    observation["omp_settlement"]["agent_end_terminal"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False

def test_omp_helm_assertions_do_not_use_agent_settled_as_completion() -> None:
    observation = {
        "observation_scope": "scenario",
        "omp_native_extension_channel_bound": True,
        "omp_agent_end_settlement_observed": True,
        "omp_native_archive_bound": True,
        "send_idle": True,
        "follow_up_native": True,
        "steer_active": True,
        "abort_native": True,
        "terminate_owned": True,
        "cold_resume_exact_file": True,
        "stale_owner_refused": True,
        "native_replacement_bound": True,
        "settlement": {
            "status": "pass",
            "agent_end_terminal": True,
            "agent_end_evidence_shape": True,
            "native_session_header_count": 1,
            "native_terminal_after_assistant": True,
            "malformed_source": False,
            "native_archive_bound": True,
            "agent_settled_is_not_completion_contract": True,
        },
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "canary_session_hidden": True,
            "isolation_removed": True,
        },
        "channel_binding": {
            "ready": True,
            "session_id_present": True,
            "native_session_id_present": True,
            "connection_id_present": True,
            "lease_generation_present": True,
            "session_file_present": True,
        },
        "send_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "follow_up_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "steer_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "cold_resume_evidence": {
            "native_source_bound": True,
            "channel_terminal_bound": True,
            "marker_count": 1,
            "terminal": True,
            "exact_file": True,
        },
        "replacement_evidence": {"native_source_bound": True, "marker_count": 1, "channel_ack_bound": True},
        "stale_owner_evidence": {"error_code": "stale_channel"},
        "abort_evidence": {
            "channel_source_bound": True,
            "terminal": True,
            "channel_ack_bound": True,
        },
    }

    assert set(omp_helm_lifecycle_assertions(observation)) == set(HELM_ASSERTIONS)
    assert all(omp_helm_lifecycle_assertions(observation).values())
    observation["abort_evidence"]["terminal"] = False
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_abort_native"] is False


def test_omp_helm_oracle_rejects_archive_only_or_unbound_evidence() -> None:
    observation = {
        "omp_native_extension_channel_bound": True,
        "send_idle": True,
        "steer_active": True,
        "abort_native": True,
        "terminate_owned": True,
        "cold_resume_exact_file": True,
        "stale_owner_refused": True,
        "native_replacement_bound": True,
        "settlement": {"agent_end_terminal": True, "native_archive_bound": True},
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
            "canary_session_hidden": False,
        },
    }

    assert all(value is False for value in omp_helm_lifecycle_assertions(observation).values())


def test_omp_console_oracle_requires_exact_native_settlement_and_retirement() -> None:
    observation = {
        "runtime_host_turn_dispatch": True,
        "omp_continuation_context_recalled": True,
        "omp_settlement": {
            "agent_end_terminal": True,
            "stream_drained": True,
            "native_archive_bound": True,
            "native_session_id_bound": False,
            "native_terminal_after_assistant": False,
            "native_marker_count": 0,
        },
        "no_orphan_provider_processes": True,
        "canary_session_hidden": False,
    }

    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False


def test_omp_cleanup_retirement_is_bound_to_the_exact_hidden_archived_session() -> None:
    assert not _exact_session_retirement({"status": "pass", "hidden": True}, "session-1")
    assert not _exact_session_retirement(
        {
            "status": "pass",
            "session_id": "other-session",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
        "session-1",
    )
    assert _exact_session_retirement(
        {
            "status": "pass",
            "session_id": "session-1",
            "hidden": True,
            "archived": True,
            "present_in_served_inventory": False,
        },
        "session-1",
    )
