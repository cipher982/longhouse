from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from zerg.managed_provider_contract_manifest import _validate_machine_control_supports
from zerg.managed_provider_contract_manifest import _validate_operation_evidence
from zerg.managed_provider_contract_manifest import normalize_contract_manifest
from zerg.managed_provider_contract_manifest import render_contract_manifest_json
from zerg.provider_cli_contract import PROVIDER_CLI_BINARY_BY_PROVIDER
from zerg.provider_cli_contract import PROVIDER_CLI_ENV_BY_PROVIDER
from zerg.services.managed_provider_contracts import _contracts_by_control_plane
from zerg.services.managed_provider_contracts import all_managed_provider_contracts
from zerg.services.managed_provider_contracts import continue_supported_providers
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
from zerg.services.managed_provider_contracts import run_once_supported_providers
from zerg.services.managed_provider_contracts import steer_control_planes
from zerg.services.managed_provider_contracts import trusted_non_runner_control_planes
from zerg.services.session_kernel_projection import direct_machine_control_planes
from zerg.session_execution_home import ManagedSessionTransport


def _manifest_item(provider: str = "test") -> dict:
    return {
        "provider": provider,
        "launch_local": True,
        "launch_remote": True,
        "reattach": True,
        "send_input": True,
        "interrupt": True,
        "steer_active_turn": True,
        "answer_pause": True,
        "terminate": True,
        "tail_output": True,
        "runtime_phase": True,
        "transcript_binding": True,
        "run_once": False,
        "operation_evidence": {
            "launch_local": {"level": "hermetic", "source": "test"},
            "launch_remote": {"level": "hermetic", "source": "test"},
            "reattach": {"level": "hermetic", "source": "test"},
            "send_input": {"level": "hermetic", "source": "test"},
            "interrupt": {"level": "hermetic", "source": "test"},
            "steer_active_turn": {"level": "hermetic", "source": "test"},
            "answer_pause": {"level": "hermetic", "source": "test"},
            "terminate": {"level": "hermetic", "source": "test"},
            "tail_output": {"level": "hermetic", "source": "test"},
            "runtime_phase": {"level": "hermetic", "source": "test"},
            "transcript_binding": {"level": "hermetic", "source": "test"},
            "run_once": {"level": "none", "source": "test"},
        },
    }


def test_managed_provider_contract_matrix_covers_launch_scope_providers():
    assert managed_provider_names() == frozenset({"codex", "claude", "opencode", "antigravity", "cursor"})
    assert {contract.provider for contract in all_managed_provider_contracts()} == managed_provider_names()


def test_managed_provider_contract_manifest_is_generated_from_schema():
    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "schemas" / "managed_providers.yml"
    manifest_path = repo_root / "server" / "zerg" / "config" / "managed_provider_contracts.json"

    schema_payload = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert normalize_contract_manifest(schema_payload) == normalize_contract_manifest(manifest_payload)
    assert render_contract_manifest_json(schema_payload) == manifest_path.read_text(encoding="utf-8")


def test_provider_cli_catalog_matches_managed_provider_contracts():
    assert set(PROVIDER_CLI_BINARY_BY_PROVIDER) == managed_provider_names()
    assert set(PROVIDER_CLI_ENV_BY_PROVIDER) == managed_provider_names()


def test_provider_identity_contracts_are_manifest_backed():
    assert {contract.provider: contract.requires_longhouse_cli for contract in all_managed_provider_contracts()} == {
        "codex": False,
        "claude": True,
        "opencode": False,
        "antigravity": True,
        "cursor": False,
    }
    assert sorted(
        control_plane for contract in all_managed_provider_contracts() for control_plane in contract.control_planes
    ) == sorted(
        {
            "codex_bridge",
            "codex_app_server",
            "claude_channel_bridge",
            "opencode_server_bridge",
            "antigravity_hook_inbox",
            "cursor_acp",
            "cursor_exec",
            "cursor_helm",
        }
    )


