#!/usr/bin/env python3
"""Regression tests for the native managed-session static contract guard."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts/qa/check-managed-session-contract.sh"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_minimal_contract_root(root: Path) -> None:
    _write(
        root / "engine/src/managed_launch_lifecycle.rs",
        """
pub struct ManagedLaunchTransaction;
impl ManagedLaunchTransaction { pub fn confirm(&mut self) {} }
impl Drop for ManagedLaunchTransaction { fn drop(&mut self) {} }
""",
    )
    _write(
        root / "engine/src/longhouse.rs",
        "use managed_launch_lifecycle::{register_managed_launch, ManagedLaunchTransaction};\n",
    )
    _write(
        root / "engine/src/cursor_helm_launcher.rs",
        """
crate::managed_launch_lifecycle::register_managed_launch();
crate::managed_launch_lifecycle::ManagedLaunchTransaction::new();
""",
    )
    _write(
        root / "server/zerg/services/managed_session_contracts.py",
        """
def capture_provider_version(provider_binary_path, *, timeout_seconds: float = 1.0):
    return None

def remove_managed_session_contract(*, provider, session_id, base_dir=None):
    return None
""",
    )
    _write(
        root / "server/zerg/services/local_health/__init__.py",
        """
managed_session_ids = {session["session_id"] for session in managed_sessions}
collect_managed_session_contract_diagnostics(base_dir=longhouse_home, session_ids=managed_session_ids)
""",
    )


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MANAGED_SESSION_CONTRACT_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(CHECK_SCRIPT)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_passes(root: Path) -> None:
    result = _run_check(root)
    assert result.returncode == 0, result.stderr + result.stdout


def _assert_fails(root: Path, expected: str) -> None:
    result = _run_check(root)
    output = result.stderr + result.stdout
    assert result.returncode != 0, output
    assert expected in output, output


def test_minimal_valid_contract_passes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _assert_passes(root)


def test_rejects_native_launcher_without_shared_registration() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(root / "engine/src/longhouse.rs", "struct ManagedLaunchTransaction;\n")
        _assert_fails(root, "native Claude/Codex/OpenCode use shared registration")


def test_rejects_cursor_without_shared_transaction() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/cursor_helm_launcher.rs",
            "crate::managed_launch_lifecycle::register_managed_launch();\n",
        )
        _assert_fails(root, "native Cursor uses the lifecycle transaction")


def test_rejects_provider_owned_contract_path_literal() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/bad.py",
            'path = "~/.claude/managed-local/contracts/claude/session.json"\n',
        )
        _assert_fails(root, "provider-owned managed-session contract storage")


def test_rejects_temp_cwd_cleanup_without_marker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "scripts/qa/bad.sh",
            """
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
longhouse codex --cwd "$TMP"
""",
        )
        _assert_fails(root, "launches a managed provider from a temp cwd with cleanup")


def test_allows_marked_temp_cwd_teardown() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "scripts/qa/good.sh",
            """
# longhouse-managed-session-temp-cwd-ok: session is stopped before cleanup
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
longhouse codex --cwd "$TMP"
""",
        )
        _assert_passes(root)


def main() -> int:
    tests = [
        test_minimal_valid_contract_passes,
        test_rejects_native_launcher_without_shared_registration,
        test_rejects_cursor_without_shared_transaction,
        test_rejects_provider_owned_contract_path_literal,
        test_rejects_temp_cwd_cleanup_without_marker,
        test_allows_marked_temp_cwd_teardown,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
