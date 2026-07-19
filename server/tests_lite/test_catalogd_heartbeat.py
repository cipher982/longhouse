from __future__ import annotations

import hashlib
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import SQLAlchemyError

import zerg.catalogd.store as catalog_store
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import FactParityDelta
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.machine_evidence import canonical_evidence_hash
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


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


def _lease(
    *,
    session_id: str,
    observed_at: datetime,
    provider: str = "codex",
    state: str = "attached",
) -> dict:
    return {
        "session_id": session_id,
        "provider": provider,
        "machine_id": "cinder",
        "sequence": 4,
        "state": state,
        "phase": "idle",
        "tool_name": None,
        "bridge_status": "ready",
        "thread_subscription_status": "subscribed",
        "observed_at": observed_at.isoformat(),
        "lease_ttl_ms": 900_000,
    }


def _schema_v3_evidence(*, session_id: str, run_id: str, observed_at: datetime) -> dict:
    fact = {
        "authority_class": "provider_runtime",
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
        "schema_version": 3,
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


def _schema_v3_control_evidence(*, session_id: str, observed_at: datetime, run_id: str | None = None) -> dict:
    connection_id = str(uuid4())
    lease_generation = str(uuid4())
    fact = {
        "authority_class": "provider_control",
        "provider": "codex",
        "session_id": session_id,
        "provider_session_id": f"provider-{session_id}",
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "run_id": run_id or str(uuid4()),
        "granted_operations": [],
        "ownership": "managed",
        "state": "attached",
        "bridge_status": "ready",
        "thread_subscription_status": "subscribed",
        "lease_ttl_ms": 900_000,
        "source": "provider_control_scan",
        "observed_at": observed_at.isoformat(),
    }
    evidence_hash = canonical_evidence_hash(fact)
    return {
        "schema_version": 3,
        "control": [fact],
        "identities": [
            {
                "fact_family": "control",
                "fact_index": 0,
                "subject_key": f"connection:{connection_id}:{lease_generation}",
                "source": "provider_control_scan",
                "source_epoch": lease_generation,
                "source_seq": None,
                "sequenced": False,
                "dedupe_key": hashlib.sha256(f"{connection_id}:{lease_generation}".encode()).hexdigest(),
                "evidence_hash": evidence_hash,
            }
        ],
    }


def test_schema_v2_evidence_is_retained_but_not_shadow_reduced():
    evidence = _schema_v3_evidence(session_id=str(uuid4()), run_id=str(uuid4()), observed_at=datetime.now(UTC))
    evidence["schema_version"] = 2

    status, facts = catalog_store._shadow_facts_from_heartbeat(
        {"raw_json": json.dumps({"machine_evidence": evidence})},
    )

    assert status == "unsupported_schema"
    assert facts == []


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
async def test_shadow_reducer_uses_heartbeat_transaction_and_one_commit_sequence(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    run_id = str(uuid4())
    evidence = _schema_v3_evidence(session_id=session_id, run_id=run_id, observed_at=now)
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
        "identity_binding": {"bound": 0, "matched": 0, "unbound": 0, "mismatched": 0},
    }
    assert replay == {**first, "exact_replay": True}
    assert duplicate["commit_seq"] == "2"
    assert duplicate["shadow_reducer"]["changed_heads"] == 0
    assert duplicate["shadow_reducer"]["duplicates"] == 1

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        head = connection.execute(FactHead.__table__.select()).mappings().one()
        assert head["subject_key"] == f"run:{run_id}"
        assert head["session_id"] == session_id
        assert head["updated_commit_seq"] == 1
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).fetchall()
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_binds_control_identity_in_heartbeat_transaction(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    thread_id = str(uuid4())
    run_id = str(uuid4())
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="production",
                device_id="cinder",
                started_at=now,
                primary_thread_id=thread_id,
            )
        )
        connection.execute(
            LiveSessionThread.__table__.insert().values(
                id=thread_id,
                session_id=session_id,
                provider="codex",
                branch_kind="root",
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            LiveSessionRun.__table__.insert().values(
                id=run_id,
                thread_id=thread_id,
                provider="codex",
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        connection.execute(
            LiveSessionConnection.__table__.insert().values(
                run_id=run_id,
                control_plane="codex_bridge",
                acquisition_kind="spawned_control",
                state="attached",
                device_id="cinder",
                acquired_at=now,
                last_health_at=now,
            )
        )
    engine.dispose()

    evidence = _schema_v3_control_evidence(session_id=session_id, run_id=run_id, observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="control-identity")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "machine.heartbeat.apply.v2",
            {
                "heartbeat": heartbeat,
                "managed_leases": [_lease(session_id=session_id, observed_at=now)],
                "managed_leases_present": True,
                "owner_id": 7,
            },
        )
    finally:
        await client.close()
        await daemon.close()

    assert result["shadow_reducer"]["identity_binding"] == {
        "bound": 1,
        "matched": 0,
        "unbound": 0,
        "mismatched": 0,
    }
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        stored = connection.execute(LiveSessionConnection.__table__.select()).mappings().one()
    assert stored["adapter_connection_id"] == evidence["control"][0]["connection_id"]
    assert stored["lease_generation"] == evidence["control"][0]["lease_generation"]
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_reducer_validation_failure_preserves_legacy_heartbeat(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "true")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
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
    evidence = _schema_v3_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
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
    evidence = _schema_v3_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
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
async def test_shadow_reducer_has_no_source_disable_kill_switch(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_DISABLED_SOURCES", "phase_ledger")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
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
    assert result["shadow_reducer"]["changed_heads"] == 1
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactHead.__table__.select()).first() is not None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is not None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_parity_is_independent_and_upserts_bounded_candidate_delta(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.delenv("LONGHOUSE_SHADOW_PARITY_ENABLED", raising=False)
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_control_evidence(session_id=session_id, observed_at=now)
    evidence["control"][0]["state"] = "degraded"
    evidence["identities"][0]["evidence_hash"] = canonical_evidence_hash(evidence["control"][0])
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="parity-seed")
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
        seeded = await client.call("machine.heartbeat.apply.v2", params)
        monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "true")
        compared_params = {
            **params,
            "heartbeat": {
                **heartbeat,
                "received_at": (now + timedelta(seconds=1)).isoformat(),
                "sessions_digest": "parity-compare",
            },
        }
        compared = await client.call("machine.heartbeat.apply.v2", compared_params)
        repeated_params = {
            **compared_params,
            "heartbeat": {
                **compared_params["heartbeat"],
                "received_at": (now + timedelta(seconds=2)).isoformat(),
                "sessions_digest": "parity-repeat",
            },
        }
        repeated = await client.call("machine.heartbeat.apply.v2", repeated_params)
        stale_evidence = json.loads(json.dumps(evidence))
        stale_evidence["control"][0]["state"] = "attached"
        stale_evidence["control"][0]["observed_at"] = (now - timedelta(seconds=1)).isoformat()
        stale_evidence["identities"][0]["dedupe_key"] = hashlib.sha256(b"stale-position").hexdigest()
        stale_evidence["identities"][0]["evidence_hash"] = canonical_evidence_hash(stale_evidence["control"][0])
        stale_params = {
            **params,
            "heartbeat": {
                **heartbeat,
                "received_at": (now + timedelta(seconds=3)).isoformat(),
                "sessions_digest": "parity-stale",
                "raw_json": json.dumps({"machine_evidence": stale_evidence}),
            },
        }
        stale = await client.call("machine.heartbeat.apply.v2", stale_params)
        exact_replay = await client.call("machine.heartbeat.apply.v2", stale_params)
    finally:
        await client.close()
        await daemon.close()

    assert seeded["shadow_reducer"]["changed_heads"] == 1
    assert seeded["shadow_parity"] == {"status": "disabled"}
    assert compared["shadow_reducer"]["status"] == "applied"
    assert compared["shadow_reducer"]["duplicates"] == 1
    assert compared["shadow_parity"] == {
        "status": "compared",
        "compared_axes": 3,
        "deltas": 1,
        "missing_heads": 0,
        "unsupported_families": [],
    }
    assert repeated["shadow_parity"]["deltas"] == 0
    assert stale["shadow_reducer"]["stale"] == 1
    assert stale["shadow_parity"]["deltas"] == 0
    assert exact_replay == {**stale, "exact_replay": True}

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        delta = connection.execute(FactParityDelta.__table__.select()).mappings().one()
        assert delta["family"] == "control"
        assert delta["subject_key"] == evidence["identities"][0]["subject_key"]
        assert delta["source"] == "provider_control_scan"
        assert delta["source_epoch"] == evidence["identities"][0]["source_epoch"]
        assert delta["axis"] == "state"
        assert delta["reason"] == "value_mismatch"
        assert delta["commit_seq"] == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_parity_uses_normalized_legacy_control_rows(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_control_evidence(session_id=session_id, observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="normalized-parity")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [
            _lease(
                session_id=session_id,
                observed_at=now,
                provider=" CoDeX ",
                state=" ATTACHED ",
            )
        ],
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

    assert result["shadow_parity"] == {
        "status": "compared",
        "compared_axes": 3,
        "deltas": 0,
        "missing_heads": 0,
        "unsupported_families": [],
    }
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactParityDelta.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_parity_skips_when_legacy_snapshot_is_unavailable(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_control_evidence(session_id=session_id, observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="snapshot-seed")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    seed = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        await client.call("machine.heartbeat.apply.v2", seed)
        monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "0")
        monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "1")
        unavailable = await client.call(
            "machine.heartbeat.apply.v2",
            {
                **seed,
                "heartbeat": {
                    **heartbeat,
                    "received_at": (now + timedelta(seconds=1)).isoformat(),
                    "sessions_digest": "snapshot-unavailable",
                },
                "managed_leases": [],
                "managed_leases_present": False,
            },
        )
    finally:
        await client.close()
        await daemon.close()

    assert unavailable["shadow_parity"] == {"status": "legacy_unavailable"}
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactParityDelta.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_parity_failure_rolls_back_only_parity_savepoint(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "1")

    original_record_delta = catalog_store._record_shadow_parity_delta

    def fail_after_delta_insert(*args, **kwargs):
        original_record_delta(*args, **kwargs)
        raise SQLAlchemyError("forced parity write failure")

    monkeypatch.setattr(catalog_store, "_record_shadow_parity_delta", fail_after_delta_insert)
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_control_evidence(session_id=session_id, observed_at=now)
    evidence["control"][0]["state"] = "degraded"
    evidence["identities"][0]["evidence_hash"] = canonical_evidence_hash(evidence["control"][0])
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="parity-failure")
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

    assert result["shadow_reducer"]["changed_heads"] == 1
    assert result["shadow_parity"] == {"status": "failed", "reason": "database_error"}
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactParityDelta.__table__.select()).first() is None
        assert connection.execute(FactHead.__table__.select()).first() is not None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is not None
        assert connection.execute(LiveControlLease.__table__.select()).first() is not None
    engine.dispose()


