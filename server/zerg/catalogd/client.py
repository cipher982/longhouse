"""Persistent, deadline-bounded client for the local catalogd socket."""

from __future__ import annotations

import asyncio
import secrets
import socket
import struct
import time
from pathlib import Path
from typing import Any

from zerg.catalogd.protocol import HEADER_BYTES
from zerg.catalogd.protocol import MAX_PAYLOAD_BYTES
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import ProtocolError
from zerg.catalogd.protocol import decode_frame
from zerg.catalogd.protocol import encode_frame
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame

_SAFE_RETRY_METHODS = {
    # create is an idempotent mutation keyed by caller-supplied token_id. It is
    # safe to replay the exact request after a response is lost.
    "auth.device.create.v2",
    "auth.device.list.v2",
    "auth.device.validate.v2",
    "auth.owner.get.v2",
    "machine.enrollment.list.v2",
    # apply is idempotent on (device_id, received_at) and rejects a key reused
    # with different content, so a lost response can safely replay once.
    "machine.heartbeat.apply.v2",
    "machine.workspace.list.v2",
    # Both control mutations are caller-keyed and exact/idempotent. A replay
    # returns the reserved grant or observes the already-terminal operation.
    "control.command.prepare.v2",
    "control.operation.finish.v2",
    "ping.v2",
    "schema.v2",
    "session.prefix.resolve.v2",
    "session.launch.idempotency.v2",
    "session.launch.intent.create.v2",
    "session.launch.local.create.v2",
    "session.launch.outcome.apply.v2",
    "session.continue.intent.create.v2",
    "session.continue.outcome.apply.v2",
    "interaction.register.v2",
    "interaction.list.v2",
    "interaction.resolve.v2",
    "interaction.decision.read.v2",
    "session.input.queued.list.v2",
    "session.input.claim.v2",
    "session.input.finish.v2",
    "session.input.receipt.read.v2",
    "session.input.recent.list.v2",
    "session.read.v2",
    "session.read.batch.v2",
    "session.preferences.update.v2",
    "session.active.list.v2",
    "session.timeline.list.v2",
}

# speed-of-light-database.md separates the 250 ms p95 performance target from
# the 1 s hard-failure budget. The default client deadline is the hard bound;
# health probes and other callers that need a tighter bound pass one explicitly.
DEFAULT_CATALOG_RPC_TIMEOUT_SECONDS = 1.0


class CatalogUnavailable(RuntimeError):
    pass


class CatalogRemoteError(RuntimeError):
    def __init__(self, response_error) -> None:
        super().__init__(response_error.message)
        self.code = response_error.code
        self.retryable = response_error.retryable
        self.retry_after_ms = response_error.retry_after_ms
        self.details = response_error.details


class CatalogClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        default_timeout_seconds: float = DEFAULT_CATALOG_RPC_TIMEOUT_SECONDS,
    ) -> None:
        self.socket_path = socket_path
        self.default_timeout_seconds = default_timeout_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            await self._poison()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        attempts = 2 if method in _SAFE_RETRY_METHODS else 1
        deadline = asyncio.get_running_loop().time() + timeout
        async with self._lock:
            for attempt in range(attempts):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise CatalogUnavailable(f"catalogd deadline exceeded for {method}")
                try:
                    return await self._call_once(method, params or {}, remaining)
                except CatalogRemoteError:
                    raise
                except (OSError, EOFError, ProtocolError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                    await self._poison()
                    if attempt + 1 == attempts:
                        raise CatalogUnavailable(f"catalogd unavailable for {method}") from exc
        raise AssertionError("unreachable")

    async def _call_once(self, method: str, params: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        loop_deadline = asyncio.get_running_loop().time() + timeout_seconds
        monotonic_deadline = time.monotonic_ns() + int(timeout_seconds * 1_000_000_000)
        request = CatalogRpcRequest(
            id=secrets.token_hex(16),
            method=method,
            deadline_mono_ns=str(monotonic_deadline),
            params=params,
        )
        async with asyncio.timeout_at(loop_deadline):
            if self._reader is None or self._writer is None:
                self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
            await write_frame(self._writer, request)
            response = await read_frame(self._reader)
        if not isinstance(response, CatalogRpcResponse):
            raise ProtocolError("invalid_request", "catalogd returned a request frame")
        if response.id != request.id:
            raise ProtocolError("invalid_request", "catalogd response id mismatch")
        if response.error is not None:
            raise CatalogRemoteError(response.error)
        return response.result or {}

    async def _poison(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def call_catalogd_sync(
    socket_path: Path,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_CATALOG_RPC_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One-shot RPC for synchronous health/readiness handlers."""

    request = CatalogRpcRequest(
        id=secrets.token_hex(16),
        method=method,
        deadline_mono_ns=str(time.monotonic_ns() + int(timeout_seconds * 1_000_000_000)),
        params=params or {},
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            connection.sendall(encode_frame(request))
            header = _recv_exact(connection, HEADER_BYTES)
            payload_length = struct.unpack(">I", header[4:])[0]
            if payload_length > MAX_PAYLOAD_BYTES:
                raise ProtocolError("invalid_request", "catalogd response exceeds frame limit")
            response = decode_frame(header + _recv_exact(connection, payload_length))
    except (EOFError, OSError, ProtocolError) as exc:
        raise CatalogUnavailable(f"catalogd unavailable for {method}") from exc
    if not isinstance(response, CatalogRpcResponse) or response.id != request.id:
        raise CatalogUnavailable("catalogd returned a mismatched response")
    if response.error is not None:
        raise CatalogRemoteError(response.error)
    return response.result or {}


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise EOFError("catalogd closed a partial response")
        chunks.extend(chunk)
    return bytes(chunks)
