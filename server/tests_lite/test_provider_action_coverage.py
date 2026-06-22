from __future__ import annotations

from zerg.services.provider_action_coverage import ActionCoverageState
from zerg.services.provider_action_coverage import OPENCODE_ORCHESTRATION_PROJECTION
from zerg.services.provider_action_coverage import derive_provider_action_coverage


def test_contract_operations_are_derived_from_managed_provider_contracts():
    coverage = derive_provider_action_coverage("opencode")

    assert coverage["send_prompt"].state == ActionCoverageState.SUPPORTED
    assert coverage["abort"].state == ActionCoverageState.SUPPORTED
    assert coverage["reattach"].state == ActionCoverageState.SUPPORTED


def test_contract_false_operation_derives_unsupported_without_manual_matrix_cell():
    coverage = derive_provider_action_coverage("antigravity")

    assert coverage["abort"].state == ActionCoverageState.UNSUPPORTED
    assert coverage["reattach"].state == ActionCoverageState.UNSUPPORTED


def test_antigravity_send_prompt_follows_executable_contract_not_old_orchestration_label():
    coverage = derive_provider_action_coverage("antigravity")

    assert coverage["send_prompt"].state == ActionCoverageState.SUPPORTED


def test_opencode_subagent_support_is_derived_from_required_harness_assertions():
    coverage = derive_provider_action_coverage(
        "opencode",
        proof_results={
            OPENCODE_ORCHESTRATION_PROJECTION: {
                "assertions": {
                    "task_child_attached_to_primary_parent": True,
                    "nested_subagent_attached_to_subagent_parent": True,
                }
            }
        },
    )

    assert coverage["classify_subagents"].state == ActionCoverageState.SUPPORTED


def test_missing_subagent_proof_derives_unknown_instead_of_stale_supported():
    coverage = derive_provider_action_coverage("opencode", proof_results={})

    assert coverage["classify_subagents"].state == ActionCoverageState.UNKNOWN


def test_partial_subagent_proof_derives_unknown():
    coverage = derive_provider_action_coverage(
        "opencode",
        proof_results={
            OPENCODE_ORCHESTRATION_PROJECTION: {
                "assertions": {
                    "task_child_attached_to_primary_parent": True,
                    "nested_subagent_attached_to_subagent_parent": False,
                }
            }
        },
    )

    assert coverage["classify_subagents"].state == ActionCoverageState.UNKNOWN


def test_observed_fork_without_control_derives_read_only():
    coverage = derive_provider_action_coverage(
        "opencode",
        proof_results={
            OPENCODE_ORCHESTRATION_PROJECTION: {
                "assertions": {
                    "fork_remains_timeline_visible": True,
                }
            }
        },
    )

    assert coverage["fork"].state == ActionCoverageState.READ_ONLY
