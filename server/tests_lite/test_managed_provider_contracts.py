from __future__ import annotations

import pytest

from zerg.managed_provider_contract_manifest import _validate_operation_evidence
from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import contract_for_control_plane
from zerg.services.managed_provider_contracts import contract_for_provider
from zerg.services.managed_provider_contracts import control_plane_for_provider
from zerg.services.managed_provider_contracts import machine_control_capability_for_command
from zerg.services.managed_provider_contracts import machine_control_launch_capability_by_provider
from zerg.services.managed_provider_contracts import machine_control_operations_by_provider
from zerg.services.managed_provider_contracts import managed_provider_names
from zerg.services.managed_provider_contracts import managed_transport_for_control_plane
from zerg.services.managed_provider_contracts import provider_for_control_plane
from zerg.services.managed_provider_contracts import remote_launch_supported_providers
from zerg.services.managed_provider_contracts import steer_control_planes
from zerg.services.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.session_execution_home import ManagedSessionTransport


def _contract_snapshot():
    return {
        contract.provider: {
            "managed_transport": contract.managed_transport.value,
            "control_plane": contract.control_plane,
            "control_plane_aliases": contract.control_plane_aliases,
            "launch_local": contract.launch_local,
            "launch_remote": contract.launch_remote,
            "reattach": contract.reattach,
            "send_input": contract.send_input,
            "interrupt": contract.interrupt,
            "steer_active_turn": contract.steer_active_turn,
            "terminate": contract.terminate,
            "tail_output": contract.tail_output,
            "runtime_phase": contract.runtime_phase,
            "transcript_binding": contract.transcript_binding,
            "can_resume": contract.can_resume,
            "operation_evidence_levels": {
                operation: evidence.get("level") for operation, evidence in sorted(contract.operation_evidence.items())
            },
            "machine_control_supports": contract.machine_control_supports,
        }
        for contract in all_managed_provider_contracts()
    }


def _manifest_item(provider: str = "test") -> dict:
    return {
        "provider": provider,
        "launch_local": True,
        "launch_remote": True,
        "reattach": True,
        "send_input": True,
        "interrupt": True,
        "steer_active_turn": True,
        "terminate": True,
        "tail_output": True,
        "runtime_phase": True,
        "transcript_binding": True,
        "operation_evidence": {
            "launch_local": {"level": "hermetic", "source": "test"},
            "launch_remote": {"level": "hermetic", "source": "test"},
            "reattach": {"level": "hermetic", "source": "test"},
            "send_input": {"level": "hermetic", "source": "test"},
            "interrupt": {"level": "hermetic", "source": "test"},
            "steer_active_turn": {"level": "hermetic", "source": "test"},
            "terminate": {"level": "hermetic", "source": "test"},
            "tail_output": {"level": "hermetic", "source": "test"},
            "runtime_phase": {"level": "hermetic", "source": "test"},
            "transcript_binding": {"level": "hermetic", "source": "test"},
        },
    }


def test_managed_provider_contract_matrix_covers_launch_scope_providers():
    assert managed_provider_names() == frozenset({"codex", "claude", "opencode", "antigravity"})
    assert {contract.provider for contract in all_managed_provider_contracts()} == managed_provider_names()


def test_provider_cli_catalog_matches_managed_provider_contracts():
    assert set(PROVIDER_CLI_BINARY_BY_PROVIDER) == managed_provider_names()
    assert set(PROVIDER_CLI_ENV_BY_PROVIDER) == managed_provider_names()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda item: item.pop("operation_evidence"), "operation_evidence must be an object"),
        (lambda item: item["operation_evidence"].pop("send_input"), "operation_evidence missing send_input"),
        (
            lambda item: item["operation_evidence"].__setitem__("made_up", {"level": "none", "source": "x"}),
            "unknown keys made_up",
        ),
        (
            lambda item: item["operation_evidence"]["send_input"].__setitem__("level", "bogus"),
            "level must be one of",
        ),
        (
            lambda item: item["operation_evidence"]["send_input"].__setitem__("source", ""),
            "source must be a non-empty string",
        ),
        (
            lambda item: item["operation_evidence"]["send_input"].__setitem__("level", "none"),
            "supported operation send_input",
        ),
        (
            lambda item: (
                item.__setitem__("steer_active_turn", False),
                item["operation_evidence"]["steer_active_turn"].__setitem__("level", "hermetic"),
            ),
            "unsupported operation steer_active_turn",
        ),
        (
            lambda item: item["operation_evidence"]["send_input"].__setitem__("next", ""),
            "next must be a non-empty string",
        ),
    ],
)
def test_operation_evidence_validation_rejects_drift(mutator, message):
    item = _manifest_item()
    mutator(item)

    with pytest.raises(ValueError, match=message):
        _validate_operation_evidence(item)


