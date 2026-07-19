from __future__ import annotations

import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import SQLAlchemyError

import zerg.catalogd.store as catalog_store
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.models import FactHead
from zerg.machine_evidence import canonical_evidence_hash
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


def _schema_v2_evidence(*, session_id: str, run_id: str, observed_at: datetime) -> dict:
    fact = {
        "provider": "codex",
        "session_id": session_id,
        "run_id": run_id,
        "kind": "idle",
        "raw_kind": "idle",
        "source": "phase_ledger",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(minutes=2)).isoformat(),
    }
    evidence_hash = canonical_evidence_hash(fact)
    return {
        "schema_version": 2,
        "activity": [fact],
        "identities": [
            {
                "fact_family": "activity",
                "fact_index": 0,
                "subject_key": f"run:{run_id}",
                "source": "phase_ledger",
                "source_epoch": run_id,
                "source_seq": 1,
                "sequenced": True,
                "dedupe_key": hashlib.sha256(f"{run_id}:1".encode()).hexdigest(),
                "evidence_hash": evidence_hash,
            }
        ],
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
        assert connection.execute(LiveArchiveOutbox.__table__.select()).first() is None
        current_stamp = next(row for row in stamps if row["sessions_digest"] == "new-digest")
        assert len(current_stamp["request_sha256"]) == 64
        assert json.loads(current_stamp["catalog_result_json"])["commit_seq"] == "1"
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


@pytest.mark.asyncio
async def test_shadow_reducer_uses_heartbeat_transaction_and_one_commit_sequence(
    daemon_paths, monkeypatch
):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    run_id = str(uuid4())
    evidence = _schema_v2_evidence(session_id=session_id, run_id=run_id, observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="digest-1")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        first = await client.call("machine.heartbeat.apply.v2", params)
        replay = await client.call("machine.heartbeat.apply.v2", params)
        duplicate_params = {
            **params,
            "heartbeat": {
                **heartbeat,
                "received_at": (now + timedelta(seconds=1)).isoformat(),
                "sessions_digest": "digest-2",
            },
        }
        duplicate = await client.call("machine.heartbeat.apply.v2", duplicate_params)
    finally:
        await client.close()
        await daemon.close()

    assert first["commit_seq"] == "1"
    assert first["shadow_reducer"] == {
        "status": "applied",
        "changed_heads": 1,
        "duplicates": 0,
        "stale": 0,
        "conflicts": 0,
        "disabled_sources": 0,
    }
    assert replay == {**first, "exact_replay": True}
    assert duplicate["commit_seq"] == "2"
    assert duplicate["shadow_reducer"]["changed_heads"] == 0
    assert duplicate["shadow_reducer"]["duplicates"] == 1

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        head = connection.execute(FactHead.__table__.select()).mappings().one()
        assert head["subject_key"] == f"run:{run_id}"
        assert head["updated_commit_seq"] == 1
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).fetchall()
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_validation_failure_preserves_legacy_heartbeat(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "true")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v2_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
    evidence["identities"][0]["evidence_hash"] = "0" * 64
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="invalid-evidence")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("machine.heartbeat.apply.v2", params)
    finally:
        await client.close()
        await daemon.close()

    assert result["commit_seq"] == "1"
    assert result["shadow_reducer"] == {"status": "failed", "reason": "invalid_evidence"}
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactHead.__table__.select()).first() is None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is not None
        assert connection.execute(LiveControlLease.__table__.select()).first() is not None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_statement_failure_rolls_back_only_savepoint(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    original_reduce = catalog_store.reduce_fact_batch

    def fail_after_reducer_writes(*args, **kwargs):
        original_reduce(*args, **kwargs)
        raise SQLAlchemyError("forced reducer statement failure")

    monkeypatch.setattr(catalog_store, "reduce_fact_batch", fail_after_reducer_writes)
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v2_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="statement-failure")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("machine.heartbeat.apply.v2", params)
    finally:
        await client.close()
        await daemon.close()

    assert result["commit_seq"] == "1"
    assert result["shadow_reducer"] == {"status": "failed", "reason": "database_error"}
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactHead.__table__.select()).first() is None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is not None
        assert connection.execute(LiveControlLease.__table__.select()).first() is not None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_invalidated_connection_aborts_outer_heartbeat(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")

    def fail_with_invalidated_connection(*_args, **_kwargs):
        raise DBAPIError(
            "forced",
            {},
            RuntimeError("connection lost"),
            connection_invalidated=True,
        )

    monkeypatch.setattr(catalog_store, "reduce_fact_batch", fail_with_invalidated_connection)
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v2_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="invalidated")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError):
            await client.call("machine.heartbeat.apply.v2", params)
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactHead.__table__.select()).first() is None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is None
        assert connection.execute(LiveControlLease.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_disabled_source_skips_only_reducer_writes(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "yes")
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_DISABLED_SOURCES", "phase_ledger")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v2_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="disabled-source")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("machine.heartbeat.apply.v2", params)
    finally:
        await client.close()
        await daemon.close()

    assert result["commit_seq"] == "1"
    assert result["shadow_reducer"]["changed_heads"] == 0
    assert result["shadow_reducer"]["disabled_sources"] == 1
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactHead.__table__.select()).first() is None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is not None
    engine.dispose()
