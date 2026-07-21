from __future__ import annotations

import asyncio
import os
import stat
import threading
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
from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.server import CatalogDaemonError
from zerg.catalogd.store import CatalogStore
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
            "schema_version": CATALOG_SCHEMA_VERSION,
            "commit_seq": "0",
            "pid": os.getpid(),
            "ready": True,
        }
        assert schema["catalog_id"] == ping["catalog_id"]
        assert schema["minimum_reader_schema_version"] == CATALOG_SCHEMA_VERSION
        assert schema["maximum_reader_schema_version"] == CATALOG_SCHEMA_VERSION
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
async def test_device_list_is_owner_scoped_ordered_and_snapshot_versioned(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert(),
            [
                {
                    "id": "older-live",
                    "owner_id": 7,
                    "device_id": "cinder",
                    "token_hash": "a" * 64,
                    "created_at": now - timedelta(days=2),
                    "revoked_at": None,
                },
                {
                    "id": "newer-revoked",
                    "owner_id": 7,
                    "device_id": "clifford",
                    "token_hash": "b" * 64,
                    "created_at": now - timedelta(days=1),
                    "revoked_at": now,
                },
                {
                    "id": "other-owner",
                    "owner_id": 8,
                    "device_id": "private",
                    "token_hash": "c" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
            ],
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        active = await client.call("auth.device.list.v2", {"owner_id": 7, "include_revoked": False})
        all_tokens = await client.call("auth.device.list.v2", {"owner_id": 7, "include_revoked": True})

        assert active["commit_seq"] == "0"
        assert active["total"] == 1
        assert [token["id"] for token in active["tokens"]] == ["older-live"]
        assert active["tokens"][0]["is_valid"] is True
        assert all_tokens["commit_seq"] == "0"
        assert all_tokens["total"] == 2
        assert [token["id"] for token in all_tokens["tokens"]] == ["newer-revoked", "older-live"]
        assert all_tokens["tokens"][0]["is_valid"] is False
    finally:
        await client.close()
        await daemon.close()


def test_device_list_holds_real_sqlite_snapshot_across_commit_seq_and_rows(daemon_paths, monkeypatch):
    from zerg.catalogd import store as store_module

    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    original_current_commit_seq = store_module._current_commit_seq
    inserted = False

    def read_seq_then_commit_other_writer(connection):
        nonlocal inserted
        value = original_current_commit_seq(connection)
        if not inserted:
            inserted = True
            writer = create_catalog_engine(database_path)
            try:
                with writer.begin() as writer_connection:
                    writer_connection.execute(
                        LiveDeviceToken.__table__.insert().values(
                            id="concurrent-token",
                            owner_id=7,
                            device_id="clifford",
                            token_hash="d" * 64,
                            created_at=datetime.now(UTC),
                        )
                    )
            finally:
                writer.dispose()
        return value

    monkeypatch.setattr(store_module, "_current_commit_seq", read_seq_then_commit_other_writer)
    try:
        result = CatalogStore(engine).list_devices(owner_id=7, include_revoked=True)
    finally:
        engine.dispose()

    assert inserted is True
    assert result == {
        "commit_seq": "0",
        "tokens": [],
        "total": 0,
        "limit_exceeded": False,
    }


def test_device_create_limit_counts_only_active_credentials(daemon_paths, monkeypatch):
    from zerg.catalogd import store as store_module

    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert(),
            [
                {
                    "id": "active-token",
                    "owner_id": 7,
                    "device_id": "active",
                    "token_hash": "a" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
                {
                    "id": "revoked-token-1",
                    "owner_id": 7,
                    "device_id": "retired-1",
                    "token_hash": "b" * 64,
                    "created_at": now - timedelta(days=2),
                    "revoked_at": now - timedelta(days=1),
                },
                {
                    "id": "revoked-token-2",
                    "owner_id": 7,
                    "device_id": "retired-2",
                    "token_hash": "c" * 64,
                    "created_at": now - timedelta(days=4),
                    "revoked_at": now - timedelta(days=3),
                },
            ],
        )

    monkeypatch.setattr(store_module, "DEVICE_TOKEN_LIMIT_PER_OWNER", 2)
    try:
        created = CatalogStore(engine).create_device(
            owner_id=7,
            token_id="new-active-token",
            device_id="github-cohort-journey",
            token_hash="d" * 64,
        )
        rejected = CatalogStore(engine).create_device(
            owner_id=7,
            token_id="one-too-many",
            device_id="overflow",
            token_hash="e" * 64,
        )
    finally:
        engine.dispose()

    assert created["created"] is True
    assert created["limit_exceeded"] is False
    assert rejected["created"] is False
    assert rejected["limit_exceeded"] is True

    engine = create_catalog_engine(database_path)
    try:
        retained = CatalogStore(engine).list_devices(owner_id=7, include_revoked=True)
    finally:
        engine.dispose()
    assert retained["limit_exceeded"] is False
    assert retained["total"] == 2
    assert [token["id"] for token in retained["tokens"]] == ["new-active-token", "active-token"]


@pytest.mark.asyncio
async def test_device_create_is_atomic_idempotent_and_visible_to_auth(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    token_id = "00000000-0000-4000-8000-000000000001"
    params = {
        "owner_id": 7,
        "token_id": token_id,
        "device_id": "cinder",
        "token_hash": "a" * 64,
    }
    try:
        first = await client.call("auth.device.create.v2", params)
        replay = await client.call("auth.device.create.v2", params)
        auth = await client.call("auth.device.validate.v2", {"token_hash": "a" * 64})
        listed = await client.call("auth.device.list.v2", {"owner_id": 7, "include_revoked": False})

        assert first["created"] is True
        assert first["commit_seq"] == "1"
        assert replay == {**first, "created": False, "exact_replay": True}
        assert auth["valid"] is True
        assert auth["commit_seq"] == "1"
        assert [token["id"] for token in listed["tokens"]] == [token_id]
        assert listed["commit_seq"] == "1"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_create_rejects_token_id_collision(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    token_id = "00000000-0000-4000-8000-000000000001"
    try:
        await client.call(
            "auth.device.create.v2",
            {"owner_id": 7, "token_id": token_id, "device_id": "cinder", "token_hash": "a" * 64},
        )
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.device.create.v2",
                {"owner_id": 7, "token_id": token_id, "device_id": "other", "token_hash": "b" * 64},
            )
        assert exc_info.value.code == "conflict"
        assert (await client.call("ping.v2"))["commit_seq"] == "1"
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
async def test_device_auth_reads_remain_live_while_mutation_executor_is_busy(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    mutation_started = threading.Event()
    release_mutation = threading.Event()

    def block_mutations():
        mutation_started.set()
        release_mutation.wait(timeout=2)

    blocked = asyncio.create_task(daemon._run_store(block_mutations))
    client = CatalogClient(socket_path)
    try:
        assert await asyncio.to_thread(mutation_started.wait, 1)
        result = await asyncio.wait_for(
            client.call("auth.device.validate.v2", {"token_hash": "a" * 64}),
            timeout=0.2,
        )
        assert result == {"valid": False, "commit_seq": "0"}
        lag = await asyncio.wait_for(
            client.call(
                "projector.state.list_lag.v2",
                {"projector": "search-v2", "after_session_id": None, "limit": 1},
            ),
            timeout=0.2,
        )
        assert lag["lag_count"] == 0
        assert not blocked.done()
    finally:
        release_mutation.set()
        await blocked
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