def test_control_plane_index_rejects_contract_collisions():
    codex = contract_for_provider("codex")
    claude = contract_for_provider("claude")

    assert codex is not None and claude is not None
    duplicate_claude = replace(claude, control_plane=codex.control_plane, control_plane_aliases=())

    with pytest.raises(ValueError, match="claimed by both codex and claude"):
        _contracts_by_control_plane((codex, duplicate_claude))


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
    assert codex.run_once is True
    assert codex.send_input is True
    assert codex.interrupt is True
    assert codex.steer_active_turn is True
    assert codex.answer_pause is True
    assert codex.machine_control_supports == (
        "codex.send",
        "codex.interrupt",
        "codex.steer",
        "codex.answer_pause",
        "codex.launch",
        "codex.continue",
        "codex.run_once",
        "codex.resume_run_once",
        "codex.turn_start",
    )
    assert codex.machine_control_operations == (
        "send",
        "interrupt",
        "steer",
        "answer_pause",
        "launch",
        "continue",
        "run_once",
        "resume_run_once",
        "turn_start",
    )
    assert remote_launch_supported_providers() == frozenset({"codex", "claude", "opencode", "cursor"})
    assert run_once_supported_providers() == frozenset({"codex", "claude", "opencode", "cursor"})


def test_codex_and_managed_claude_advertise_remote_pause_answering():
    supports_by_provider = {
        contract.provider: set(contract.machine_control_supports) for contract in all_managed_provider_contracts()
    }

    assert "codex.answer_pause" in supports_by_provider["codex"]
    assert "claude.answer_pause" in supports_by_provider["claude"]
    assert "opencode.answer_pause" not in supports_by_provider["opencode"]
    assert "antigravity.answer_pause" not in supports_by_provider["antigravity"]


def test_continue_supported_providers_matches_manifest_can_resume():
    assert continue_supported_providers() == frozenset({"codex", "claude"})


def test_claude_contract_is_first_class_channel_control_provider():
    claude = contract_for_provider("claude")

    assert claude is not None
    assert claude.launch_local is True
    assert claude.launch_remote is True
    assert claude.send_input is True
    assert claude.interrupt is True
    assert claude.steer_active_turn is True
    assert claude.answer_pause is True
    assert claude.operation_evidence_for("steer_active_turn")["level"] == "live_token"
    assert "scheduled live token canary" in claude.operation_evidence_for("steer_active_turn")["next"]
    assert claude.can_resume is True
    assert claude.machine_control_supports == (
        "claude.send",
        "claude.interrupt",
        "claude.steer",
        "claude.answer_pause",
        "claude.launch",
        "claude.continue",
        "claude.turn_start",
    )


