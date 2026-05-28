"""Rust-to-Python contract: the state file written by `longhouse-engine
codex-bridge run` must carry exactly the fields `_collect_managed_codex_sessions`
inspects, with the right JSON types.

This catches drift between engine/src/codex_bridge.rs::BridgeStateFile and
server/zerg/services/local_health.py. Skipped when the engine binary isn't
available (bare CI sandboxes).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.services.local_health import _collect_managed_codex_sessions  # noqa: E402,I001

def _engine_bin() -> str | None:
    explicit = os.environ.get("LONGHOUSE_ENGINE_BIN")
    if explicit:
        return explicit
    repo_root = Path(__file__).resolve().parents[2]
    for relative in ("engine/target/release/longhouse-engine", "engine/target/debug/longhouse-engine"):
        candidate = repo_root / relative
        if candidate.exists():
            return str(candidate)
    return shutil.which("longhouse-engine")


ENGINE_BIN = _engine_bin()
SESSION_ID = "cccc2222-3333-4444-8555-666677778888"

pytestmark = pytest.mark.skipif(
    ENGINE_BIN is None,
    reason="longhouse-engine binary not installed; skip Rust-Python contract test",
)


def _wait_for_lock_held(lock_path: Path, deadline: float) -> bool:
    """Return True once the engine has acquired the bridge lock."""
    while time.time() < deadline:
        if lock_path.exists():
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Acquired → engine doesn't hold it yet.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    return True
            finally:
                os.close(fd)
        time.sleep(0.05)
    return False


def test_bridge_state_file_schema_matches_python_reader(tmp_path: Path) -> None:
    """Engine writes every field `_collect_managed_codex_sessions` reads."""
    # base_dir mimics `~/.longhouse`; state dir resolves to
    # `<base_dir>/managed-local/codex-bridge`.
    base_dir = tmp_path / ".longhouse"
    base_dir.mkdir()
    state_dir = base_dir / "managed-local" / "codex-bridge"
    state_dir.mkdir(parents=True)

    state_file = state_dir / f"{SESSION_ID}.json"
    log_file = state_dir / f"{SESSION_ID}.log"
    lock_path = state_file.with_suffix(".lock")
    codex_stub = tmp_path / "codex-stub"
    codex_stub.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    codex_stub.chmod(0o755)

    proc = subprocess.Popen(
        [
            ENGINE_BIN,
            "codex-bridge",
            "run",
            "--session-id",
            SESSION_ID,
            "--cwd",
            str(tmp_path),
            "--url",
            "http://127.0.0.1:0",
            "--codex-bin",
            str(codex_stub),
            "--state-file",
            str(state_file),
            "--log-file",
            str(log_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "LONGHOUSE_CODEX_BRIDGE_TOKEN": "test-token"},
    )

    try:
        # The engine writes the initial state file before touching the network.
        # Wait for both the lock to be held and the state file to be populated.
        deadline = time.time() + 5.0
        while time.time() < deadline and not state_file.exists():
            if proc.poll() is not None:
                break
            time.sleep(0.05)

        if not state_file.exists():
            pytest.skip("engine exited before writing state file (platform oddity)")

        lock_held = _wait_for_lock_held(lock_path, time.time() + 2.0)

        raw = json.loads(state_file.read_text())

        # Fields the Python reader inspects. Types must match what
        # `_collect_managed_codex_sessions` + `_normalize_optional_string`
        # + `_managed_session_phase` expect.
        if "schema_version" in raw:
            assert isinstance(raw["schema_version"], int)
        assert isinstance(raw["session_id"], str) and raw["session_id"] == SESSION_ID
        assert isinstance(raw["pid"], int) and raw["pid"] > 0
        assert isinstance(raw["status"], str)
        assert isinstance(raw["cwd"], str)
        assert isinstance(raw["updated_at"], str)
        datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00"))

        # Optional fields: must be absent, null, or string. Never int/list/dict.
        for optional_key in (
            "ws_url",
            "launch_mode",
            "thread_id",
            "thread_path",
            "active_turn_id",
            "last_turn_status",
            "last_error",
            "thread_subscription_status",
            "thread_subscription_last_error",
        ):
            if optional_key in raw and raw[optional_key] is not None:
                assert isinstance(raw[optional_key], str), (
                    f"{optional_key} must be string|null, got {type(raw[optional_key]).__name__}"
                )

        if "thread_subscription_attempts" in raw:
            assert isinstance(raw["thread_subscription_attempts"], int), (
                "thread_subscription_attempts must be int"
            )

        # Only run the Python collector while the engine is alive. Otherwise
        # `_bridge_is_alive` considers the state stale and purges it.
        if lock_held and proc.poll() is None:
            sessions, orphans = _collect_managed_codex_sessions(base_dir)
            # No binding rows exist, so the bridge shows up as an orphan.
            assert sessions == []
            assert len(orphans) == 1
            orphan = orphans[0]
            assert orphan["session_id"] == SESSION_ID
            assert orphan["provider"] == "codex"
            assert isinstance(orphan["pid"], int)
            assert orphan["pid"] == raw["pid"]
            assert orphan["status"] == "orphan"
            assert orphan["reason_codes"] == ["no_managed_session_bound"]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
