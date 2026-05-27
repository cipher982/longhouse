#!/usr/bin/env python3
"""Regression tests for the managed Codex static contract guard."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts/qa/check-managed-codex-contract.sh"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_minimal_contract_root(root: Path) -> None:
    _write(
        root / "engine/src/codex_bridge.rs",
        """
pub const LAUNCH_MODE_DETACHED_UI: &str = "detached_ui";
pub const LEGACY_LAUNCH_MODE_HEADLESS: &str = "headless";
pub const PERSISTED_DETACHED_UI_LAUNCH_MODE: &str = LAUNCH_MODE_DETACHED_UI;

fn legacy_reader(value: &str) -> bool {
    value.eq_ignore_ascii_case(LEGACY_LAUNCH_MODE_HEADLESS)
}

fn detached_ui_writer() {
    let _state = BridgeState {
        launch_mode: Some(PERSISTED_DETACHED_UI_LAUNCH_MODE.to_string()),
    };
}
""",
    )
    _write(root / "engine/src/main.rs", "")
    _write(root / "server/zerg/cli/codex.py", "")
    _write(root / "scripts/qa/codex-smoke.sh", "#!/usr/bin/env bash\n")
    _write(root / "scripts/ci/codex-smoke.sh", "#!/usr/bin/env bash\n")


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MANAGED_CODEX_CONTRACT_ROOT"] = str(root)
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


def test_reader_compatibility_for_legacy_headless_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/managed_reaper.rs",
            """
fn legacy_reader(value: &str) -> bool {
    value.eq_ignore_ascii_case(codex_bridge::LEGACY_LAUNCH_MODE_HEADLESS)
}
""",
        )
        _assert_passes(root)


def test_marked_legacy_headless_fixture_is_allowed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/managed_reaper.rs",
            """
fn legacy_fixture(obs: &mut Observation) {
    obs.launch_mode = Some(codex_bridge::LEGACY_LAUNCH_MODE_HEADLESS.to_string()); // LEGACY_HEADLESS_COMPAT_OK
}
""",
        )
        _assert_passes(root)


def test_unrelated_thread_router_names_are_allowed() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "server/zerg/routers/threads.py",
            "async def start_thread_run(thread_id: int):\n    return thread_id\n",
        )
        _assert_passes(root)


def test_rejects_legacy_start_thread_flag_outside_cli_module() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(root / "server/zerg/services/managed_control.py", "args.append('--start-thread')\n")
        _assert_fails(root, "legacy Codex start-thread flag")


def test_rejects_packaged_codex_release_artifact() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(root / "scripts/release/build-managed-codex.sh", "#!/usr/bin/env bash\n")
        _assert_fails(root, "Forbidden managed Codex packaging artifact exists")


def test_rejects_packaged_codex_source_selector() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(root / "scripts/qa/codex-smoke.sh", "echo $LONGHOUSE_CODEX_SOURCE\n")
        _assert_fails(root, "packaged Codex source selector")


def test_rejects_legacy_start_thread_flag() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(root / "server/zerg/cli/codex.py", "args.append('--start-thread')\n")
        _assert_fails(root, "legacy Codex start-thread flag")


def test_rejects_writer_side_legacy_headless_persistence() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/codex_bridge.rs",
            """
pub const LAUNCH_MODE_DETACHED_UI: &str = "detached_ui";
pub const LEGACY_LAUNCH_MODE_HEADLESS: &str = "headless";
pub const PERSISTED_DETACHED_UI_LAUNCH_MODE: &str = LEGACY_LAUNCH_MODE_HEADLESS;
""",
        )
        _assert_fails(root, "detached-ui persisted alias must not point at legacy headless")


def test_rejects_writer_side_legacy_headless_persistence_outside_bridge() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/managed_reaper.rs",
            """
fn legacy_writer(obs: &mut Observation) {
    obs.launch_mode =
        Some(codex_bridge::LEGACY_LAUNCH_MODE_HEADLESS.to_string());
}
""",
        )
        _assert_fails(root, "detached-ui writers must not persist legacy headless state")


def test_rejects_literal_headless_launch_mode_writer() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_minimal_contract_root(root)
        _write(
            root / "engine/src/managed_bridge_scan.rs",
            """
fn legacy_writer(obs: &mut Observation) {
    obs.launch_mode = Some("headless".to_string());
}
""",
        )
        _assert_fails(root, "detached-ui writers must not persist legacy headless state")


def main() -> int:
    tests = [
        test_minimal_valid_contract_passes,
        test_reader_compatibility_for_legacy_headless_is_allowed,
        test_marked_legacy_headless_fixture_is_allowed,
        test_unrelated_thread_router_names_are_allowed,
        test_rejects_legacy_start_thread_flag_outside_cli_module,
        test_rejects_packaged_codex_release_artifact,
        test_rejects_packaged_codex_source_selector,
        test_rejects_legacy_start_thread_flag,
        test_rejects_writer_side_legacy_headless_persistence,
        test_rejects_writer_side_legacy_headless_persistence_outside_bridge,
        test_rejects_literal_headless_launch_mode_writer,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
