#!/usr/bin/env python3
"""Regression tests for the managed-session static contract guard."""

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
        root / "docs/specs/managed-provider-session-contract.md",
        "# Managed Provider Session Contract\n",
    )
    _write(
        root / "server/zerg/cli/_common.py",
        """
def load_api_credentials(*, config_dir_is_provider_home=False, **kwargs):
    return "", ""

def ensure_managed_launch_preflight(*, config_dir_is_provider_home=True, **kwargs):
    return None
""",
    )
    _write(
        root / "server/zerg/cli/_managed_contract.py",
        """
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home

def record_managed_provider_contract(*, config_dir=None, config_dir_is_provider_home=False, **kwargs):
    base_dir = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home else config_dir
    return base_dir

def remove_managed_provider_contract(*, config_dir=None, config_dir_is_provider_home=False, **kwargs):
    base_dir = resolve_longhouse_home_from_provider_home(config_dir) if config_dir_is_provider_home else config_dir
    return base_dir
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
        root / "server/zerg/services/local_health.py",
        """
def collect():
    managed_session_ids = {session["session_id"] for session in managed_sessions}
    return collect_managed_session_contract_diagnostics(base_dir=longhouse_home, session_ids=managed_session_ids)
""",
    )
    _write(
        root / "server/zerg/cli/claude.py",
        """
def run():
    load_api_credentials(config_dir_is_provider_home=True)
    record_managed_provider_contract(provider="claude", config_dir_is_provider_home=True)
    remove_managed_provider_contract(provider="claude", config_dir_is_provider_home=True)
""",
    )
    _write(
        root / "server/zerg/cli/codex.py",
        """
def run():
    _load_api_credentials(config_dir_is_provider_home=True)
    record_managed_provider_contract(provider="codex", config_dir_is_provider_home=True)
    remove_managed_provider_contract(provider="codex", config_dir_is_provider_home=True)
""",
    )
    _write(
        root / "server/zerg/cli/opencode.py",
        """
def _run_native_opencode():
    def _record_state():
        state_path = write_opencode_bridge_state(session_id="sid")
        record_managed_provider_contract(provider="opencode", control_state_path=state_path)
    _record_state()
    remove_managed_provider_contract(provider="opencode")

def launch_script():
    _ensure_managed_launch_preflight(config_dir_is_provider_home=False)
    record_managed_provider_contract(provider="opencode")
""",
    )
    _write(
        root / "server/zerg/cli/antigravity.py",
        """
def run():
    _ensure_managed_launch_preflight(config_dir_is_provider_home=False)
    record_managed_provider_contract(provider="antigravity")
    remove_managed_provider_contract(provider="antigravity")
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


def test_rejects_claude_contract_in_provider_home_semantics() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/claude.py",
            """
def run():
    record_managed_provider_contract(provider="claude")
    remove_managed_provider_contract(provider="claude", config_dir_is_provider_home=True)
""",
        )
        _assert_fails(root, "config_dir_is_provider_home=True")


def test_rejects_codex_contract_cleanup_in_provider_home_semantics() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/codex.py",
            """
def run():
    record_managed_provider_contract(provider="codex", config_dir_is_provider_home=True)
    remove_managed_provider_contract(provider="codex")
""",
        )
        _assert_fails(root, "config_dir_is_provider_home=True")


def test_rejects_opencode_preflight_provider_home_mapping() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/opencode.py",
            """
def _run_native_opencode():
    def _record_state():
        state_path = write_opencode_bridge_state(session_id="sid")
        record_managed_provider_contract(provider="opencode", control_state_path=state_path)
    _record_state()
    remove_managed_provider_contract(provider="opencode")

def launch_script():
    _ensure_managed_launch_preflight(config_dir_is_provider_home=True)
    record_managed_provider_contract(provider="opencode")
""",
        )
        _assert_fails(root, "Longhouse-home config dirs stay Longhouse-home config dirs")


def test_rejects_opencode_contract_before_bridge_state() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/opencode.py",
            """
def _run_native_opencode():
    def _record_state():
        record_managed_provider_contract(provider="opencode")
        state_path = write_opencode_bridge_state(session_id="sid")
    _record_state()
    remove_managed_provider_contract(provider="opencode")
""",
        )
        _assert_fails(root, "opencode contract is recorded before bridge state exists")


def test_rejects_opencode_contract_without_state_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/cli/opencode.py",
            """
def _run_native_opencode():
    def _record_state():
        state_path = write_opencode_bridge_state(session_id="sid")
        record_managed_provider_contract(provider="opencode")
    _record_state()
    remove_managed_provider_contract(provider="opencode")
""",
        )
        _assert_fails(root, "control_state_path=state_path")


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
            root / "scripts/qa/bad-managed-session.sh",
            """#!/usr/bin/env bash
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
longhouse codex --cwd "$TMP_ROOT/work"
""",
        )
        _assert_fails(root, "launches a managed provider from a temp cwd with cleanup")


def test_allows_marked_temp_cwd_cleanup() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "scripts/qa/good-managed-session.sh",
            """#!/usr/bin/env bash
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
# longhouse-managed-session-temp-cwd-ok: stops-before-cleanup
longhouse codex --cwd "$TMP_ROOT/work"
""",
        )
        _assert_passes(root)


def test_rejects_local_health_without_active_session_filter() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/services/local_health.py",
            """
def collect():
    return collect_managed_session_contract_diagnostics(base_dir=longhouse_home)
""",
        )
        _assert_fails(root, "active managed session ids")


def test_rejects_slow_provider_version_capture_timeout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/services/managed_session_contracts.py",
            """
def capture_provider_version(provider_binary_path, *, timeout_seconds: float = 5.0):
    return None

def remove_managed_session_contract(*, provider, session_id, base_dir=None):
    return None
""",
        )
        _assert_fails(root, "provider version capture must be bounded")


def main() -> int:
    tests = [
        test_minimal_valid_contract_passes,
        test_rejects_claude_contract_in_provider_home_semantics,
        test_rejects_codex_contract_cleanup_in_provider_home_semantics,
        test_rejects_opencode_preflight_provider_home_mapping,
        test_rejects_opencode_contract_before_bridge_state,
        test_rejects_opencode_contract_without_state_path,
        test_rejects_provider_owned_contract_path_literal,
        test_rejects_temp_cwd_cleanup_without_marker,
        test_allows_marked_temp_cwd_cleanup,
        test_rejects_local_health_without_active_session_filter,
        test_rejects_slow_provider_version_capture_timeout,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
