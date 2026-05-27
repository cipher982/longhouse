from __future__ import annotations

import pytest

from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import contract_for_control_plane
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import control_plane_for_provider
from zerg.services.managed_provider_contracts import machine_control_capability_for_command
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
from zerg.services.managed_provider_contracts import provider_for_control_plane
from zerg.services.managed_provider_contracts import remote_launch_supported_providers
from zerg.services.managed_provider_contracts import steer_control_planes
from zerg.services.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.session_execution_home import ManagedSessionTransport


def test_managed_provider_contract_matrix_covers_launch_scope_providers():
    assert managed_provider_names() == frozenset({"codex", "claude", "opencode", "antigravity"})
    assert {contract.provider for contract in all_managed_provider_contracts()} == managed_provider_names()


@pytest.mark.parametrize(
    ("provider", "transport", "control_plane"),
    [
        ("codex", ManagedSessionTransport.CODEX_APP_SERVER, "codex_bridge"),
        ("claude", ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE, "claude_channel_bridge"),
        ("opencode", ManagedSessionTransport.OPENCODE_SERVER_BRIDGE, "opencode_server_bridge"),
        ("antigravity", ManagedSessionTransport.ANTIGRAVITY_PROCESS, "antigravity_process"),
    ],
)
def test_provider_contract_maps_transport_and_control_plane(provider, transport, control_plane):
    contract = contract_for_provider(provider)

    assert contract is not None
    assert contract.managed_transport == transport
    assert contract.control_plane == control_plane
    assert control_plane_for_provider(provider) == control_plane
    assert ManagedSessionTransport.for_provider(provider) == transport
    assert managed_transport_for_control_plane(control_plane) == transport


def test_codex_contract_is_current_remote_launch_engine_channel_provider():
    codex = contract_for_provider("codex")

    assert codex is not None
    assert codex.launch_local is True
    assert codex.launch_remote is True
    assert codex.send_input is True
    assert codex.interrupt is True
    assert codex.steer_active_turn is True
    assert codex.machine_control_supports == ("codex.send", "codex.interrupt", "codex.steer", "codex.launch")
    assert remote_launch_supported_providers() == frozenset({"codex", "claude", "opencode"})


def test_claude_contract_is_first_class_channel_control_provider():
    claude = contract_for_provider("claude")

    assert claude is not None
    assert claude.launch_local is True
    assert claude.launch_remote is True
    assert claude.send_input is True
    assert claude.interrupt is True
    assert claude.steer_active_turn is True
    assert claude.machine_control_supports == ("claude.send", "claude.interrupt", "claude.steer", "claude.launch")


def test_opencode_contract_is_server_bridge_control_provider_without_active_turn_steer():
    opencode = contract_for_provider("opencode")

    assert opencode is not None
    assert opencode.launch_local is True
    assert opencode.launch_remote is True
    assert opencode.send_input is True
    assert opencode.interrupt is True
    assert opencode.steer_active_turn is False
    assert opencode.machine_control_supports == ("opencode.send", "opencode.interrupt", "opencode.launch")
    assert opencode.connection_capabilities == {
        "can_send_input": 1,
        "can_interrupt": 1,
        "can_terminate": 1,
        "can_tail_output": 1,
        "can_resume": 1,
    }


def test_antigravity_process_wrapper_is_observe_only_until_named_control_plane_lands():
    provider = "antigravity"
    contract = contract_for_provider(provider)

    assert contract is not None
    assert contract.launch_local is True
    assert contract.launch_remote is False
    assert contract.send_input is False
    assert contract.interrupt is False
    assert contract.steer_active_turn is False
    assert contract.tail_output is True
    assert contract.runtime_phase is True
    assert contract.transcript_binding is True
    assert contract.connection_capabilities == {
        "can_send_input": 0,
        "can_interrupt": 0,
        "can_terminate": 0,
        "can_tail_output": 1,
        "can_resume": 0,
    }


def test_control_plane_aliases_are_explicit_contract_not_scattered_literals():
    codex = contract_for_control_plane("codex_app_server")

    assert codex is not None
    assert codex.provider == "codex"
    assert provider_for_control_plane("codex_app_server") == "codex"
    assert "codex_app_server" in steer_control_planes()
    assert "claude_channel_bridge" in steer_control_planes()
    assert "opencode_server_bridge" not in steer_control_planes()
    assert "opencode_process" not in steer_control_planes()
    assert "antigravity_process" not in steer_control_planes()
    assert managed_transport_for_control_plane("opencode_process") == ManagedSessionTransport.OPENCODE_PROCESS
    assert provider_for_control_plane("opencode_process") == "opencode"
    assert "opencode_process" not in trusted_non_runner_control_planes()


@pytest.mark.parametrize(
    ("provider", "command_type", "capability"),
    [
        ("codex", "session.send_text", "codex.send"),
        ("codex", "session.interrupt", "codex.interrupt"),
        ("codex", "session.steer_text", "codex.steer"),
        ("claude", "session.send_text", "claude.send"),
        ("claude", "session.interrupt", "claude.interrupt"),
        ("claude", "session.steer_text", "claude.steer"),
        ("opencode", "session.send_text", "opencode.send"),
        ("opencode", "session.interrupt", "opencode.interrupt"),
        ("opencode", "session.steer_text", None),
        ("antigravity", "session.send_text", None),
    ],
)
def test_machine_control_capability_for_command_uses_provider_contract(provider, command_type, capability):
    assert machine_control_capability_for_command(provider, command_type) == capability