def test_managed_provider_contract_manifest_snapshot():
    assert _contract_snapshot() == {
        "codex": {
            "managed_transport": "codex_app_server",
            "control_plane": "codex_bridge",
            "control_plane_aliases": ("codex_app_server",),
            "launch_local": True,
            "launch_remote": True,
            "reattach": True,
            "send_input": True,
            "interrupt": True,
            "steer_active_turn": True,
            "terminate": True,
            "tail_output": True,
            "runtime_phase": True,
            "transcript_binding": True,
            "can_resume": True,
            "operation_evidence_levels": {
                "interrupt": "hermetic",
                "launch_local": "live_no_token",
                "launch_remote": "live_no_token",
                "reattach": "live_no_token",
                "runtime_phase": "hermetic",
                "send_input": "hermetic",
                "steer_active_turn": "hermetic",
                "tail_output": "live_no_token",
                "terminate": "hermetic",
                "transcript_binding": "hermetic",
            },
            "machine_control_supports": (
                "codex.send",
                "codex.interrupt",
                "codex.steer",
                "codex.launch",
                "codex.continue",
            ),
        },
        "claude": {
            "managed_transport": "claude_channel_bridge",
            "control_plane": "claude_channel_bridge",
            "control_plane_aliases": (),
            "launch_local": True,
            "launch_remote": True,
            "reattach": True,
            "send_input": True,
            "interrupt": True,
            "steer_active_turn": True,
            "terminate": True,
            "tail_output": True,
            "runtime_phase": True,
            "transcript_binding": True,
            "can_resume": False,
            "operation_evidence_levels": {
                "interrupt": "hermetic",
                "launch_local": "live_no_token",
                "launch_remote": "source_review",
                "reattach": "hermetic",
                "runtime_phase": "hermetic",
                "send_input": "hermetic",
                "steer_active_turn": "manual_live_token",
                "tail_output": "hermetic",
                "terminate": "hermetic",
                "transcript_binding": "hermetic",
            },
            "machine_control_supports": ("claude.send", "claude.interrupt", "claude.steer", "claude.launch"),
        },
        "opencode": {
            "managed_transport": "opencode_server_bridge",
            "control_plane": "opencode_server_bridge",
            "control_plane_aliases": (),
            "launch_local": True,
            "launch_remote": True,
            "reattach": True,
            "send_input": True,
            "interrupt": True,
            "steer_active_turn": False,
            "terminate": True,
            "tail_output": True,
            "runtime_phase": True,
            "transcript_binding": True,
            "can_resume": False,
            "operation_evidence_levels": {
                "interrupt": "manual_live_token",
                "launch_local": "live_no_token",
                "launch_remote": "hermetic",
                "reattach": "live_no_token",
                "runtime_phase": "hermetic",
                "send_input": "manual_live_token",
                "steer_active_turn": "none",
                "tail_output": "hermetic",
                "terminate": "hermetic",
                "transcript_binding": "manual_live_token",
            },
            "machine_control_supports": ("opencode.send", "opencode.interrupt", "opencode.launch"),
        },
        "antigravity": {
            "managed_transport": "antigravity_hook_inbox",
            "control_plane": "antigravity_hook_inbox",
            "control_plane_aliases": (),
            "launch_local": True,
            "launch_remote": False,
            "reattach": False,
            "send_input": True,
            "interrupt": False,
            "steer_active_turn": False,
            "terminate": False,
            "tail_output": True,
            "runtime_phase": True,
            "transcript_binding": True,
            "can_resume": False,
            "operation_evidence_levels": {
                "interrupt": "none",
                "launch_local": "live_no_token",
                "launch_remote": "none",
                "reattach": "none",
                "runtime_phase": "hermetic",
                "send_input": "manual_live_token",
                "steer_active_turn": "none",
                "tail_output": "hermetic",
                "terminate": "none",
                "transcript_binding": "hermetic",
            },
            "machine_control_supports": ("antigravity.send",),
        },
    }


