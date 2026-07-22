"""Provider-independent capability semantics and product action projection.

Provider-specific adapters still own execution. This module freezes the shared
vocabulary used while the legacy managed-provider booleans remain in service as
the compatibility implementation ceiling.
"""

from __future__ import annotations

from enum import StrEnum


class CapabilityDisposition(StrEnum):
    IMPLEMENTED = "implemented"
    NOT_IMPLEMENTED = "not_implemented"
    UPSTREAM_ABSENT = "upstream_absent"
    POLICY_DISABLED = "policy_disabled"


class VerificationState(StrEnum):
    PROVEN = "proven"
    MISSING = "missing"
    STALE = "stale"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    INAPPLICABLE = "inapplicable"


class RuntimeState(StrEnum):
    READY = "ready"
    NOT_REQUIRED = "not_required"
    UNAVAILABLE = "unavailable"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ActionGate(StrEnum):
    CEILING = "ceiling"
    WARN = "warn"
    STRICT = "strict"


class ProductAction(StrEnum):
    ENABLED = "enabled"
    ENABLED_WITH_WARNING = "enabled_with_warning"
    DISABLED = "disabled"
    HIDDEN = "hidden"


SEMANTIC_CAPABILITY_IDS = frozenset(
    {
        "session.launch",
        "session.turn.start",
        "session.run_once",
        "session.resume",
        "session.reattach",
        "session.input.send_idle",
        "session.input.steer_active",
        "session.interrupt.active",
        "session.pause.answer",
        "session.terminate",
        "session.transcript.tail",
        "session.transcript.bind",
        "session.runtime.phase",
        "coordination.awareness.create",
        "coordination.awareness.post_compaction",
        "coordination.message.send",
        "coordination.message.receive",
    }
)

# This is a migration map, not evidence. A true legacy boolean establishes only
# the compatibility implementation ceiling for the corresponding semantic ID.
# In particular, startup_coordination_context does not prove awareness after
# resume/compaction or any messaging semantic.
LEGACY_FIELD_TO_SEMANTIC_CAPABILITY = {
    "launch_local": "session.launch",
    "turn_start": "session.turn.start",
    "run_once": "session.run_once",
    "can_resume": "session.resume",
    "reattach": "session.reattach",
    "send_input": "session.input.send_idle",
    "steer_active_turn": "session.input.steer_active",
    "interrupt": "session.interrupt.active",
    "answer_pause": "session.pause.answer",
    "terminate": "session.terminate",
    "tail_output": "session.transcript.tail",
    "transcript_binding": "session.transcript.bind",
    "runtime_phase": "session.runtime.phase",
    "startup_coordination_context": "coordination.awareness.create",
}

SEMANTIC_CAPABILITY_TO_LEGACY_FIELD = {capability: field for field, capability in LEGACY_FIELD_TO_SEMANTIC_CAPABILITY.items()}


def project_product_action(
    *,
    disposition: CapabilityDisposition,
    verification: VerificationState,
    runtime: RuntimeState,
    gate: ActionGate,
    applicable: bool = True,
) -> ProductAction:
    """Apply the normative state-to-affordance precedence table."""

    if not applicable or verification is VerificationState.INAPPLICABLE:
        return ProductAction.HIDDEN
    if disposition is not CapabilityDisposition.IMPLEMENTED:
        return ProductAction.DISABLED
    if runtime not in {RuntimeState.READY, RuntimeState.NOT_REQUIRED}:
        return ProductAction.DISABLED
    if gate is ActionGate.STRICT and verification is not VerificationState.PROVEN:
        return ProductAction.DISABLED
    if gate is ActionGate.WARN and verification is not VerificationState.PROVEN:
        return ProductAction.ENABLED_WITH_WARNING
    return ProductAction.ENABLED
