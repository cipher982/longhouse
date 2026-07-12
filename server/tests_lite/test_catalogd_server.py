from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame
from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.server import CatalogDaemonError


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_daemon_publishes_private_socket_and_serves_ping_schema(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    metadata = await daemon.start()
    client = CatalogClient(socket_path)
    try:
        ping = await client.call("ping.v2")
        schema = await client.call("schema.v2")
        assert ping == {
            "catalog_id": str(metadata.catalog_id),
            "schema_generation": CATALOG_SCHEMA_GENERATION,
            "schema_version": 1,
            "commit_seq": "0",
            "pid": os.getpid(),
            "ready": True,
        }
        assert schema["catalog_id"] == ping["catalog_id"]
        assert schema["minimum_reader_schema_version"] == 1
        assert schema["maximum_reader_schema_version"] == 1
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    finally:
        await client.close()
        await daemon.close()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_daemon_restart_preserves_catalog_identity(daemon_paths):
    database_path, socket_path = daemon_paths
    first = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    first_meta = await first.start()
    await first.close()
    second = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    second_meta = await second.start()
    try:
        assert second_meta.catalog_id == first_meta.catalog_id
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_second_daemon_cannot_steal_lock_or_socket(daemon_paths):
    database_path, socket_path = daemon_paths
    first = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await first.start()
    inode = socket_path.stat().st_ino
    second = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    try:
        with pytest.raises(CatalogDaemonError, match="lock is already held"):
            await second.start()
        assert socket_path.stat().st_ino == inode
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_daemon_refuses_nonsocket_publication_target(daemon_paths):
    database_path, socket_path = daemon_paths
    socket_path.write_text("do not replace", encoding="utf-8")
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    with pytest.raises(CatalogDaemonError, match="not a socket"):
        await daemon.start()
    assert socket_path.read_text(encoding="utf-8") == "do not replace"


@pytest.mark.asyncio
async def test_expired_deadline_is_typed(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await write_frame(
            writer,
            CatalogRpcRequest(
                id="0" * 32,
                method="ping.v2",
                deadline_mono_ns="0",
                params={},
            ),
        )
        response = await read_frame(reader)
        assert isinstance(response, CatalogRpcResponse)
        assert response.error is not None
        assert response.error.code == "deadline_exceeded"
    finally:
        writer.close()
        await writer.wait_closed()
        await daemon.close()
