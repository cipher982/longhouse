from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveUser


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-session-messages-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_owner_sessions(database_path: Path) -> tuple[str, str, str]:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    sender_id = str(uuid4())
    target_id = str(uuid4())
    foreign_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {"id": 7, "email": "owner@example.com", "role": "ADMIN", "is_active": True},
                {"id": 8, "email": "other@example.com", "role": "ADMIN", "is_active": True},
            ],
        )
        connection.execute(
            LiveSession.__table__.insert(),
            [
                {
                    "session_id": sender_id,
                    "owner_id": "7",
                    "provider": "codex",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
                {
                    "session_id": target_id,
                    "owner_id": "7",
                    "provider": "claude",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
                {
                    "session_id": foreign_id,
                    "owner_id": "8",
                    "provider": "codex",
                    "state": "idle",
                    "last_seen_at": now,
                    "updated_at": now,
                },
            ],
        )
    engine.dispose()
    return sender_id, target_id, foreign_id


@pytest.mark.asyncio
async def test_catalogd_session_message_lifecycle_is_idempotent(daemon_paths):
    database_path, socket_path = daemon_paths
    sender_id, target_id, _foreign_id = _seed_owner_sessions(database_path)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).replace(microsecond=0)
    params = {
        "message_key": str(uuid4()),
        "owner_id": 7,
        "from_session_id": sender_id,
        "to_session_id": target_id,
        "text": "Check the migration result",
        "source_event_id": 42,
        "created_at": now.isoformat(),
    }
    try:
        created = await client.call("session.message.create.v2", params)
        replay = await client.call("session.message.create.v2", params)
        assert created["created"] is True
        assert created["message"]["delivery_status"] == "stored_only"
        assert replay["created"] is False
        assert replay["message"] == created["message"]
        message_id = created["message"]["id"]

        listed = await client.call(
            "session.message.list.v2",
            {
                "owner_id": 7,
                "session_id": target_id,
                "direction": "inbound",
                "unacknowledged_only": True,
                "limit": 50,
            },
        )
        assert listed["messages"] == [created["message"]]
        counts = await client.call(
            "session.message.pending_counts.v2",
            {"owner_id": 7, "session_ids": [sender_id, target_id]},
        )
        assert counts["counts"] == {sender_id: 0, target_id: 1}

        acknowledged = await client.call(
            "session.message.ack.v2",
            {
                "owner_id": 7,
                "message_id": message_id,
                "target_session_id": target_id,
                "acknowledged_at": now.isoformat(),
            },
        )
        ack_replay = await client.call(
            "session.message.ack.v2",
            {
                "owner_id": 7,
                "message_id": message_id,
                "target_session_id": target_id,
                "acknowledged_at": now.isoformat(),
            },
        )
        assert acknowledged["changed"] is True
        assert ack_replay["changed"] is False
        assert ack_replay["message"]["acknowledged_at"] == now.isoformat()

        delivery = await client.call(
            "session.message.delivery.v2",
            {
                "owner_id": 7,
                "message_id": message_id,
                "expected_status": "stored_only",
                "delivery_status": "delivered",
                "delivery_attempts": 1,
                "last_error": None,
                "delivered_via": "managed_push",
                "delivered_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        delivery_replay = await client.call(
            "session.message.delivery.v2",
            {
                "owner_id": 7,
                "message_id": message_id,
                "expected_status": "stored_only",
                "delivery_status": "delivered",
                "delivery_attempts": 1,
                "last_error": None,
                "delivered_via": "managed_push",
                "delivered_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        assert delivery["changed"] is True
        assert delivery["message"]["delivery_attempts"] == 1
        assert delivery_replay["changed"] is False
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_session_messages_enforce_owner_and_ack_state(daemon_paths):
    database_path, socket_path = daemon_paths
    sender_id, target_id, foreign_id = _seed_owner_sessions(database_path)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        with pytest.raises(CatalogRemoteError) as owner_error:
            await client.call(
                "session.message.create.v2",
                {
                    "message_key": str(uuid4()),
                    "owner_id": 7,
                    "from_session_id": sender_id,
                    "to_session_id": foreign_id,
                    "text": "cross-owner message",
                    "source_event_id": None,
                    "created_at": now.isoformat(),
                },
            )
        assert owner_error.value.code == "not_found"

        created = await client.call(
            "session.message.create.v2",
            {
                "message_key": str(uuid4()),
                "owner_id": 7,
                "from_session_id": sender_id,
                "to_session_id": target_id,
                "text": "queued message",
                "source_event_id": None,
                "created_at": now.isoformat(),
            },
        )
        message_id = created["message"]["id"]
        await client.call(
            "session.message.delivery.v2",
            {
                "owner_id": 7,
                "message_id": message_id,
                "expected_status": "stored_only",
                "delivery_status": "queued",
                "delivery_attempts": 0,
                "last_error": None,
                "delivered_via": None,
                "delivered_at": None,
                "updated_at": now.isoformat(),
            },
        )
        with pytest.raises(CatalogRemoteError) as ack_error:
            await client.call(
                "session.message.ack.v2",
                {
                    "owner_id": 7,
                    "message_id": message_id,
                    "target_session_id": target_id,
                    "acknowledged_at": now.isoformat(),
                },
            )
        assert ack_error.value.code == "conflict"
        assert ack_error.value.details == {"reason": "not_delivered"}

        with pytest.raises(CatalogRemoteError) as key_error:
            await client.call(
                "session.message.create.v2",
                {
                    "message_key": created["message"]["id"],
                    "owner_id": 7,
                    "from_session_id": sender_id,
                    "to_session_id": target_id,
                    "text": "bad key",
                    "source_event_id": None,
                    "created_at": now.isoformat(),
                },
            )
        assert key_error.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()
