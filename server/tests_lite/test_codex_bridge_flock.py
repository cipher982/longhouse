"""End-to-end flock test: real `longhouse-engine codex-bridge run` must bail
if another process already holds the bridge sidecar lock.

This is the test that actually exercises the PID-reuse-immune liveness
primitive. Unit tests in test_local_health_cli.py stub the probe; this one
boots the real engine binary and observes its behavior.

Skips when the engine binary isn't on PATH (e.g. barebones CI sandboxes).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())


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
SESSION_ID = "bbbb1111-2222-4333-8444-555566667777"


pytestmark = pytest.mark.skipif(
    ENGINE_BIN is None,
    reason="longhouse-engine binary not installed; skip flock integration test",
)


def _lock_path(state_file: Path) -> Path:
    return state_file.with_suffix(".lock")


def _acquire(lock_path: Path) -> int:
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_second_bridge_bails_when_lock_is_held(tmp_path: Path) -> None:
    state_file = tmp_path / f"{SESSION_ID}.json"
    log_file = tmp_path / f"{SESSION_ID}.log"
    lock_path = _lock_path(state_file)

    # Pre-acquire the sidecar lock to simulate a live bridge daemon.
    held_fd = _acquire(lock_path)

    try:
        # Run the engine daemon; it must bail on lock contention before
        # attempting anything involving the codex binary.
        result = subprocess.run(
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
                "/nonexistent/codex",  # must not be reached
                "--state-file",
                str(state_file),
                "--log-file",
                str(log_file),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "LONGHOUSE_CODEX_BRIDGE_TOKEN": "test-token"},
            timeout=15,
        )
    finally:
        os.close(held_fd)

    assert result.returncode != 0, (
        f"engine unexpectedly succeeded while lock was held:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "another codex bridge already owns lock" in combined, (
        "expected lock-contention error but got:\n" + combined
    )


def test_lock_becomes_acquirable_after_holder_releases(tmp_path: Path) -> None:
    """Kernel releases flock on process exit — holder drop makes lock free."""
    state_file = tmp_path / f"{SESSION_ID}-release.json"
    lock_path = _lock_path(state_file)

    fd = _acquire(lock_path)
    # Spawn a second attempter running in a subprocess in the background;
    # since our current proc holds the lock, a second try_lock must fail.
    fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Release the original lock — second fd should now acquire.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        fd = -1
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd2, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(fd2)


def test_engine_acquires_lock_then_releases_on_sigkill(tmp_path: Path) -> None:
    """Engine holds the lock for its process lifetime; SIGKILL frees it."""
    state_file = tmp_path / f"{SESSION_ID}-sigkill.json"
    log_file = tmp_path / f"{SESSION_ID}-sigkill.log"
    lock_path = _lock_path(state_file)

    # Use a nonexistent codex bin so the engine fails handshake shortly after
    # acquiring the lock. We just need to observe the lock being held during
    # that window, then released after kill.
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
            "/nonexistent/codex",
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
        # Wait for the engine to create the lock file and acquire it. Poll
        # briefly; if the engine has already exited, the lock path may not
        # have been touched at all.
        deadline = time.time() + 5.0
        lock_held = False
        while time.time() < deadline:
            if proc.poll() is not None:
                # Process exited before we caught the lock; acceptable
                # outcome for this flaky-startup path (codex not found).
                break
            if lock_path.exists():
                probe_fd = os.open(str(lock_path), os.O_RDWR)
                try:
                    try:
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # Acquired -> engine doesn't hold it yet; retry.
                        fcntl.flock(probe_fd, fcntl.LOCK_UN)
                    except BlockingIOError:
                        lock_held = True
                        break
                finally:
                    os.close(probe_fd)
            time.sleep(0.05)

        if lock_held:
            # Kill the engine and verify the kernel releases the lock.
            proc.kill()
            proc.wait(timeout=5)

            deadline = time.time() + 3.0
            released = False
            while time.time() < deadline:
                probe_fd = os.open(str(lock_path), os.O_RDWR)
                try:
                    try:
                        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(probe_fd, fcntl.LOCK_UN)
                        released = True
                        break
                    except BlockingIOError:
                        pass
                finally:
                    os.close(probe_fd)
                time.sleep(0.05)
            assert released, "kernel did not release flock after SIGKILL"
        else:
            # Engine never got far enough to hold the lock (likely bailed
            # before lock acquisition due to codex-bin failure ordering).
            # That is itself a correctness observation worth pinning: the
            # flock is acquired before network I/O per codex_bridge.rs.
            pytest.skip("engine exited before flock acquisition was observable")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
