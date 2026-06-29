#!/usr/bin/env python3
"""Regression tests for the native device-entrypoint contract checker."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts/qa/check-native-device-entrypoints.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_root(root: Path) -> None:
    _write(
        root / "scripts/qa/check-no-python-device-path.py",
        """
DEFAULT_INVENTORY = (
    {
        "id": "cli-main-provider-entrypoints",
        "category": "transitional_device",
        "provider": "all",
        "path": "server/zerg/cli/main.py",
    },
    {
        "id": "codex-launch-wrapper",
        "category": "transitional_device",
        "provider": "codex",
        "path": "server/zerg/cli/codex.py",
    },
)
""",
    )
    _write(
        root / "server/pyproject.toml",
        """
[project]
name = "longhouse-test"

[project.scripts]
longhouse = "zerg.cli.main:main"
""",
    )


def _contract(*commands: dict, shims: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "native_owner": {
            "binary": "longhouse-engine",
            "namespace": "device",
            "status": "planned",
        },
        "compatibility_shims": shims
        if shims is not None
        else [
            {
                "script": "longhouse",
                "target": "zerg.cli.main:main",
                "status": "transitional_shim",
                "delegates_to": "longhouse-engine device",
                "phase1_inventory_ids": ["cli-main-provider-entrypoints"],
                "removal_phase": "phase7",
            }
        ],
        "commands": [
            {
                "id": "device-root",
                "status": "planned",
                "implementation_phase": "phase2",
                "legacy_commands": ["longhouse"],
                "native_target_command": "longhouse-engine device",
                "phase1_inventory_ids": ["cli-main-provider-entrypoints"],
                "providers": "all",
                "provider_binary_ownership": "not_applicable",
                "token_policy": "not_applicable",
                "cwd_policy": "not_applicable",
                "notes": "test",
            },
            {
                "id": "codex-managed",
                "status": "planned",
                "implementation_phase": "phase4",
                "legacy_commands": ["longhouse codex"],
                "native_target_command": "longhouse-engine device codex",
                "phase1_inventory_ids": ["codex-launch-wrapper"],
                "providers": ["codex"],
                "provider_binary_ownership": "user_owned",
                "token_policy": "env_or_state_file",
                "cwd_policy": "strict_absolute_or_existing",
                "notes": "test",
            },
            *commands,
        ],
    }


def _run(root: Path, contract: dict) -> subprocess.CompletedProcess[str]:
    contract_path = root / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return subprocess.run(
        ["python3", str(CHECK_SCRIPT), "--root", str(root), "--contract", str(contract_path)],
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


def test_minimal_contract_passes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)

        _assert_passes(_run(root, _contract()))


def test_packaged_console_script_requires_compatibility_plan() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)

        _assert_fails(
            _run(root, _contract(shims=[])),
            "packaged console script longhouse has no native entrypoint compatibility plan",
        )


def test_schema_version_is_pinned() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["schema_version"] = 2

        _assert_fails(
            _run(root, contract),
            "schema_version must be 1",
        )


def test_native_owner_status_is_validated() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["native_owner"]["status"] = "aspirational"

        _assert_fails(
            _run(root, contract),
            "native_owner.status must be one of",
        )


def test_implementation_phase_is_validated() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][1]["implementation_phase"] = "phaze4"

        _assert_fails(
            _run(root, contract),
            "codex-managed: implementation_phase must be one of",
        )


def test_every_transitional_inventory_id_needs_command_plan() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"] = [command for command in contract["commands"] if command["id"] != "codex-managed"]

        _assert_fails(
            _run(root, contract),
            "Phase 1 transitional inventory id codex-launch-wrapper has no native device entrypoint plan",
        )


def test_unknown_phase1_inventory_id_fails() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][0]["phase1_inventory_ids"] = ["missing-id"]

        _assert_fails(
            _run(root, contract),
            "device-root: references unknown Phase 1 inventory id missing-id",
        )


def test_native_target_must_not_route_through_python_or_longhouse() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][0]["native_target_command"] = "longhouse local-health"

        _assert_fails(
            _run(root, contract),
            "device-root: native_target_command must not route through longhouse",
        )


def test_invalid_cwd_policy_fails() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][1]["cwd_policy"] = "wherever"

        _assert_fails(
            _run(root, contract),
            "codex-managed: cwd_policy must be one of",
        )


def test_provider_command_requires_concrete_cwd_policy() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][1]["cwd_policy"] = "not_applicable"

        _assert_fails(
            _run(root, contract),
            "codex-managed: provider command plans must declare a concrete cwd_policy",
        )


def test_provider_command_must_keep_provider_binary_user_owned() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][1]["provider_binary_ownership"] = "not_applicable"

        _assert_fails(
            _run(root, contract),
            "codex-managed: provider command plans must keep provider binaries user_owned",
        )


def test_native_status_rejects_transitional_phase1_debt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_root(root)
        contract = _contract()
        contract["commands"][0]["status"] = "native"

        _assert_fails(
            _run(root, contract),
            "device-root: cannot be native while Phase 1 inventory id cli-main-provider-entrypoints is still transitional_device",
        )


def main() -> int:
    tests = [
        test_minimal_contract_passes,
        test_packaged_console_script_requires_compatibility_plan,
        test_schema_version_is_pinned,
        test_native_owner_status_is_validated,
        test_implementation_phase_is_validated,
        test_every_transitional_inventory_id_needs_command_plan,
        test_unknown_phase1_inventory_id_fails,
        test_native_target_must_not_route_through_python_or_longhouse,
        test_invalid_cwd_policy_fails,
        test_provider_command_requires_concrete_cwd_policy,
        test_provider_command_must_keep_provider_binary_user_owned,
        test_native_status_rejects_transitional_phase1_debt,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