def test_opencode_contract_is_server_bridge_control_provider_without_active_turn_steer():
    opencode = contract_for_provider("opencode")

    assert opencode is not None
    assert opencode.launch_local is True
    assert opencode.launch_remote is True
    assert opencode.send_input is True
    assert opencode.interrupt is True
    assert opencode.steer_active_turn is False
    assert opencode.answer_pause is False
    assert opencode.reattach is True
    assert opencode.can_resume is False
    assert opencode.operation_evidence_for("launch_remote")["level"] == "hermetic"
    assert opencode.operation_evidence_for("terminate")["level"] == "hermetic"
    assert opencode.machine_control_supports == (
        "opencode.send",
        "opencode.interrupt",
        "opencode.launch",
        "opencode.terminate",
        "opencode.turn_start",
    )
    assert opencode.connection_capabilities == {
        "can_send_input": 1,
        "can_interrupt": 1,
        "can_terminate": 1,
        "can_tail_output": 1,
        "can_resume": 1,
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
    assert contract.answer_pause is False
    assert contract.tail_output is True
    assert contract.runtime_phase is True
    assert contract.transcript_binding is True
    assert contract.operation_evidence_for("send_input")["level"] == "live_token"
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


def test_codex_exec_is_direct_one_shot_control_not_a_steer_alias():
    assert contract_for_control_plane("codex_exec") is None
    assert managed_transport_for_control_plane("codex_exec") is None
    assert provider_for_control_plane("codex_exec") is None
    assert "codex_exec" in direct_machine_control_planes()
    assert "codex_exec" not in steer_control_planes()
    assert "codex_exec" not in trusted_non_runner_control_planes()


@pytest.mark.parametrize(
    ("provider", "command_type", "capability"),
    [
        ("codex", "session.send_text", "codex.send"),
        ("codex", "session.interrupt", "codex.interrupt"),
        ("codex", "session.steer_text", "codex.steer"),
        ("codex", "session.answer_pause", "codex.answer_pause"),
        ("codex", "session.terminate", None),
        ("codex", "session.run_once", "codex.run_once"),
        ("claude", "session.send_text", "claude.send"),
        ("claude", "session.interrupt", "claude.interrupt"),
        ("claude", "session.steer_text", "claude.steer"),
        ("claude", "session.answer_pause", "claude.answer_pause"),
        ("claude", "session.terminate", None),
        ("claude", "session.run_once", None),
        ("opencode", "session.send_text", "opencode.send"),
        ("opencode", "session.interrupt", "opencode.interrupt"),
        ("opencode", "session.steer_text", None),
        ("opencode", "session.answer_pause", None),
        ("opencode", "session.terminate", "opencode.terminate"),
        ("antigravity", "session.send_text", "antigravity.send"),
        ("antigravity", "session.interrupt", None),
        ("antigravity", "session.steer_text", None),
        ("antigravity", "session.answer_pause", None),
        ("antigravity", "session.terminate", None),
    ],
)
def test_machine_control_capability_for_command_uses_provider_contract(provider, command_type, capability):
    assert machine_control_capability_for_command(provider, command_type) == capability


def test_machine_control_command_projection_is_manifest_backed_for_every_provider():
    command_by_operation = {
        "send": "session.send_text",
        "interrupt": "session.interrupt",
        "steer": "session.steer_text",
        "answer_pause": "session.answer_pause",
        "terminate": "session.terminate",
        "run_once": "session.run_once",
        "turn_start": "session.turn.start",
    }
    launch_only_operations = {"launch", "continue", "resume_run_once"}

    for contract in all_managed_provider_contracts():
        supports = set(contract.machine_control_supports)
        for operation, command_type in command_by_operation.items():
            capability = f"{contract.provider}.{operation}"
            assert machine_control_capability_for_command(contract.provider, command_type) == (
                capability if capability in supports else None
            )

        for support in supports:
            provider, operation = support.split(".", 1)
            assert provider == contract.provider
            assert operation in command_by_operation or operation in launch_only_operations

    assert machine_control_launch_capability_by_provider() == {
        contract.provider: f"{contract.provider}.launch"
        for contract in all_managed_provider_contracts()
        if f"{contract.provider}.launch" in contract.machine_control_supports
    }
    assert continue_supported_providers() == frozenset(
        contract.provider
        for contract in all_managed_provider_contracts()
        if contract.can_resume
    )
    # Every can_resume provider must advertise some continuation capability:
    # Helm providers advertise `.continue` (live), Console-only providers
    # advertise `.resume_run_once` (one-shot --resume).
    for contract in all_managed_provider_contracts():
        if contract.can_resume:
            assert (
                f"{contract.provider}.continue" in contract.machine_control_supports
                or f"{contract.provider}.resume_run_once" in contract.machine_control_supports
            ), f"{contract.provider} has can_resume but no continue/resume_run_once capability"
    assert run_once_supported_providers() == frozenset(
        contract.provider
        for contract in all_managed_provider_contracts()
        if contract.run_once
    )


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
            "codex.answer_pause",
            "codex.launch",
            "codex.run_once",
            "opencode.terminate",
            "claude.answer_pause",
            "unknown.launch",
        ],
        connected=True,
    ) == {
        "codex": ("send", "answer_pause", "launch", "run_once"),
        "claude": ("steer", "answer_pause", "launch"),
        "opencode": ("terminate",),
    }


def test_machine_control_operations_by_provider_requires_connected_channel():
    assert machine_control_operations_by_provider(["codex.launch", "antigravity.send"], connected=False) == {}


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda item: item["machine_control_supports"].__setitem__(0, "send"), "must be provider.operation"),
        (lambda item: item["machine_control_supports"].__setitem__(0, "other.send"), "must use provider prefix test"),
        (lambda item: item["machine_control_supports"].__setitem__(0, "test.made_up"), "unknown operation 'made_up'"),
        (
            lambda item: (
                item.__setitem__("answer_pause", False),
                item["machine_control_supports"].append("test.answer_pause"),
            ),
            "requires answer_pause=true",
        ),
        (
            lambda item: (
                item.__setitem__("run_once", True),
                item.__setitem__("can_resume", False),
                item["machine_control_supports"].append("test.resume_run_once"),
            ),
            "requires can_resume=true",
        ),
    ],
)
def test_machine_control_support_validation_rejects_non_executable_tokens(mutator, message):
    item = _manifest_item()
    item["can_resume"] = True
    item["machine_control_supports"] = ["test.send"]
    mutator(item)

    with pytest.raises(ValueError, match=message):
        _validate_machine_control_supports(item)


