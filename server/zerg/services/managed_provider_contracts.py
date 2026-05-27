"""Executable managed-provider control contracts.

This registry is intentionally small. It centralizes what each provider can
truthfully support today without pretending the provider mechanics are generic.
Provider-specific launch/control code still owns how an operation runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from zerg.session_execution_home import ManagedSessionTransport


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


_CONTRACTS: tuple[ManagedProviderContract, ...] = (
    ManagedProviderContract(
        provider="codex",
        managed_transport=ManagedSessionTransport.CODEX_APP_SERVER,
        control_plane="codex_bridge",
        control_plane_aliases=("codex_app_server",),
        launch_remote=True,
        reattach=True,
        send_input=True,
        interrupt=True,
        steer_active_turn=True,
        terminate=True,
        can_resume=True,
        machine_control_supports=("codex.send", "codex.interrupt", "codex.steer", "codex.launch"),
    ),
    ManagedProviderContract(
        provider="claude",
        managed_transport=ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE,
        control_plane="claude_channel_bridge",
        reattach=True,
        send_input=True,
        interrupt=True,
        steer_active_turn=True,
        terminate=True,
        can_resume=True,
    ),
    ManagedProviderContract(
        provider="opencode",
        managed_transport=ManagedSessionTransport.OPENCODE_PROCESS,
        control_plane="opencode_process",
    ),
    ManagedProviderContract(
        provider="antigravity",
        managed_transport=ManagedSessionTransport.ANTIGRAVITY_PROCESS,
        control_plane="antigravity_process",
    ),
)

_BY_PROVIDER = {contract.provider: contract for contract in _CONTRACTS}
_BY_CONTROL_PLANE = {control_plane: contract for contract in _CONTRACTS for control_plane in contract.control_planes}


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
    contract = contract_for_control_plane(control_plane)
    return contract.managed_transport if contract is not None else None


def control_plane_for_provider(provider: str | None) -> str:
    return require_contract_for_provider(provider).control_plane


def provider_for_control_plane(control_plane: str | None) -> str | None:
    contract = contract_for_control_plane(control_plane)
    return contract.provider if contract is not None else None


def remote_launch_supported_providers() -> frozenset[str]:
    return frozenset(contract.provider for contract in _CONTRACTS if contract.launch_remote)


def steer_control_planes() -> frozenset[str]:
    return frozenset(control_plane for contract in _CONTRACTS if contract.steer_active_turn for control_plane in contract.control_planes)


def trusted_non_runner_control_planes() -> frozenset[str]:
    return frozenset(control_plane for contract in _CONTRACTS for control_plane in contract.control_planes)
