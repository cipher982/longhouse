"""Run a real catalogd process and talk to it over its Unix socket, with nothing from the product imported.

Leaf module (stdlib only). Oracles that need a live catalog start catalogd as a child process and drive it
through the wire protocol (``LHR2`` + u32be length + JSON), so the catalog is exercised as a black box. The
framing below is an independent implementation: a change to catalogd's wire format fails the oracle
instead of moving with it.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAGIC = b"LHR2"
_VERSION = 2
_HEADER_BYTES = len(_MAGIC) + 4
DEFAULT_RPC_TIMEOUT_SECONDS = 15.0


class CatalogdRpcError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"{method}: {error.get('code')}: {error.get('message')}")
        self.code = str(error.get("code"))
        self.retryable = bool(error.get("retryable"))
        self.details = error.get("details") or {}


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise EOFError("catalogd closed a partial response")
        chunks.extend(chunk)
    return bytes(chunks)


def call(
    socket_path: Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One request over a fresh connection. Raises ``CatalogdRpcError`` for an error response."""

    request_id = secrets.token_hex(16)
    deadline = time.monotonic_ns() + int(timeout_seconds * 1_000_000_000)
    payload = json.dumps(
        {"v": _VERSION, "id": request_id, "method": method, "deadline_mono_ns": str(deadline), "params": params or {}},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        connection.connect(str(socket_path))
        connection.sendall(_MAGIC + struct.pack(">I", len(payload)) + payload)
        header = _recv_exact(connection, _HEADER_BYTES)
        if header[: len(_MAGIC)] != _MAGIC:
            raise ValueError("catalogd answered with an invalid frame magic")
        length = struct.unpack(">I", header[len(_MAGIC) :])[0]
        response = json.loads(_recv_exact(connection, length).decode("utf-8"))
    if response.get("id") != request_id:
        raise ValueError("catalogd response id mismatch")
    if "error" in response:
        raise CatalogdRpcError(method, response["error"])
    return response.get("result") or {}


@dataclass(frozen=True)
class CatalogdHandle:
    database_path: Path
    socket_path: Path

    def rpc(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        return call(self.socket_path, method, params, timeout_seconds=timeout_seconds)

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """A read-only look at the catalog file (catalogd keeps writing; SQLite WAL readers do not block it)."""

        connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True, timeout=15)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        """Author a fixture row (for example the owning user) that has no RPC."""

        connection = sqlite3.connect(self.database_path, timeout=15)
        try:
            with connection:
                connection.execute(sql, parameters)
        finally:
            connection.close()


def _ready(handle: CatalogdHandle) -> bool:
    if not handle.socket_path.exists():
        return False
    try:
        tables = {row[0] for row in handle.query("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "catalog_meta"} <= tables:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(handle.socket_path))
    except (OSError, sqlite3.Error):
        return False
    return True


@contextmanager
def run_catalogd(
    *,
    server_root: Path,
    database_path: Path | None = None,
    startup_timeout_seconds: float = 90.0,
) -> Iterator[CatalogdHandle]:
    """Start catalogd on a private socket, yield a handle, and stop it (and the scratch dir) on exit.

    ``server_root`` is the checkout's ``server`` directory; the daemon runs from it. ``/tmp`` keeps the
    socket path inside the Unix 104-byte limit. With no ``database_path`` the catalog lives in the scratch dir.
    """

    scratch = Path(tempfile.mkdtemp(prefix="lh-catalogd-", dir="/tmp"))
    handle = CatalogdHandle(database_path=database_path or scratch / "live.db", socket_path=scratch / "catalogd.sock")
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(server_root), os.environ.get("PYTHONPATH", "")]))}
    log = (scratch / "catalogd.log").open("ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "zerg.catalogd", "--database", str(handle.database_path), "--socket", str(handle.socket_path)],
        cwd=server_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + startup_timeout_seconds
        while not _ready(handle):
            if process.poll() is not None:
                raise RuntimeError(f"catalogd exited during startup with {process.returncode}")
            if time.monotonic() > deadline:
                raise TimeoutError("catalogd did not become ready")
            time.sleep(0.1)
        yield handle
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
        shutil.rmtree(scratch, ignore_errors=True)
