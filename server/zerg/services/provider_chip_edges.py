"""Landing chip -> factory proof edges, and the certification rollup.

Stdlib only: `scripts/generate/provider_capabilities_ts.py` imports this with
plain `python3` to light the *covered* chip set at build time, and the Runtime
Host imports it to roll published proofs up into the *certified* chip set it
serves. One definition of which assertions stand behind a chip, two layers
that never merge. See control-plane docs/specs/provider-chip-proof-graph.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

CHIPS: tuple[str, ...] = ("launchAndSend", "interrupt", "steerMidTurn", "resume", "search")

# Chip -> the contract operations whose factory proof lights it. A chip is only
# as proven as every edge behind it; runtime booleans and implementation
# dispositions stay authoritative for control and are never read here.
CHIP_OPERATIONS: dict[str, tuple[str, ...]] = {
    "launchAndSend": ("launch_local", "send_input"),
    "interrupt": ("interrupt", "terminate"),
    "steerMidTurn": ("steer_active_turn",),
}
# Chips proven by a capability rather than an operation.
CHIP_CAPABILITIES: dict[str, str] = {
    "resume": "session.resume.helm",
    "search": "session.transcript.search",
}

CERTIFIED = "certified"
UNVERIFIED = "unverified"
STALE = "stale"
FAILING = "failing"
UNPROVEN = "unproven"
CERTIFICATION_STATES = (CERTIFIED, UNVERIFIED, STALE, FAILING, UNPROVEN)


def _live(assertion: object) -> bool:
    return isinstance(assertion, Mapping) and list(assertion.get("acceptable_evidence") or ()) == ["live_token"]


def _live_token_proof(assertions: object) -> bool:
    """Every required assertion demands a real provider turn.

    A `live_no_token` cell proves a binary's surface, not that a user's
    instruction produced provider work, so it cannot light a chip.
    """

    return isinstance(assertions, list) and bool(assertions) and all(_live(item) for item in assertions)


def operation_assertions(provider: Mapping[str, Any], operation: str) -> tuple[Mapping[str, Any], ...] | None:
    evidence = (provider.get("operation_evidence") or {}).get(operation) or {}
    assertions = evidence.get("required_assertions")
    if provider.get(operation) is not True or evidence.get("disposition") != "implemented":
        return None
    if not _live_token_proof(assertions):
        return None
    return tuple(assertions)


def capability_assertions(provider: Mapping[str, Any], capability: str) -> tuple[Mapping[str, Any], ...] | None:
    """A capability lights its chip through its live-token assertions.

    Capabilities legitimately pair live cells with hermetic invariants (resume
    idempotency, single owner); those still gate the capability in the factory
    but are not what a user-visible claim rests on.
    """

    declaration = (provider.get("capabilities") or {}).get(capability) or {}
    live = [item for item in declaration.get("required_assertions") or [] if _live(item)]
    if declaration.get("disposition") != "implemented" or not live:
        return None
    return tuple(live)


def operation_proven(provider: Mapping[str, Any], operation: str) -> bool:
    return operation_assertions(provider, operation) is not None


def capability_proven(provider: Mapping[str, Any], capability: str) -> bool:
    return capability_assertions(provider, capability) is not None


def chip_edges(provider: Mapping[str, Any]) -> dict[str, tuple[Mapping[str, Any], ...] | None]:
    """Chip -> the live-token assertions behind it; None when not covered."""

    edges: dict[str, tuple[Mapping[str, Any], ...] | None] = {}
    for chip in CHIPS:
        if chip in CHIP_OPERATIONS:
            collected: list[Mapping[str, Any]] = []
            for operation in CHIP_OPERATIONS[chip]:
                assertions = operation_assertions(provider, operation)
                if assertions is None:
                    collected = []
                    break
                collected.extend(assertions)
            edges[chip] = tuple(collected) if collected else None
        else:
            edges[chip] = capability_assertions(provider, CHIP_CAPABILITIES[chip])
    if provider.get("can_resume") is not True:
        edges["resume"] = None
    return edges


def chip_states(provider: Mapping[str, Any]) -> dict[str, bool]:
    """The *covered* layer: a declared live-token test exists for every edge."""

    return {chip: assertions is not None for chip, assertions in chip_edges(provider).items()}


def requirement_key(provider: str, assertion: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
    return (provider, str(assertion["scenario_id"]), str(assertion["id"]), assertion.get("variant"))


def rollup_state(proof_statuses: Iterable[str | None]) -> str:
    """Roll the per-requirement projection statuses up into one chip state.

    `pass` means the projection found a currently admissible proof, which it
    keeps even when a newer run failed, so a red candidate does not revoke a
    released claim until that pass ages out (`stale`). With no admissible pass,
    a semantic failure is `failing`; infrastructure errors, blocked, skipped,
    never-proven and unacceptable evidence are `unverified`, not failures.
    """

    statuses = list(proof_statuses)
    if statuses and all(status == "pass" for status in statuses):
        return CERTIFIED
    if "semantic_fail" in statuses:
        return FAILING
    if "stale" in statuses:
        return STALE
    return UNVERIFIED


__all__ = [
    "CERTIFICATION_STATES",
    "CERTIFIED",
    "CHIPS",
    "CHIP_CAPABILITIES",
    "CHIP_OPERATIONS",
    "FAILING",
    "STALE",
    "UNPROVEN",
    "UNVERIFIED",
    "capability_assertions",
    "chip_edges",
    "chip_states",
    "capability_proven",
    "operation_assertions",
    "operation_proven",
    "requirement_key",
    "rollup_state",
]
