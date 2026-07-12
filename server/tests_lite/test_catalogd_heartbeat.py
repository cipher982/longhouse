from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSession


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-heartbeat-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _heartbeat(*, device_id: str, received_at: datetime, digest: str) -> dict:
    return {
        "device_id": device_id,
        "received_at": received_at.isoformat(),
        "version": "engine-test",
        "last_ship_at": None,
        "last_ship_attempt_at": None,
        "last_ship_result": "ok",
        "last_ship_latency_ms": 12,
        "last_ship_http_status": 200,
        "spool_pending": 0,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "consecutive_failures": 0,
        "ship_attempts_1h": 2,
        "ship_successes_1h": 2,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": 10,
        "ship_latency_p95_ms_1h": 20,
        "disk_free_bytes": 1_000_000,
        "is_offline": 0,
        "raw_json": "{}",
        "sessions_digest": digest,
        "sessions_sequence": 9,
    }


def _lease(*, session_id: str, observed_at: datetime) -> dict:
    return {
        "session_id": session_id,
        "provider": "codex",
        "machine_id": "cinder",
        "sequence": 4,
        "state": "attached",
        "phase": "idle",
        "tool_name": None,
        "bridge_status": "ready",
        "thread_subscription_status": "subscribed",
        "observed_at": observed_at.isoformat(),
        "lease_ttl_ms": 900_000,
    }


@pytest.mark.asyncio
async def test_heartbeat_apply_is_atomic_replay_safe_and_reconciles_snapshot(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    current_id = str(uuid4())
    missing_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveHeartbeatStamp.__table__.insert(),
            [
                {
                    "device_id": "cinder",
                    "received_at": now - timedelta(minutes=1),
                    "sessions_digest": "old-digest",
                },
                {
                    "device_id": "cinder",
                    "received_at": now - timedelta(days=31),
                    "sessions_digest": "stale",
                },
                {
                    "device_id": "other",
                    "received_at": now - timedelta(days=31),
                    "sessions_digest": "other-stale",
                },
            ],
        )
        connection.execute(
            LiveControlLease.__table__.insert().values(
                session_id=missing_id,
                provider="codex",
                device_id="cinder",
                state="attached",
                heartbeat_at=now - timedelta(minutes=1),
            )
        )
        connection.execute(
            LiveSession.__table__.insert().values(
                session_id=missing_id,
                provider="codex",
                device_id="cinder",
                state="attached",
                last_seen_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    engine.dispose()

    params = {
        "heartbeat": _heartbeat(device_id="cinder", received_at=now, digest="new-digest"),
        "managed_leases": [_lease(session_id=current_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = await client.call("machine.heartbeat.apply.v2", params)
        replay = await client.call("machine.heartbeat.apply.v2", params)
        assert first["previous_sessions_digest"] == "old-digest"
        assert first["exact_replay"] is False
        assert first["commit_seq"] == "1"
        assert set(first["touched_session_ids"]) == {current_id, missing_id}
        assert replay == {**first, "exact_replay": True}

        changed = {**params, "heartbeat": {**params["heartbeat"], "version": "different"}}
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("machine.heartbeat.apply.v2", changed)
        assert exc_info.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        stamps = (
            connection.execute(
                LiveHeartbeatStamp.__table__.select().order_by(
                    LiveHeartbeatStamp.device_id, LiveHeartbeatStamp.received_at
                )
            )
            .mappings()
            .all()
        )
        assert [(row["device_id"], row["sessions_digest"]) for row in stamps] == [
            ("cinder", "old-digest"),
            ("cinder", "new-digest"),
            ("other", "other-stale"),
        ]
        outbox_rows = connection.execute(LiveArchiveOutbox.__table__.select()).mappings().all()
        assert len(outbox_rows) == 1
        outbox_payload = json.loads(outbox_rows[0]["payload_json"])
        # The existing archive drainer reads payload["heartbeat"] and ignores
        # sibling receipt metadata, so this replay receipt stays compatible.
        assert outbox_rows[0]["kind"] == "heartbeat_stamp.v1"
        assert outbox_payload["heartbeat"]["device_id"] == "cinder"
        assert outbox_payload["heartbeat"]["received_at"] == now.isoformat()
        assert outbox_payload["catalog_result"]["commit_seq"] == "1"
        leases = {
            row["session_id"]: row["state"]
            for row in connection.execute(LiveControlLease.__table__.select()).mappings()
        }
        sessions = {
            row["session_id"]: row["state"] for row in connection.execute(LiveSession.__table__.select()).mappings()
        }
        assert leases == {current_id: "attached", missing_id: "missing"}
        assert sessions == {current_id: "attached", missing_id: "missing"}
    engine.dispose()


@pytest.mark.asyncio
async def test_heartbeat_apply_rejects_unbounded_or_malformed_contract(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        params = {
            "heartbeat": _heartbeat(device_id="cinder", received_at=now, digest="digest"),
            "managed_leases": [],
            "managed_leases_present": True,
            "owner_id": 7,
        }
        params["heartbeat"]["unexpected"] = True
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("machine.heartbeat.apply.v2", params)
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()