@pytest.mark.parametrize(
    ("provider", "transport", "control_plane"),
    [
        ("codex", ManagedSessionTransport.CODEX_APP_SERVER, "codex_bridge"),
        ("claude", ManagedSessionTransport.CLAUDE_CHANNEL_BRIDGE, "claude_channel_bridge"),
        ("opencode", ManagedSessionTransport.OPENCODE_SERVER_BRIDGE, "opencode_server_bridge"),
        ("antigravity", ManagedSessionTransport.ANTIGRAVITY_HOOK_INBOX, "antigravity_hook_inbox"),
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
    assert codex.machine_control_supports == (
        "codex.send",
        "codex.interrupt",
        "codex.steer",
        "codex.launch",
        "codex.continue",
    )
    assert remote_launch_supported_providers() == frozenset({"codex", "claude", "opencode"})


def test_claude_contract_is_first_class_channel_control_provider():
    claude = contract_for_provider("claude")

    assert claude is not None
    assert claude.launch_local is True
    assert claude.launch_remote is True
    assert claude.send_input is True
    assert claude.interrupt is True
    assert claude.steer_active_turn is True
    assert claude.operation_evidence_for("steer_active_turn")["level"] == "manual_live_token"
    assert "scheduled live token canary" in claude.operation_evidence_for("steer_active_turn")["next"]
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
        "can_resume": 0,
    }


def test_antigravity_contract_is_hook_inbox_send_only():
    provider = "antigravity"
    contract = contract_for_provider(provider)

    assert contract is not None
    assert contract.launch_local is True
    assert contract.launch_remote is False
    assert contract.send_input is True
    assert contract.interrupt is False
    assert contract.steer_active_turn is False
    assert contract.tail_output is True
    assert contract.runtime_phase is True
    assert contract.transcript_binding is True
    assert contract.operation_evidence_for("send_input")["level"] == "manual_live_token"
    assert contract.operation_evidence_for("steer_active_turn")["level"] == "none"
    assert contract.machine_control_supports == ("antigravity.send",)
    assert contract.connection_capabilities == {
        "can_send_input": 1,
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
    assert "antigravity_hook_inbox" not in steer_control_planes()
    assert "antigravity_process" not in steer_control_planes()
    assert managed_transport_for_control_plane("opencode_process") == ManagedSessionTransport.OPENCODE_PROCESS
    assert provider_for_control_plane("opencode_process") == "opencode"
    assert managed_transport_for_control_plane("antigravity_process") == ManagedSessionTransport.ANTIGRAVITY_PROCESS
    assert provider_for_control_plane("antigravity_process") == "antigravity"
    assert "opencode_process" not in trusted_non_runner_control_planes()
    assert "antigravity_process" not in trusted_non_runner_control_planes()
    assert "antigravity_hook_inbox" in trusted_non_runner_control_planes()


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
        ("antigravity", "session.send_text", "antigravity.send"),
        ("antigravity", "session.interrupt", None),
        ("antigravity", "session.steer_text", None),
    ],
)
def test_machine_control_capability_for_command_uses_provider_contract(provider, command_type, capability):
    assert machine_control_capability_for_command(provider, command_type) == capability


def test_machine_control_launch_capability_map_comes_from_provider_contracts():
    assert machine_control_launch_capability_by_provider() == {
        "codex": "codex.launch",
        "claude": "claude.launch",
        "opencode": "opencode.launch",
    }


def test_machine_control_operations_by_provider_projects_live_supports():
    assert machine_control_operations_by_provider(
        [
            "claude.launch",
            "claude.steer",
            "codex.send",
            "codex.launch",
            "unknown.launch",
        ],
        connected=True,
    ) == {
        "codex": ("send", "launch"),
        "claude": ("steer", "launch"),
    }


def test_machine_control_operations_by_provider_requires_connected_channel():
    assert machine_control_operations_by_provider(["codex.launch", "antigravity.send"], connected=False) == {}


def test_provider_cli_discovery_contract_comes_from_managed_provider_manifest():
    assert PROVIDER_CLI_BINARY_BY_PROVIDER == {
        "codex": "codex",
        "claude": "claude",
        "opencode": "opencode",
        "antigravity": "agy",
    }
    assert PROVIDER_CLI_ENV_BY_PROVIDER == {
        "codex": "LONGHOUSE_CODEX_BIN",
        "claude": None,
        "opencode": "LONGHOUSE_OPENCODE_BIN",
        "antigravity": "LONGHOUSE_ANTIGRAVITY_BIN",
    }
