"""Executable managed-provider control contracts.

This registry is intentionally small. It centralizes what each provider can
truthfully support today without pretending the provider mechanics are generic.
Provider-specific launch/control code still owns how an operation runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field

from zerg.managed_provider_contract_manifest import managed_provider_contract_items
from zerg.session_execution_home import ManagedSessionTransport

COMMAND_INTERRUPT = "session.interrupt"
COMMAND_SEND_TEXT = "session.send_text"
COMMAND_STEER_TEXT = "session.steer_text"

_MACHINE_CONTROL_OPERATION_BY_COMMAND = {
    COMMAND_SEND_TEXT: "send",
    COMMAND_INTERRUPT: "interrupt",
    COMMAND_STEER_TEXT: "steer",
}


@dataclass(frozen=True)
class ManagedProviderContract:
    provider: str
    managed_transport: ManagedSessionTransport
    control_plane: str
    control_plane_aliases: tuple[str, ...] = ()
    launch_local: bool = True
    launch_remote: bool = False
    reattach: bool = False
    send_input: bool = False
    interrupt: bool = False
    steer_active_turn: bool = False
    terminate: bool = False
    tail_output: bool = True
    runtime_phase: bool = True
    transcript_binding: bool = True
    can_resume: bool = False
    # Expected machine-control channel operation names. The engine still owns
    # the live supports[] handshake; this field documents the provider ceiling.
    machine_control_supports: tuple[str, ...] = ()
    # Per-operation evidence is intentionally separate from the support flag.
    # A provider can be first-class by design while still carrying a lower proof
    # level until scheduled live canaries promote the evidence.
    operation_evidence: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @property
    def control_planes(self) -> tuple[str, ...]:
        return (self.control_plane, *self.control_plane_aliases)

    @property
    def connection_capabilities(self) -> dict[str, int]:
        return {
            "can_send_input": int(self.send_input),
            "can_interrupt": int(self.interrupt),
            "can_terminate": int(self.terminate),
            "can_tail_output": int(self.tail_output),
            "can_resume": int(self.can_resume),
        }

    def operation_evidence_for(self, operation: str) -> Mapping[str, str]:
        return self.operation_evidence.get(operation, {})


def _contract_from_manifest_item(item: dict[str, object]) -> ManagedProviderContract:
    return ManagedProviderContract(
        provider=str(item["provider"]),
        managed_transport=ManagedSessionTransport(str(item["managed_transport"])),
        control_plane=str(item["control_plane"]),
        control_plane_aliases=tuple(str(value) for value in item.get("control_plane_aliases") or ()),
        launch_local=bool(item.get("launch_local", True)),
        launch_remote=bool(item.get("launch_remote", False)),
        reattach=bool(item.get("reattach", False)),
        send_input=bool(item.get("send_input", False)),
        interrupt=bool(item.get("interrupt", False)),
        steer_active_turn=bool(item.get("steer_active_turn", False)),
        terminate=bool(item.get("terminate", False)),
        tail_output=bool(item.get("tail_output", True)),
        runtime_phase=bool(item.get("runtime_phase", True)),
        transcript_binding=bool(item.get("transcript_binding", True)),
        can_resume=bool(item.get("can_resume", False)),
        machine_control_supports=tuple(str(value) for value in item.get("machine_control_supports") or ()),
        operation_evidence={
            str(operation): {str(key): str(value) for key, value in dict(evidence).items()}
            for operation, evidence in dict(item.get("operation_evidence") or {}).items()
            if isinstance(evidence, dict)
        },
    )


_CONTRACTS: tuple[ManagedProviderContract, ...] = tuple(_contract_from_manifest_item(item) for item in managed_provider_contract_items())

_BY_PROVIDER = {contract.provider: contract for contract in _CONTRACTS}
_BY_CONTROL_PLANE = {control_plane: contract for contract in _CONTRACTS for control_plane in contract.control_planes}
_LEGACY_CONTROL_PLANE_PROVIDERS = {
    "opencode_process": "opencode",
    "antigravity_process": "antigravity",
}
_LEGACY_CONTROL_PLANE_TRANSPORTS = {
    "opencode_process": ManagedSessionTransport.OPENCODE_PROCESS,
    "antigravity_process": ManagedSessionTransport.ANTIGRAVITY_PROCESS,
}


def all_managed_provider_contracts() -> tuple[ManagedProviderContract, ...]:
    return _CONTRACTS


def managed_provider_names() -> frozenset[str]:
    return frozenset(_BY_PROVIDER)


def contract_for_provider(provider: str | None) -> ManagedProviderContract | None:
    return _BY_PROVIDER.get(str(provider or "").strip().lower())


def require_contract_for_provider(provider: str | None) -> ManagedProviderContract:
    contract = contract_for_provider(provider)
    if contract is None:
        raise ValueError(f"Unsupported managed-local provider: {provider}")
    return contract


def contract_for_control_plane(control_plane: str | None) -> ManagedProviderContract | None:
    return _BY_CONTROL_PLANE.get(str(control_plane or "").strip())


def managed_transport_for_provider(provider: str | None) -> ManagedSessionTransport:
    return require_contract_for_provider(provider).managed_transport


def managed_transport_for_control_plane(control_plane: str | None) -> ManagedSessionTransport | None:
    normalized = str(control_plane or "").strip()
    if normalized in _LEGACY_CONTROL_PLANE_TRANSPORTS:
        return _LEGACY_CONTROL_PLANE_TRANSPORTS[normalized]
    contract = contract_for_control_plane(control_plane)
    return contract.managed_transport if contract is not None else None


def control_plane_for_provider(provider: str | None) -> str:
    return require_contract_for_provider(provider).control_plane


def provider_for_control_plane(control_plane: str | None) -> str | None:
    normalized = str(control_plane or "").strip()
    if normalized in _LEGACY_CONTROL_PLANE_PROVIDERS:
        return _LEGACY_CONTROL_PLANE_PROVIDERS[normalized]
    contract = contract_for_control_plane(control_plane)
    return contract.provider if contract is not None else None


def remote_launch_supported_providers() -> frozenset[str]:
    return frozenset(contract.provider for contract in _CONTRACTS if contract.launch_remote)


def machine_control_launch_capability_by_provider() -> dict[str, str]:
    return {
        contract.provider: f"{contract.provider}.launch"
        for contract in _CONTRACTS
        if f"{contract.provider}.launch" in contract.machine_control_supports
    }


def machine_control_operations_by_provider(
    supports: Iterable[str],
    *,
    connected: bool,
) -> dict[str, tuple[str, ...]]:
    """Project live machine-control supports into provider operation names.

    The raw ``supports[]`` list remains the transport handshake. This helper is
    the shared read model for UI/agent surfaces that need to know the actual
    operations a connected machine can perform without hardcoding provider
    allowlists.
    """
    if not connected:
        return {}

    support_set = {str(item).strip() for item in supports if str(item).strip()}
    operations_by_provider: dict[str, tuple[str, ...]] = {}
    for contract in _CONTRACTS:
        operations = tuple(
            capability.split(".", 1)[1]
            for capability in contract.machine_control_supports
            if capability in support_set and "." in capability
        )
        if operations:
            operations_by_provider[contract.provider] = operations
    return operations_by_provider


def steer_control_planes() -> frozenset[str]:
    control_planes: list[str] = []
    for contract in _CONTRACTS:
        if contract.steer_active_turn:
            control_planes.extend(contract.control_planes)
    return frozenset(control_planes)


def trusted_non_runner_control_planes() -> frozenset[str]:
    return frozenset(control_plane for contract in _CONTRACTS for control_plane in contract.control_planes)


def machine_control_capability_for_command(provider: str | None, command_type: str | None) -> str | None:
    contract = contract_for_provider(provider)
    operation = _MACHINE_CONTROL_OPERATION_BY_COMMAND.get(str(command_type or "").strip())
    if contract is None or operation is None:
        return None
    capability = f"{contract.provider}.{operation}"
    return capability if capability in contract.machine_control_supports else None
