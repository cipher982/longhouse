from __future__ import annotations

from zerg.qa.omp_console_producer import ASSERTION_ID as CONSOLE_ASSERTION
from zerg.qa.omp_console_producer import REGISTRATION as CONSOLE_REGISTRATION
from zerg.qa.omp_console_producer import omp_console_assertions
from zerg.qa.omp_helm_lifecycle import ASSERTIONS as HELM_ASSERTIONS
from zerg.qa.omp_helm_lifecycle import REGISTRATION as HELM_REGISTRATION
from zerg.qa.omp_helm_lifecycle import omp_helm_lifecycle_assertions
from zerg.qa.provider_qualification import _PROFILES


def test_omp_qualification_producers_are_registered_on_their_own_contracts() -> None:
    assert CONSOLE_REGISTRATION.producer_id == "omp.console_lifecycle.v1"
    assert CONSOLE_REGISTRATION.providers == ("omp",)
    assert CONSOLE_REGISTRATION.scenario_id == "omp_console_lifecycle"
    assert HELM_REGISTRATION.producer_id == "omp.helm_lifecycle.v1"
    assert HELM_REGISTRATION.providers == ("omp",)
    assert HELM_REGISTRATION.scenario_id == "omp_helm_lifecycle"
    assert ("omp", "omp_print_v1") in _PROFILES
    assert ("omp", "omp_helm_v1") in _PROFILES


def test_omp_console_settlement_requires_native_agent_end_and_archive() -> None:
    observation = {
        "runtime_host_turn_dispatch": True,
        "omp_settlement": {
            "agent_end_terminal": True,
            "stream_drained": True,
            "native_archive_bound": True,
        },
        "no_orphan_provider_processes": True,
    }

    assert omp_console_assertions(observation) == {CONSOLE_ASSERTION: True}
    observation["omp_settlement"]["agent_end_terminal"] = False
    assert omp_console_assertions(observation)[CONSOLE_ASSERTION] is False


def test_omp_helm_assertions_do_not_use_agent_settled_as_completion() -> None:
    observation = {
        "omp_native_extension_channel_bound": True,
        "omp_agent_end_settlement_observed": True,
        "omp_native_archive_bound": True,
        "send_idle": True,
        "steer_active": True,
        "abort_native": True,
        "terminate_owned": True,
        "cold_resume_exact_file": True,
        "stale_owner_refused": True,
        "native_replacement_bound": True,
        "settlement": {
            "agent_end_terminal": True,
            "native_archive_bound": True,
            "agent_settled_is_not_completion_contract": True,
        },
        "cleanup": {
            "status": "pass",
            "provider_process_dead": True,
            "process_group_dead": True,
            "orphan_count": 0,
        },
    }

    assert set(omp_helm_lifecycle_assertions(observation)) == set(HELM_ASSERTIONS)
    assert all(omp_helm_lifecycle_assertions(observation).values())
    observation["settlement"]["agent_end_terminal"] = False
    assert omp_helm_lifecycle_assertions(observation)["omp_helm_abort_native"] is False
