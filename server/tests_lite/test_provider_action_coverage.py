from __future__ import annotations

from zerg.services.provider_action_coverage import OPENCODE_ORCHESTRATION_PROJECTION
from zerg.services.provider_action_coverage import ActionCoverageReasonCode
from zerg.services.provider_action_coverage import ActionCoverageState
from zerg.services.provider_action_coverage import derive_provider_action_coverage
from zerg.services.provider_action_coverage import derive_provider_action_coverage_from_artifact
from zerg.services.provider_action_coverage import provider_action_proof_results_from_artifact
from zerg.services.provider_action_coverage import serialize_provider_action_coverage


def test_contract_operations_are_derived_from_managed_provider_contracts():
    coverage = derive_provider_action_coverage("opencode")

    assert set(coverage) == {
        "observe_transcript",
        "observe_child_sessions",
        "classify_forks",
        "classify_subagents",
        "send_prompt",
        "send_async_prompt",
        "structured_question",
        "plan_approval",
        "abort",
        "reattach",
        "fork",
        "switch_actor",
        "background_task_status",
    }
    assert coverage["observe_transcript"].state == ActionCoverageState.SUPPORTED
    assert coverage["send_prompt"].state == ActionCoverageState.SUPPORTED
    assert coverage["send_prompt"].reason_code == ActionCoverageReasonCode.CONTRACT_PROVEN
    assert coverage["abort"].state == ActionCoverageState.SUPPORTED
    assert coverage["reattach"].state == ActionCoverageState.SUPPORTED
    assert coverage["send_async_prompt"].state == ActionCoverageState.UNKNOWN


def test_provider_specific_pause_actions_are_derived_without_manual_matrix():
    codex = derive_provider_action_coverage("codex")
    opencode = derive_provider_action_coverage("opencode")
    antigravity = derive_provider_action_coverage("antigravity")

    assert codex["structured_question"].state == ActionCoverageState.SUPPORTED
    assert codex["structured_question"].reason_code == ActionCoverageReasonCode.PROVIDER_PAUSE_ANSWER_SUPPORTED
    assert codex["plan_approval"].state == ActionCoverageState.SUPPORTED
    assert codex["plan_approval"].reason_code == ActionCoverageReasonCode.PROVIDER_PAUSE_ANSWER_SUPPORTED

    assert opencode["structured_question"].state == ActionCoverageState.READ_ONLY
    assert opencode["structured_question"].reason_code == ActionCoverageReasonCode.PROVIDER_PAUSE_DETECT_ONLY
    assert antigravity["structured_question"].state == ActionCoverageState.READ_ONLY
    assert antigravity["structured_question"].reason_code == ActionCoverageReasonCode.PROVIDER_PAUSE_DETECT_ONLY
    assert opencode["plan_approval"].state == ActionCoverageState.UNKNOWN
    assert opencode["plan_approval"].reason_code == ActionCoverageReasonCode.PROVIDER_SURFACE_UNPROVEN


def test_declared_rich_provider_gaps_have_specific_reason_codes():
    coverage = derive_provider_action_coverage("opencode")

    assert coverage["switch_actor"].state == ActionCoverageState.UNKNOWN
    assert coverage["switch_actor"].reason_code == ActionCoverageReasonCode.PROVIDER_GAP_DECLARED
    assert coverage["background_task_status"].state == ActionCoverageState.UNKNOWN
    assert coverage["background_task_status"].reason_code == ActionCoverageReasonCode.PROVIDER_GAP_DECLARED


def test_contract_false_operation_derives_unsupported_without_manual_matrix_cell():
    coverage = derive_provider_action_coverage("antigravity")

    assert coverage["abort"].state == ActionCoverageState.UNSUPPORTED
    assert coverage["abort"].reason_code == ActionCoverageReasonCode.CONTRACT_UNSUPPORTED
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
    assert coverage["observe_child_sessions"].state == ActionCoverageState.SUPPORTED
    assert coverage["classify_subagents"].reason_code == ActionCoverageReasonCode.REQUIRED_PROOF_PASSED


