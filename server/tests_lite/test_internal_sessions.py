from __future__ import annotations

from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_hatch_execution_contract
from zerg.services.internal_sessions import is_provider_coordination_awareness_marker
from zerg.services.internal_sessions import is_provider_factory_cwd
from zerg.services.internal_sessions import is_provider_factory_machine_id
from zerg.services.internal_sessions import is_provider_product_canary_marker
from zerg.services.internal_sessions import is_provider_reply_exact_marker
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan


def test_cursor_product_canary_marker_is_exact_and_bounded():
    marker = "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7"

    assert is_provider_product_canary_marker(marker)
    assert classify_provider_proof_environment(first_user_text=marker) == "test"
    assert not is_provider_product_canary_marker("Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_not-hex")
    assert classify_provider_proof_environment(first_user_text="Please fix LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7") is None


def test_provider_reply_exact_marker_is_bounded_to_longhouse_canary_shapes():
    marker = "Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_94afb881e8684faca669fefd44ec40 and nothing else."

    assert is_provider_reply_exact_marker(marker)
    assert is_provider_reply_exact_marker("Reply with exactly LONGHOUSE_CLAUDE_TURN_BOUNDARY_27eeb18bb0b349b0b1778e83a51c7b6e and nothing else.")
    assert classify_provider_proof_environment(first_user_text=marker) == "test"
    assert is_provider_reply_exact_marker("Reply exactly LONGHOUSE_CODEX_COLD_RESUME_SEED_8ee711c900c448f18c7762b3fa0c649c")
    assert is_provider_reply_exact_marker("Reply exactly FRESH_AFTER_CANCEL_OK.")
    assert not is_provider_reply_exact_marker("Reply exactly OK")
    assert not is_provider_reply_exact_marker("Please reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_abc123")


def test_provider_factory_evidence_workspace_is_automation_classified_without_hiding_user_repos():
    assert is_provider_factory_cwd(
        "/var/lib/provider-factory/artifacts/_assurance/executions/run-1/cursor/process_loss/evidence/cursor-workspace"
    )
    assert is_provider_factory_cwd("/tmp/live-cell-run-cursor.coordination.directed.v1-abc123/evidence/cursor-workspace")
    assert is_provider_factory_machine_id("provider-factory-resume")
    assert is_provider_coordination_awareness_marker("print exactly LONGHOUSE_CURSOR_COORD_AWARENESS_f70043f7b0")
    assert classify_provider_proof_environment(
        cwd="/tmp/lhx-claude-coord-create-abc123/workspace"
    ) == "test"
    assert classify_provider_proof_environment(machine_id="provider-factory-resume") == "test"
    assert not is_provider_factory_cwd("/Users/davidrose/git/control-plane/provider_factory")


def test_hatch_execution_contract_is_exact_and_automation_classified():
    contract = (
        "Hatch execution contract:\n"
        "This is a single bounded, non-interactive run. A human is waiting for a useful answer."
    )

    assert is_hatch_execution_contract(contract)
    assert is_hatch_execution_contract(
        "Hatch execution contract:\n"
        "This is a single bounded, non-interactive run with a time budget of about 15 minutes."
    )
    assert not is_hatch_execution_contract("Hatch execution contract: please help me write one")


def test_managed_canary_launch_carries_hidden_provenance_without_hiding_normal_helm():
    canary = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="provider-factory-resume",
            cwd="/tmp/longhouse-provider-runtime/sandbox-home/canaries/provider-live/cursor/workspace",
            provider="cursor",
            project="managed-local",
            machine_name="provider-factory-resume",
        )
    )
    human = build_managed_local_launch_plan(
        ManagedLocalLaunchParams(
            owner_id=42,
            runner_target="cinder",
            cwd="/Users/davidrose/git/zerg/longhouse",
            provider="codex",
            project="longhouse",
            machine_name="cinder",
        )
    )

    assert (canary.environment, canary.origin_kind, canary.hidden_from_default_timeline) == (
        "test",
        "test_or_canary",
        1,
    )
    assert (human.environment, human.origin_kind, human.hidden_from_default_timeline) == ("development", None, 0)
