from __future__ import annotations

import pytest

from zerg.services.provider_capability_contract import ActionGate
from zerg.services.provider_capability_contract import CapabilityDisposition
from zerg.services.provider_capability_contract import LEGACY_FIELD_TO_SEMANTIC_CAPABILITY
from zerg.services.provider_capability_contract import ProductAction
from zerg.services.provider_capability_contract import RuntimeState
from zerg.services.provider_capability_contract import SEMANTIC_CAPABILITY_IDS
from zerg.services.provider_capability_contract import VerificationState
from zerg.services.provider_capability_contract import project_product_action
from zerg.services.provider_support_state import CONTRACT_OPERATIONS


def test_legacy_contract_operations_have_stable_semantic_ids() -> None:
    assert set(CONTRACT_OPERATIONS) <= set(LEGACY_FIELD_TO_SEMANTIC_CAPABILITY)
    assert set(LEGACY_FIELD_TO_SEMANTIC_CAPABILITY.values()) <= SEMANTIC_CAPABILITY_IDS
    assert LEGACY_FIELD_TO_SEMANTIC_CAPABILITY["run_once"] == "session.run_once"
    assert LEGACY_FIELD_TO_SEMANTIC_CAPABILITY["startup_coordination_context"] == "coordination.awareness.create"


@pytest.mark.parametrize(
    ("disposition", "verification", "runtime", "gate", "applicable", "expected"),
    [
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.PROVEN,
            RuntimeState.READY,
            ActionGate.STRICT,
            True,
            ProductAction.ENABLED,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.MISSING,
            RuntimeState.READY,
            ActionGate.STRICT,
            True,
            ProductAction.DISABLED,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.MISSING,
            RuntimeState.READY,
            ActionGate.WARN,
            True,
            ProductAction.ENABLED_WITH_WARNING,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.MISSING,
            RuntimeState.NOT_REQUIRED,
            ActionGate.CEILING,
            True,
            ProductAction.ENABLED,
        ),
        (
            CapabilityDisposition.UPSTREAM_ABSENT,
            VerificationState.PROVEN,
            RuntimeState.NOT_REQUIRED,
            ActionGate.CEILING,
            True,
            ProductAction.DISABLED,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.PROVEN,
            RuntimeState.UNHEALTHY,
            ActionGate.CEILING,
            True,
            ProductAction.DISABLED,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.PROVEN,
            RuntimeState.READY,
            ActionGate.STRICT,
            False,
            ProductAction.HIDDEN,
        ),
        (
            CapabilityDisposition.IMPLEMENTED,
            VerificationState.INAPPLICABLE,
            RuntimeState.READY,
            ActionGate.STRICT,
            True,
            ProductAction.HIDDEN,
        ),
    ],
)
def test_product_action_precedence(
    disposition: CapabilityDisposition,
    verification: VerificationState,
    runtime: RuntimeState,
    gate: ActionGate,
    applicable: bool,
    expected: ProductAction,
) -> None:
    assert (
        project_product_action(
            disposition=disposition,
            verification=verification,
            runtime=runtime,
            gate=gate,
            applicable=applicable,
        )
        is expected
    )