def test_missing_subagent_proof_derives_unknown_instead_of_stale_supported():
    coverage = derive_provider_action_coverage("opencode", proof_results={})

    assert coverage["classify_subagents"].state == ActionCoverageState.UNKNOWN
    assert coverage["classify_subagents"].reason_code == ActionCoverageReasonCode.REQUIRED_PROOF_MISSING


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
    assert coverage["classify_forks"].state == ActionCoverageState.READ_ONLY
    assert coverage["fork"].reason_code == ActionCoverageReasonCode.OBSERVATION_PROOF_PASSED


def test_universal_harness_artifact_results_feed_derived_coverage():
    artifact = {
        "artifact_kind": "universal_agent_harness_run",
        "provider": "opencode",
        "results": [
            {
                "provider": "opencode",
                "scenario": OPENCODE_ORCHESTRATION_PROJECTION,
                "status": "pass",
                "data": {
                    "scenario": OPENCODE_ORCHESTRATION_PROJECTION,
                    "assertions": {
                        "task_child_attached_to_primary_parent": True,
                        "nested_subagent_attached_to_subagent_parent": True,
                        "fork_remains_timeline_visible": True,
                    },
                },
            }
        ],
    }

    coverage = derive_provider_action_coverage_from_artifact(artifact)

    assert coverage["classify_subagents"].state == ActionCoverageState.SUPPORTED
    assert coverage["fork"].state == ActionCoverageState.READ_ONLY


def test_release_proof_operation_evidence_feeds_derived_coverage():
    artifact = {
        "artifact_kind": "provider_release_proof",
        "provider": "opencode",
        "operation_evidence": {
            "universal_opencode_subagent_projection": {"status": "pass", "level": "hermetic"},
            "universal_opencode_nested_subagent_projection": {"status": "pass", "level": "hermetic"},
            "universal_opencode_fork_projection": {"status": "pass", "level": "hermetic"},
        },
    }

    coverage = derive_provider_action_coverage_from_artifact(artifact)

    assert coverage["classify_subagents"].state == ActionCoverageState.SUPPORTED
    assert coverage["fork"].state == ActionCoverageState.READ_ONLY


def test_failed_release_proof_operation_evidence_stays_unknown():
    artifact = {
        "artifact_kind": "provider_release_proof",
        "provider": "opencode",
        "operation_evidence": {
            "universal_opencode_subagent_projection": {"status": "pass", "level": "hermetic"},
            "universal_opencode_nested_subagent_projection": {"status": "fail", "level": "hermetic"},
        },
    }

    coverage = derive_provider_action_coverage_from_artifact(artifact)

    assert coverage["classify_subagents"].state == ActionCoverageState.UNKNOWN


def test_nested_normalized_operation_evidence_is_accepted_without_file_io():
    artifact = {
        "artifact_kind": "provider_release_proof",
        "provider": "opencode",
        "normalized": {
            "operation_evidence": {
                "universal_opencode_fork_projection": {"status": "pass", "level": "hermetic"},
            }
        },
    }

    proofs = provider_action_proof_results_from_artifact(artifact)
    coverage = derive_provider_action_coverage_from_artifact(artifact)

    assert proofs[OPENCODE_ORCHESTRATION_PROJECTION]["assertions"]["fork_remains_timeline_visible"] is True
    assert coverage["fork"].state == ActionCoverageState.READ_ONLY


def test_serialized_coverage_has_stable_product_shape():
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

    serialized = serialize_provider_action_coverage(coverage)

    assert serialized["fork"] == {
        "id": "fork",
        "product_label": "Create fork",
        "state": "read_only",
        "reason_code": "observation_proof_passed",
        "reason": "Observation proof passed, but no Longhouse control contract exists.",
        "proof_refs": [
            {
                "scenario": OPENCODE_ORCHESTRATION_PROJECTION,
                "assertion": "fork_remains_timeline_visible",
            }
        ],
    }
