from __future__ import annotations

import asyncio
import os
import stat
import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.protocol import CatalogRpcRequest
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import read_frame
from zerg.catalogd.protocol import write_frame
from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.server import CatalogDaemonError
from zerg.models.live_store import LiveDeviceToken


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


@pytest.mark.asyncio
async def test_device_auth_is_typed_read_only_and_reports_commit_seq(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    created_at = datetime.now(UTC) - timedelta(days=2)
    token_hash = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="token-1",
                owner_id=7,
                device_id="cinder",
                token_hash=token_hash,
                created_at=created_at,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = await client.call("auth.device.validate.v2", {"token_hash": token_hash})
        retry = await client.call("auth.device.validate.v2", {"token_hash": token_hash})
        invalid = await client.call("auth.device.validate.v2", {"token_hash": "b" * 64})
        ping = await client.call("ping.v2")

        assert first == {
            "valid": True,
            "commit_seq": "0",
            "token": {
                "id": "token-1",
                "owner_id": 7,
                "device_id": "cinder",
                "created_at": created_at.isoformat(),
                "last_used_at": None,
                "revoked_at": None,
            },
        }
        assert retry == first
        assert invalid == {"valid": False, "commit_seq": "0"}
        assert ping["commit_seq"] == "0"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_auth_rejects_malformed_params_without_touching_store(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.device.validate.v2",
                {"token_hash": "NOT-A-HASH"},
            )
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_revoke_is_atomic_idempotent_and_invalidates_auth(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    token_hash = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="token-1",
                owner_id=7,
                device_id="cinder",
                token_hash=token_hash,
                created_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = await client.call("auth.device.revoke.v2", {"owner_id": 7, "token_id": "token-1"})
        replay = await client.call("auth.device.revoke.v2", {"owner_id": 7, "token_id": "token-1"})
        auth = await client.call("auth.device.validate.v2", {"token_hash": token_hash})
        ping = await client.call("ping.v2")

        assert first["found"] is True
        assert first["changed"] is True
        assert first["commit_seq"] == "1"
        assert replay == {**first, "changed": False}
        assert auth == {"valid": False, "commit_seq": "1"}
        assert ping["commit_seq"] == "1"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_revoke_cannot_cross_owner_or_advance_commit_seq(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="token-1",
                owner_id=7,
                device_id="cinder",
                token_hash="a" * 64,
                created_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("auth.device.revoke.v2", {"owner_id": 8, "token_id": "token-1"})
        assert result == {"found": False, "changed": False, "commit_seq": "0"}
        assert (await client.call("ping.v2"))["commit_seq"] == "0"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_revoke_survives_daemon_restart(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    token_hash = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="token-1",
                owner_id=7,
                device_id="cinder",
                token_hash=token_hash,
                created_at=datetime.now(UTC),
            )
        )
    engine.dispose()

    first_daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await first_daemon.start()
    first_client = CatalogClient(socket_path)
    result = await first_client.call("auth.device.revoke.v2", {"owner_id": 7, "token_id": "token-1"})
    await first_client.close()
    await first_daemon.close()
    assert result["commit_seq"] == "1"

    second_daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await second_daemon.start()
    second_client = CatalogClient(socket_path)
    try:
        auth = await second_client.call("auth.device.validate.v2", {"token_hash": token_hash})
        assert auth == {"valid": False, "commit_seq": "1"}
        assert (await second_client.call("ping.v2"))["commit_seq"] == "1"
    finally:
        await second_client.close()
        await second_daemon.close()


@pytest.mark.asyncio
async def test_mutation_internal_error_is_not_marked_safe_to_retry(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    assert daemon._store is not None

    def fail_revoke(**_kwargs):
        raise RuntimeError("ambiguous mutation failure")

    daemon._store.revoke_device = fail_revoke
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("auth.device.revoke.v2", {"owner_id": 7, "token_id": "token-1"})
        assert exc_info.value.code == "internal"
        assert exc_info.value.retryable is False
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalog_operations_run_off_the_socket_event_loop(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    assert daemon._store is not None
    original = daemon._store.authenticate_device

    def slow_authenticate(**kwargs):
        time.sleep(0.08)
        return original(**kwargs)

    daemon._store.authenticate_device = slow_authenticate
    client = CatalogClient(socket_path)
    try:
        call = asyncio.create_task(client.call("auth.device.validate.v2", {"token_hash": "a" * 64}))
        started = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.05
        assert not call.done()
        assert await call == {"valid": False, "commit_seq": "0"}
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_owns_periodic_passive_checkpoint(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(
        database_path=database_path,
        socket_path=socket_path,
        checkpoint_interval_seconds=0.01,
    )
    await daemon.start()
    assert daemon._store is not None
    original = daemon._store.checkpoint_passive
    checkpoints = 0

    def counted_checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        return original()

    daemon._store.checkpoint_passive = counted_checkpoint
    try:
        deadline = time.monotonic() + 1
        while checkpoints == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert checkpoints > 0
    finally:
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
