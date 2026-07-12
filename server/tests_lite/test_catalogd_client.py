from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.catalogd.client import call_catalogd_sync
from zerg.catalogd.protocol import CatalogRpcError
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame


@pytest.fixture
def socket_path():
    path = Path(tempfile.gettempdir()) / f"lhc-{uuid4().hex}.sock"
    yield path
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_client_reuses_connection_and_matches_response_ids(socket_path):
    connections = 0
    requests = 0

    async def handle(reader, writer):
        nonlocal connections, requests
        connections += 1
        try:
            while True:
                request = await read_frame(reader)
                assert isinstance(request, CatalogRpcRequest)
                requests += 1
                await write_frame(writer, CatalogRpcResponse(id=request.id, result={"method": request.method}))
        except (EOFError, asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    client = CatalogClient(socket_path)
    try:
        assert await client.call("ping.v2") == {"method": "ping.v2"}
        assert await client.call("schema.v2") == {"method": "schema.v2"}
        assert connections == 1
        assert requests == 2
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_surfaces_typed_remote_error(socket_path):
    async def handle(reader, writer):
        request = await read_frame(reader)
        assert isinstance(request, CatalogRpcRequest)
        await write_frame(
            writer,
            CatalogRpcResponse(
                id=request.id,
                error=CatalogRpcError(
                    code="schema_incompatible",
                    message="schema mismatch",
                    retryable=False,
                    retry_after_ms=None,
                    details={"expected": 1},
                ),
            ),
        )
        writer.close()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("ping.v2")
        assert exc_info.value.code == "schema_incompatible"
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_maps_missing_socket_to_catalog_unavailable(socket_path):
    client = CatalogClient(socket_path, default_timeout_seconds=0.05)
    with pytest.raises(CatalogUnavailable):
        await client.call("ping.v2")


@pytest.mark.asyncio
async def test_safe_retry_shares_one_total_deadline(socket_path):
    async def handle(_reader, writer):
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    client = CatalogClient(socket_path, default_timeout_seconds=0.04)
    started = time.monotonic()
    try:
        with pytest.raises(CatalogUnavailable):
            await client.call("ping.v2")
        assert time.monotonic() - started < 0.08
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_sync_health_client_roundtrip(socket_path):
    async def handle(reader, writer):
        request = await read_frame(reader)
        assert isinstance(request, CatalogRpcRequest)
        await write_frame(writer, CatalogRpcResponse(id=request.id, result={"ready": True}))
        writer.close()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    try:
        result = await asyncio.to_thread(call_catalogd_sync, socket_path, "ping.v2")
        assert result == {"ready": True}
    finally:
        server.close()
        await server.wait_closed()
