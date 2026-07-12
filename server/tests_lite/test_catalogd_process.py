from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient


@pytest.fixture
def process_paths():
    root = Path("/tmp") / f"lhcdp-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _start(database_path: Path, socket_path: Path, *, extra_env=None):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zerg.catalogd",
            "--database",
            str(database_path),
            "--socket",
            str(socket_path),
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, **(extra_env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def _wait_ping(socket_path: Path, *, timeout: float = 10.0):
    client = CatalogClient(socket_path, default_timeout_seconds=0.25)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                return await client.call("ping.v2")
            except Exception:
                await asyncio.sleep(0.05)
        raise AssertionError("catalogd did not become ready")
    finally:
        await client.close()


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_kill_restart_recovers_wal_and_preserves_catalog_identity(process_paths):
    database_path, socket_path = process_paths
    first = _start(database_path, socket_path)
    try:
        first_ping = await _wait_ping(socket_path)
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=5)
        second = _start(database_path, socket_path)
        try:
            second_ping = await _wait_ping(socket_path)
            assert second_ping["catalog_id"] == first_ping["catalog_id"]
            assert second_ping["pid"] != first_ping["pid"]
        finally:
            _stop(second)
    finally:
        _stop(first)


@pytest.mark.asyncio
async def test_exit_after_schema_never_publishes_socket(process_paths):
    database_path, socket_path = process_paths
    process = _start(
        database_path,
        socket_path,
        extra_env={"LONGHOUSE_CATALOGD_TEST_EXIT_AFTER_SCHEMA": "1"},
    )
    process.wait(timeout=10)
    assert process.returncode == 93
    assert database_path.exists()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_second_process_cannot_replace_live_socket(process_paths):
    database_path, socket_path = process_paths
    first = _start(database_path, socket_path)
    try:
        await _wait_ping(socket_path)
        inode = socket_path.stat().st_ino
        second = _start(database_path, socket_path)
        second.wait(timeout=10)
        assert second.returncode != 0
        assert socket_path.stat().st_ino == inode
        assert (await _wait_ping(socket_path))["pid"] == first.pid
    finally:
        _stop(first)


@pytest.mark.asyncio
async def test_sigterm_unpublishes_socket(process_paths):
    database_path, socket_path = process_paths
    process = _start(database_path, socket_path)
    await _wait_ping(socket_path)
    process.terminate()
    process.wait(timeout=10)
    assert process.returncode == 0
    assert not socket_path.exists()
