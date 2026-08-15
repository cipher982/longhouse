from __future__ import annotations

from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_hatch_execution_contract
from zerg.services.internal_sessions import is_provider_product_canary_marker
from zerg.services.managed_local_launcher import ManagedLocalLaunchParams
from zerg.services.managed_local_launcher import build_managed_local_launch_plan


def test_cursor_product_canary_marker_is_exact_and_bounded():
    marker = "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7"

    assert is_provider_product_canary_marker(marker)
    assert classify_provider_proof_environment(first_user_text=marker) == "test"
    assert not is_provider_product_canary_marker("Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_not-hex")
    assert classify_provider_proof_environment(first_user_text="Please fix LONGHOUSE_CURSOR_PRODUCT_ONE_91b38069e7") is None


def test_hatch_execution_contract_is_exact_and_automation_classified():
    contract = (
        "Hatch execution contract:\n"
        "This is a single bounded, non-interactive run. A human is waiting for a useful answer."
    )

    assert is_hatch_execution_contract(contract)
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
