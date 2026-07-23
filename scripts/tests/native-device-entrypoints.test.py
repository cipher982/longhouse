#!/usr/bin/env python3
"""Regression tests for the native device command contract."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/qa/check-native-device-entrypoints.py"


def contract() -> dict:
    return {"schema_version": 2, "native_owner": {"binary": "longhouse", "namespace": "device", "status": "available"}, "commands": [{"id": "codex", "status": "available", "native_target_command": "longhouse codex", "providers": ["codex"], "provider_binary_ownership": "user_owned", "token_policy": "env_or_state_file", "cwd_policy": "strict_absolute_or_existing", "notes": "test"}]}


def run(payload: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "contract.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(["python3", str(CHECK), "--contract", str(path)], text=True, capture_output=True, check=False)


def assert_fails(payload: dict, text: str) -> None:
    result = run(payload)
    assert result.returncode, result.stdout + result.stderr
    assert text in result.stdout + result.stderr


def test_valid_contract() -> None:
    assert run(contract()).returncode == 0


def test_rejects_non_native_target() -> None:
    payload = contract(); payload["commands"][0]["native_target_command"] = "python3 command"
    assert_fails(payload, "must start with longhouse")


def test_requires_available_native_owner() -> None:
    payload = contract(); payload["native_owner"]["status"] = "planned"
    assert_fails(payload, "native_owner")


def test_rejects_unknown_status() -> None:
    payload = contract(); payload["commands"][0]["status"] = "planned"
    assert_fails(payload, "status must be one of")


if __name__ == "__main__":
    for test in (test_valid_contract, test_rejects_non_native_target, test_requires_available_native_owner, test_rejects_unknown_status):
        test(); print(f"PASS {test.__name__}")