def test_provider_cli_discovery_contract_comes_from_managed_provider_manifest():
    assert PROVIDER_CLI_BINARY_BY_PROVIDER == {
        "codex": "codex",
        "claude": "claude",
        "opencode": "opencode",
        "antigravity": "agy",
        "cursor": "cursor-agent",
    }
    assert PROVIDER_CLI_ENV_BY_PROVIDER == {
        "codex": "LONGHOUSE_CODEX_BIN",
        "claude": None,
        "opencode": "LONGHOUSE_OPENCODE_BIN",
        "antigravity": "LONGHOUSE_ANTIGRAVITY_BIN",
        "cursor": "LONGHOUSE_CURSOR_BIN",
    }


# Every managed local launcher must share the same _launch_ui launch
# experience: the hearth splash (launch_panel), the closing bookend
# (exit_bookend), the low-key progress line (progress), and the diagnostic-log
# quieting (quiet_diagnostic_logs). This is a source-level contract guard so a
# new provider can't silently hand-roll its own launch UI and drift from the
# others (cursor_helm originally did — it missed the hearth splash entirely
# because it wasn't on the shared template rail).
_MANAGED_LAUNCHER_MODULES = {
    "claude": "claude.py",
    "codex": "codex.py",
    "opencode": "opencode.py",
    "antigravity": "antigravity.py",
    "cursor": "cursor_helm.py",
}
_SHARED_LAUNCH_UI_HELPERS = (
    "launch_ui.launch_panel(",
    "launch_ui.exit_bookend(",
    "launch_ui.progress(",
    "launch_ui.quiet_diagnostic_logs(",
)


@pytest.mark.parametrize("provider", sorted(_MANAGED_LAUNCHER_MODULES))
def test_managed_launcher_uses_shared_launch_ui_template(provider):
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "server" / "zerg" / "cli" / _MANAGED_LAUNCHER_MODULES[provider]
    source = module_path.read_text(encoding="utf-8")
    missing = [call for call in _SHARED_LAUNCH_UI_HELPERS if call not in source]
    assert not missing, (
        f"{provider} launcher {module_path.name} drifted from the shared "
        f"_launch_ui template; missing: {missing}. Every managed local launcher "
        f"must use the shared hearth splash / exit bookend / progress / "
        f"quiet_diagnostic_logs so the launch experience can't diverge."
    )
    assert "from zerg.cli import _launch_ui" in source or "import _launch_ui" in source, (
        f"{provider} launcher must import the shared _launch_ui module"
    )


def test_agents_service_package_imports_without_database_url():
    """Regression: remote-only CLI launchers (``longhouse cursor``) run with no
    local ``DATABASE_URL``. The ``zerg.services.agents`` package init must not
    eagerly import DB-bound submodules (``store``/``schema``/``helpers``), and
    the Pydantic wire-contract models must import without triggering
    ``zerg.database`` config validation. Verified in a clean subprocess so the
    test process's own env cannot mask the regression.
    """
    import subprocess
    import sys

    code = (
        "from zerg.services.agents.models import EventIngest, SessionIngest; "
        "from zerg.services.agents import SessionIngest as S2, IngestResult; "
        "from zerg.services.cursor_transcript import decode_store_db; "
        "print('IMPORT_OK')"
    )
    repo_root = Path(__file__).resolve().parents[2]
    server_root = repo_root / "server"
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "FERNET_SECRET"}}
    # Sanity: confirm the variables really are absent in the child.
    env.pop("DATABASE_URL", None)
    env.pop("FERNET_SECRET", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(server_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"agents package import failed without DATABASE_URL:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout
