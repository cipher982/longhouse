#!/usr/bin/env python3
"""Regression tests for the no-Python device-path inventory checker."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts/qa/check-no-python-device-path.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_contract(provider: str, *, requires_longhouse_cli: bool) -> dict:
    return {
        "provider": provider,
        "requires_longhouse_cli": requires_longhouse_cli,
        "machine_control_supports": [f"{provider}.send"],
    }


def _write_root(root: Path) -> None:
    _write(
        root / "server/zerg/config/managed_provider_contracts.json",
        json.dumps(
            {
                "schema_version": 1,
                "providers": [
                    _minimal_contract("claude", requires_longhouse_cli=True),
                    _minimal_contract("codex", requires_longhouse_cli=False),
                    _minimal_contract("antigravity", requires_longhouse_cli=True),
                ],
            }
        ),
    )
    _write(root / "server/zerg/cli/claude.py", "def main(): pass\n")
    _write(root / "server/zerg/cli/codex.py", "def main(): pass\n")
    _write(root / "server/zerg/cli/antigravity.py", "def main(): pass\n")
    _write(root / "server/zerg/cli/antigravity_channel.py", "def main(): pass\n")
    _write(
        root / "engine/src/control_channel.rs",
        "fn claude_channel_send_text() {}\n"
        "fn claude_channel_interrupt() {}\n"
        "fn claude_channel_control_result() {}\n"
        "fn run_antigravity_channel_command() {}\n",
    )
    _write(
        root / "engine/src/claude_channel_launch.rs",
        "struct ClaudeChannelLaunchConfig {}\nfn build_launch_command_plan() {}\nfn launch_detached() {}\n",
    )
    _write(
        root / "engine/src/claude_channel_server.rs",
        "struct ClaudeChannelServeConfig {}\nfn run() {}\n",
    )
    _write(
        root / "server/zerg/services/shipper/hooks.py",
        "HOOK_SCRIPT = 'longhouse-hook.sh shell text'\n"
        "PERMISSION_GATE_SCRIPT = '#!/usr/bin/env python3\\nprint(\"gate\")'\n"
        "CODEX_HOOK_SCRIPT = 'longhouse-codex-hook.sh shell text'\n",
    )
    _write(
        root / "server/zerg/services/antigravity_hook_inbox.py",
        "_ANTIGRAVITY_HOOK_SCRIPT = '#!/bin/bash\\npython3 - <<\\'PY\\'\\nPY\\n'\n",
    )


def _write_pyproject_scripts(root: Path, scripts: dict[str, str]) -> None:
    lines = ["[project]", 'name = "longhouse-test"', "", "[project.scripts]"]
    lines.extend(f'{name} = "{target}"' for name, target in scripts.items())
    _write(root / "server/pyproject.toml", "\n".join(lines) + "\n")


def _inventory(*entries: dict) -> list[dict]:
    return [
        {
            "id": "claude-wrapper",
            "category": "transitional_device",
            "provider": "claude",
            "path": "server/zerg/cli/claude.py",
            "owner_area": "claude-native",
            "replacement_phase": "phase3",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "entrypoint",
        },
        {
            "id": "codex-wrapper",
            "category": "transitional_device",
            "provider": "codex",
            "path": "server/zerg/cli/codex.py",
            "owner_area": "native-entrypoint",
            "replacement_phase": "phase4",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "entrypoint",
        },
        {
            "id": "antigravity-wrapper",
            "category": "transitional_device",
            "provider": "antigravity",
            "path": "server/zerg/cli/antigravity.py",
            "owner_area": "antigravity-decision",
            "replacement_phase": "phase6",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "entrypoint",
        },
        {
            "id": "antigravity-channel",
            "category": "transitional_device",
            "provider": "antigravity",
            "path": "server/zerg/cli/antigravity_channel.py",
            "owner_area": "antigravity-decision",
            "replacement_phase": "phase6",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "control_shellout",
        },
        {
            "id": "claude-rust-shellout",
            "category": "native_device",
            "provider": "claude",
            "path": "engine/src/control_channel.rs",
            "symbol": "claude_channel_control_result",
            "native_dispatch_symbols": [
                "claude_channel_send_text",
                "claude_channel_interrupt",
                "claude_channel_control_result",
            ],
            "owner_area": "claude-native",
            "replacement_phase": "phase3",
            "reason": "test",
            "device_command": True,
        },
        {
            "id": "claude-channel-server-native",
            "category": "native_device",
            "provider": "claude",
            "path": "engine/src/claude_channel_server.rs",
            "symbol": "run",
            "native_dispatch_symbols": [
                "ClaudeChannelServeConfig",
                "run",
            ],
            "owner_area": "claude-native",
            "replacement_phase": "phase3",
            "reason": "test",
            "device_command": True,
        },
        {
            "id": "device-hook-installer-python",
            "category": "transitional_device",
            "provider": "all",
            "path": "server/zerg/services/shipper/hooks.py",
            "owner_area": "native-health-repair",
            "replacement_phase": "phase7",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "hook_installer",
        },
        {
            "id": "claude-lifecycle-hook-shell",
            "category": "native_exempt",
            "provider": "claude",
            "path": "server/zerg/services/shipper/hooks.py",
            "symbol": "HOOK_SCRIPT",
            "installed_path": "~/.claude/hooks/longhouse-hook.sh",
            "owner_area": "native-health-repair",
            "replacement_phase": "exempt",
            "reason": "test",
            "device_command": True,
        },
        {
            "id": "claude-permission-gate-hook-python",
            "category": "transitional_device",
            "provider": "claude",
            "path": "server/zerg/services/shipper/hooks.py",
            "symbol": "PERMISSION_GATE_SCRIPT",
            "installed_path": "~/.claude/hooks/longhouse-permission-gate.py",
            "owner_area": "claude-native",
            "replacement_phase": "phase3",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "hook_script",
        },
        {
            "id": "codex-lifecycle-hook-shell",
            "category": "native_exempt",
            "provider": "codex",
            "path": "server/zerg/services/shipper/hooks.py",
            "symbol": "CODEX_HOOK_SCRIPT",
            "installed_path": "~/.codex/hooks/longhouse-codex-hook.sh",
            "owner_area": "native-health-repair",
            "replacement_phase": "exempt",
            "reason": "test",
            "device_command": True,
        },
        {
            "id": "antigravity-hook-script-python",
            "category": "transitional_device",
            "provider": "antigravity",
            "path": "server/zerg/services/antigravity_hook_inbox.py",
            "symbol": "_ANTIGRAVITY_HOOK_SCRIPT",
            "installed_path": "~/.gemini/antigravity-cli/plugins/longhouse-runtime/longhouse-antigravity-hook.sh",
            "owner_area": "antigravity-decision",
            "replacement_phase": "phase6",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "hook_script",
        },
        *entries,
    ]


def _run(root: Path, inventory: list[dict]) -> subprocess.CompletedProcess[str]:
    inventory_path = root / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    return subprocess.run(
        ["python3", str(CHECK_SCRIPT), "--root", str(root), "--inventory", str(inventory_path)],
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_passes(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stderr + result.stdout


def _assert_fails(result: subprocess.CompletedProcess[str], expected: str) -> None:
    output = result.stderr + result.stdout
    assert result.returncode != 0, output
    assert expected in output, output


def test_minimal_inventory_passes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)

        _assert_passes(_run(root, _inventory()))


def test_requires_longhouse_cli_provider_must_have_transitional_entry() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = [entry for entry in _inventory() if entry["provider"] != "claude"]

        _assert_fails(
            _run(root, inventory),
            "provider claude requires_longhouse_cli=true but has no transitional_device inventory entry",
        )


def test_unclassified_provider_control_python_fails() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(root / "server/zerg/cli/claude_channel.py", "def serve(): pass\n")

        _assert_fails(
            _run(root, _inventory()),
            "server/zerg/cli/claude_channel.py is provider-control Python but is missing",
        )


def test_unclassified_generic_device_python_fails() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(root / "server/zerg/cli/local_health.py", "def app(): pass\n")

        _assert_fails(
            _run(root, _inventory()),
            "server/zerg/cli/local_health.py is provider-control Python but is missing",
        )


def test_packaged_console_script_python_requires_inventory_stance() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(root / "server/zerg/cli/local_health_fast.py", "def main(): pass\n")
        _write_pyproject_scripts(
            root,
            {"longhouse-local-health": "zerg.cli.local_health_fast:main"},
        )

        _assert_fails(
            _run(root, _inventory()),
            "server/zerg/cli/local_health_fast.py is provider-control Python but is missing",
        )


def test_transitional_python_entries_require_dependency_kind() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = _inventory()
        inventory[0].pop("python_dependency_kind")

        _assert_fails(
            _run(root, inventory),
            "claude-wrapper: transitional entries must include python_dependency_kind",
        )


def test_device_installed_python_hook_requires_inventory_stance() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = [
            entry
            for entry in _inventory()
            if entry["id"] != "claude-permission-gate-hook-python"
        ]

        _assert_fails(
            _run(root, inventory),
            "~/.claude/hooks/longhouse-permission-gate.py is installed device artifact but is missing",
        )


def test_python_installed_hook_cannot_be_marked_native_exempt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = _inventory()
        gate = next(entry for entry in inventory if entry["id"] == "claude-permission-gate-hook-python")
        gate["category"] = "native_exempt"
        gate.pop("python_dependency_kind")

        _assert_fails(
            _run(root, inventory),
            "claude-permission-gate-hook-python: Python installed artifact "
            "~/.claude/hooks/longhouse-permission-gate.py cannot be classified as native_exempt",
        )


def test_installed_hook_template_requires_artifact_registration() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        with (root / "server/zerg/services/shipper/hooks.py").open("a", encoding="utf-8") as handle:
            handle.write("FUTURE_HOOK_SCRIPT = '#!/bin/bash\\npython3 -c \"pass\"\\n'\n")

        _assert_fails(
            _run(root, _inventory()),
            "server/zerg/services/shipper/hooks.py::FUTURE_HOOK_SCRIPT looks like an installed device hook script",
        )


def test_installed_hook_runtime_flag_must_match_template() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(
            root / "server/zerg/services/shipper/hooks.py",
            "HOOK_SCRIPT = 'longhouse-hook.sh shell text'\n"
            "PERMISSION_GATE_SCRIPT = '#!/usr/bin/env python3\\nprint(\"gate\")'\n"
            "CODEX_HOOK_SCRIPT = '#!/bin/bash\\npython3 -c \"pass\"\\n'\n",
        )

        _assert_fails(
            _run(root, _inventory()),
            "server/zerg/services/shipper/hooks.py::CODEX_HOOK_SCRIPT "
            "requires_python_runtime=False but source template invokes Python=True",
        )


def test_blocker_entries_fail_until_resolved() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = _inventory(
            {
                "id": "future-python-hook-blocker",
                "category": "blocker",
                "provider": "claude",
                "path": "server/zerg/services/shipper/hooks.py",
                "owner_area": "claude-native",
                "replacement_phase": "phase3",
                "reason": "test",
                "device_command": True,
            }
        )

        _assert_fails(
            _run(root, inventory),
            "future-python-hook-blocker: blocker entries must be resolved",
        )


def test_requires_longhouse_cli_false_rejects_remote_control_shellout_debt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = _inventory(
            {
                "id": "codex-control-shellout",
                "category": "transitional_device",
                "provider": "codex",
                "path": "engine/src/control_channel.rs",
                "owner_area": "codex-native",
                "replacement_phase": "phase4",
                "reason": "test",
                "device_command": True,
                "python_dependency_kind": "control_shellout",
            }
        )

        _assert_fails(
            _run(root, inventory),
            "provider codex has requires_longhouse_cli=false but codex-control-shellout is control_shellout",
        )


def test_native_dispatch_symbols_must_exist() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(root / "engine/src/control_channel.rs", "fn something_else() {}\n")

        _assert_fails(
            _run(root, _inventory()),
            "native dispatch symbol 'claude_channel_send_text' was not found",
        )


def test_device_command_cannot_be_test_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        inventory = _inventory()
        inventory[0]["category"] = "test_only"

        _assert_fails(
            _run(root, inventory),
            "device_command=true cannot be classified as test_only",
        )


def main() -> int:
    tests = [
        test_minimal_inventory_passes,
        test_requires_longhouse_cli_provider_must_have_transitional_entry,
        test_unclassified_provider_control_python_fails,
        test_unclassified_generic_device_python_fails,
        test_packaged_console_script_python_requires_inventory_stance,
        test_transitional_python_entries_require_dependency_kind,
        test_device_installed_python_hook_requires_inventory_stance,
        test_python_installed_hook_cannot_be_marked_native_exempt,
        test_installed_hook_template_requires_artifact_registration,
        test_installed_hook_runtime_flag_must_match_template,
        test_blocker_entries_fail_until_resolved,
        test_requires_longhouse_cli_false_rejects_remote_control_shellout_debt,
        test_native_dispatch_symbols_must_exist,
        test_device_command_cannot_be_test_only,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