@pytest.mark.asyncio
async def test_outer_rollback_does_not_advance_shadow_parity_count_cache(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_control_evidence(session_id=session_id, observed_at=now)
    evidence["control"][0]["state"] = "degraded"
    evidence["identities"][0]["evidence_hash"] = canonical_evidence_hash(evidence["control"][0])
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="outer-rollback")
    heartbeat["raw_json"] = json.dumps({"machine_evidence": evidence})
    params = {
        "heartbeat": heartbeat,
        "managed_leases": [_lease(session_id=session_id, observed_at=now)],
        "managed_leases_present": True,
        "owner_id": 7,
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    assert daemon._engine is not None

    def fail_result_receipt(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE live_heartbeat_stamps SET catalog_result_json"):
            raise SQLAlchemyError("forced outer rollback")

    event.listen(daemon._engine, "before_cursor_execute", fail_result_receipt)
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError):
            await client.call("machine.heartbeat.apply.v2", params)
        assert daemon._store is not None
        assert daemon._store._shadow_parity_delta_count is None
    finally:
        await client.close()
        event.remove(daemon._engine, "before_cursor_execute", fail_result_receipt)
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactParityDelta.__table__.select()).first() is None
        assert connection.execute(FactHead.__table__.select()).first() is None
        assert connection.execute(LiveHeartbeatStamp.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_shadow_parity_explicitly_reports_activity_as_unsupported(daemon_paths, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "1")
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    evidence = _schema_v3_evidence(session_id=session_id, run_id=str(uuid4()), observed_at=now)
    heartbeat = _heartbeat(device_id="cinder", received_at=now, digest="activity-unsupported")
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

    assert result["shadow_parity"] == {
        "status": "compared",
        "compared_axes": 0,
        "deltas": 0,
        "missing_heads": 0,
        "unsupported_families": [{"family": "activity", "reason": "canonical_projector_unavailable"}],
    }
    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        assert connection.execute(FactParityDelta.__table__.select()).first() is None
    engine.dispose()


def test_shadow_parity_deltas_are_globally_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_store, "_MAX_PARITY_DELTAS", 2)
    engine = create_catalog_engine(tmp_path / "bounded-parity.db")
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        for index in range(3):
            catalog_store._record_shadow_parity_delta(
                connection,
                fact=SimpleNamespace(
                    family="activity",
                    subject_key=f"run:{index}",
                    source="phase_ledger",
                    source_epoch=f"epoch-{index}",
                ),
                head_hash=hashlib.sha256(f"head-{index}".encode()).hexdigest(),
                axis="phase",
                legacy_value="idle",
                reason="value_mismatch",
                received_at=now + timedelta(seconds=index),
                commit_seq=index + 1,
            )
        retained = catalog_store._prune_shadow_parity_deltas(connection, known_delta_count=3)
        assert retained == 2

    with engine.connect() as connection:
        rows = connection.execute(FactParityDelta.__table__.select().order_by(FactParityDelta.commit_seq)).mappings()
        assert [row["commit_seq"] for row in rows] == [2, 3]
    engine.dispose()
