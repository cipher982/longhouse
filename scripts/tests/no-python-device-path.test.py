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
                ],
            }
        ),
    )
    _write(root / "server/zerg/cli/claude.py", "def main(): pass\n")
    _write(root / "server/zerg/cli/codex.py", "def main(): pass\n")
    _write(root / "engine/src/control_channel.rs", "fn run_claude_channel_command() {}\n")


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
            "id": "claude-rust-shellout",
            "category": "transitional_device",
            "provider": "claude",
            "path": "engine/src/control_channel.rs",
            "symbol": "run_claude_channel_command",
            "owner_area": "claude-native",
            "replacement_phase": "phase3",
            "reason": "test",
            "device_command": True,
            "python_dependency_kind": "control_shellout",
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


def test_rust_shellout_symbol_must_exist() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        _write(root / "engine/src/control_channel.rs", "fn something_else() {}\n")

        _assert_fails(
            _run(root, _inventory()),
            "symbol 'run_claude_channel_command' was not found",
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
        test_transitional_python_entries_require_dependency_kind,
        test_requires_longhouse_cli_false_rejects_remote_control_shellout_debt,
        test_rust_shellout_symbol_must_exist,
        test_device_command_cannot_be_test_only,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
