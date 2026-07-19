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
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.models import FactHead
from zerg.catalogd.models import StorageSession
from zerg.catalogd.protocol import HEADER_BYTES
from zerg.catalogd.protocol import MAX_PAYLOAD_BYTES
from zerg.catalogd.protocol import CatalogRpcResponse
from zerg.catalogd.protocol import encode_frame
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLivePreview
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.services import catalog_read_gateway
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.session_state_diagnostics import compare_session_state_axes
from zerg.services.session_state_facts_projector import project_shadow_session_state_facts


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-reads-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_session(
    connection,
    *,
    session_id: str,
    device_id: str,
    now: datetime,
    project: str = "zerg",
    owner_id: str | None = None,
) -> None:
    thread_id = str(uuid4())
    run_id = str(uuid4())
    connection.execute(
        LiveSessionCatalog.__table__.insert().values(
            session_id=session_id,
            provider="codex",
            environment="prod",
            project=project,
            device_id=device_id,
            device_name="Cinder",
            cwd="/Users/david/git/zerg",
            git_repo="https://github.com/cipher982/longhouse.git",
            git_branch="main",
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(minutes=2),
            primary_thread_id=thread_id,
        )
    )
    connection.execute(
        LiveSession.__table__.insert().values(
            session_id=session_id,
            owner_id=owner_id,
            provider="codex",
            device_id=device_id,
            state="attached",
            started_at=now - timedelta(hours=2),
            last_seen_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        LiveTimelineCard.__table__.insert().values(
            session_id=session_id,
            provider="codex",
            environment="prod",
            project=project,
            device_id=device_id,
            cwd="/Users/david/git/zerg",
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(minutes=2),
            user_messages=2,
            assistant_messages=3,
            tool_calls=4,
            parser_revision="parser-v2",
        )
    )
    connection.execute(
        LiveSessionThread.__table__.insert().values(
            id=thread_id,
            session_id=session_id,
            provider="codex",
            is_primary=1,
            created_at=now - timedelta(hours=2),
            updated_at=now,
        )
    )
    connection.execute(
        LiveSessionRun.__table__.insert().values(
            id=run_id,
            thread_id=thread_id,
            provider="codex",
            host_id=device_id,
            launch_origin="longhouse_spawned",
            started_at=now - timedelta(hours=1),
        )
    )
    connection.execute(
        LiveSessionConnection.__table__.insert().values(
            run_id=run_id,
            control_plane="managed_local",
            acquisition_kind="launch_local",
            state="attached",
            device_id=device_id,
            can_send_input=1,
            acquired_at=now - timedelta(hours=1),
            last_health_at=now,
        )
    )
    connection.execute(
        LiveControlLease.__table__.insert().values(
            session_id=session_id,
            provider="codex",
            device_id=device_id,
            machine_id=device_id,
            state="attached",
            sequence=9,
            heartbeat_at=now,
            payload_json=json.dumps(
                {
                    "bridge_status": "ready",
                    "thread_subscription_status": "subscribed",
                    "lease_ttl_ms": 900000,
                }
            ),
            updated_at=now,
        )
    )
    connection.execute(
        LiveSessionThreadAlias.__table__.insert().values(
            thread_id=thread_id,
            provider="codex",
            alias_kind="provider_session_id",
            alias_value=f"provider-{session_id}",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    connection.execute(
        LiveSessionThreadAlias.__table__.insert().values(
            thread_id=thread_id,
            provider="codex",
            alias_kind="source_path",
            alias_value=f"/sessions/{session_id}.jsonl",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    connection.execute(
        LiveRuntimeState.__table__.insert().values(
            runtime_key=f"codex:{session_id}",
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            provider="codex",
            device_id=device_id,
            phase="quiescent",
            phase_source="hook",
            timeline_anchor_at=now,
            runtime_version=7,
            updated_at=now,
        )
    )
    connection.execute(
        LiveLaunchReadiness.__table__.insert().values(
            session_id=session_id,
            owner_id="7",
            provider="codex",
            device_id=device_id,
            execution_lifetime="live_control",
            state="adopted",
            created_at=now,
            updated_at=now,
        )
    )
    connection.execute(
        LiveSessionLivePreview.__table__.insert().values(
            session_id=session_id,
            thread_id=thread_id,
            turn_key=f"turn:{session_id}",
            seq=7,
            preview_text="Streaming output",
            provisional_cursor=f"turn:{session_id}:7",
            provisional_complete=0,
            event_origin="live_provisional",
            preview_observed_at=now,
            preview_updated_at=now,
            source="codex_bridge_live",
            last_observation_id=f"observation:{session_id}:7",
        )
    )


def _reducer_fact(*, family: str, session_id: str, now: datetime) -> ReducerFact:
    if family == "activity":
        value = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": session_id,
            "run_id": "run-shadow",
            "kind": "running",
            "raw_kind": "running",
            "tool_name": "Shell",
            "source": "provider_runtime",
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=2)).isoformat(),
        }
        return ReducerFact(
            family=family,
            subject_key="run:run-shadow",
            source="provider_runtime",
            source_epoch="run-shadow",
            source_seq=1,
            dedupe_key="a" * 64,
            evidence_hash=canonical_evidence_hash(value),
            value=value,
            observed_at=now,
            session_id=session_id,
            valid_until=now + timedelta(minutes=2),
        )
    value = {
        "authority_class": "provider_control",
        "provider": "codex",
        "session_id": session_id,
        "run_id": "run-shadow",
        "connection_id": "connection-shadow",
        "lease_generation": "lease-shadow",
        "granted_operations": ["interrupt", "send_input"],
        "state": "attached",
        "lease_ttl_ms": 120_000,
        "source": "provider_control",
        "observed_at": now.isoformat(),
    }
    return ReducerFact(
        family=family,
        subject_key="connection:connection-shadow:lease-shadow",
        source="provider_control",
        source_epoch="lease-shadow",
        source_seq=1,
        dedupe_key="b" * 64,
        evidence_hash=canonical_evidence_hash(value),
        value=value,
        observed_at=now,
        session_id=session_id,
        valid_until=now + timedelta(minutes=2),
    )


@pytest.mark.asyncio
async def test_canonical_session_reads_prefer_storage_v2_facts_without_legacy_catalog_rows(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert().values(
                session_id=session_id,
                tenant_id="default",
                owner_id="42",
                provider="codex",
                environment="prod",
                machine_id="cinder",
                project="longhouse",
                cwd="/workspace/longhouse",
                git_repo="cipher982/longhouse",
                git_branch="main",
                started_at=now - timedelta(hours=1),
                last_activity_at=now,
                user_messages=2,
                assistant_messages=3,
                tool_calls=4,
                summary_title="Storage v2 session",
                first_user_message_preview="Migrate the database",
                last_visible_text_preview="Done",
                transcript_revision=7,
                current_render_generation=str(uuid4()),
                raw_state="durable",
                render_state="ready",
                media_state="complete",
                commit_seq=7,
                created_at=now,
                updated_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        read = await client.call("session.read.v2", {"session_id": session_id})
        assert read["found"] is True
        assert read["facts"]["catalog"]["device_id"] == "cinder"
        assert read["facts"]["catalog"]["summary_title"] == "Storage v2 session"
        assert read["facts"]["card"]["archive_state"] == "current"
        assert read["facts"]["card"]["tool_calls"] == 4
        timeline = await client.call(
            "session.timeline.list.v2",
            {
                "project": "longhouse",
                "provider": None,
                "environment": None,
                "include_test": False,
                "hide_autonomous": True,
                "include_automation": False,
                "device_id": None,
                "days_back": 7,
                "limit": 20,
                "offset": 0,
            },
        )
        assert timeline["total"] == 1
        assert timeline["rows"][0]["facts"]["catalog"]["session_id"] == session_id
    finally:
        await client.close()
        await daemon.close()


def test_catalog_gateway_normalizes_missing_file_backing(monkeypatch):
    monkeypatch.setattr(
        catalog_read_gateway,
        "catalogd_paths",
        lambda: (_ for _ in ()).throw(RuntimeError("not file backed")),
    )
    with pytest.raises(catalog_read_gateway.CatalogReadError, match="temporarily unavailable"):
        catalog_read_gateway.active_owner_id()


@pytest.mark.asyncio
async def test_active_owner_read_is_catalog_owned(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {
                    "id": 1,
                    "email": "service@example.com",
                    "role": "USER",
                    "provider": "service",
                    "is_active": True,
                },
                {
                    "id": 7,
                    "email": "owner@example.com",
                    "role": "ADMIN",
                    "provider": None,
                    "is_active": True,
                },
            ],
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("auth.owner.get.v2", {})
        assert result["found"] is True
        assert result["owner_id"] == 7
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_session_timeline_and_read_return_assembled_snapshot_facts(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    pending_id = "33333333-3333-4333-8333-333333333333"
    with engine.begin() as connection:
        _seed_session(connection, session_id=first_id, device_id="cinder", now=now)
        _seed_session(connection, session_id=second_id, device_id="clifford", now=now - timedelta(hours=1))
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=pending_id,
                provider="codex",
                environment="prod",
                project="zerg",
                device_id="cinder",
                started_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "session.timeline.list.v2",
            {
                "project": "zerg",
                "provider": None,
                "environment": None,
                "include_test": False,
                "hide_autonomous": True,
                "include_automation": False,
                "device_id": None,
                "days_back": 7,
                "limit": 1,
                "offset": 0,
            },
        )
        assert result["commit_seq"] == "0"
        assert result["total"] == 2
        assert result["has_real_sessions"] is True
        assert len(result["rows"]) == 1
        facts = result["rows"][0]["facts"]
        assert facts["catalog"]["session_id"] == first_id
        assert facts["card"]["tool_calls"] == 4
        assert facts["runtime"]["phase"] == "quiescent"
        assert facts["readiness"]["state"] == "adopted"
        assert facts["primary_thread"]["id"] == facts["catalog"]["primary_thread_id"]
        assert facts["latest_run"]["thread_id"] == facts["primary_thread"]["id"]
        assert facts["connections"][0]["can_send_input"] == 1
        assert facts["provider_alias"] is None
        assert facts["resume"] is None
        assert "display_phase" not in facts and "status" not in facts

        read = await client.call("session.read.v2", {"session_id": first_id})
        assert read["found"] is True
        assert read["facts"]["catalog"]["session_id"] == first_id
        assert read["facts"]["control_leases"][0]["sequence"] == 9
        assert read["facts"]["live_preview"]["preview_text"] == "Streaming output"
        assert read["facts"]["provider_alias"] == f"provider-{first_id}"
        assert read["facts"]["resume"] == {
            "provider_session_id": f"provider-{first_id}",
            "source_path": f"/sessions/{first_id}.jsonl",
            "ever_managed": True,
        }
        assert read["observed_at"].endswith("+00:00")
        batch = await client.call("session.read.batch.v2", {"session_ids": [first_id, second_id]})
        assert [item["catalog"]["session_id"] for item in batch["facts"]] == [first_id, second_id]
        active = await client.call(
            "session.active.list.v2",
            {"limit": 10, "days_back": 7, "observed_at": now.isoformat()},
        )
        assert active["session_ids"] == [first_id, second_id]
        pending = await client.call("session.read.v2", {"session_id": pending_id})
        assert pending["found"] is True
        assert pending["facts"]["catalog"]["session_id"] == pending_id
        assert pending["facts"]["card"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_shadow_state_read_is_owner_scoped_and_commit_coherent(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = "44444444-4444-4444-8444-444444444444"
    ownerless_session_id = "55555555-5555-4555-8555-555555555555"
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {
                    "id": 7,
                    "email": "owner@example.com",
                    "role": "ADMIN",
                    "provider": None,
                    "is_active": True,
                },
                {
                    "id": 8,
                    "email": "other@example.com",
                    "role": "USER",
                    "provider": None,
                    "is_active": True,
                },
            ],
        )
        _seed_session(connection, session_id=session_id, device_id="cinder", now=now, owner_id="7")
        _seed_session(connection, session_id=ownerless_session_id, device_id="cinder", now=now)
        reduced = reduce_fact_batch(
            connection,
            [
                _reducer_fact(family="activity", session_id=session_id, now=now),
                _reducer_fact(family="control", session_id=session_id, now=now),
            ],
            received_at=now,
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "session.shadow_state.read.v2",
            {"session_id": session_id, "owner_id": 7},
        )
        assert result["found"] is True
        assert result["commit_seq"] == str(reduced.commit_seq)
        assert result["head_count"] == 2
        assert result["legacy_facts"]["catalog"]["session_id"] == session_id
        assert {head["family"] for head in result["heads"]} == {"activity", "control"}
        activity = next(head for head in result["heads"] if head["family"] == "activity")
        assert json.loads(activity["value_json"])["tool_name"] == "Shell"
        assert activity["updated_commit_seq"] == reduced.commit_seq
        observed_at = datetime.fromisoformat(result["observed_at"])
        legacy = project_catalog_session_facts(result["legacy_facts"], observed_at=observed_at).session_state
        shadow = project_shadow_session_state_facts(
            session_id=session_id,
            commit_seq=int(result["commit_seq"]),
            catalog_facts=result["legacy_facts"],
            heads=result["heads"],
            supported_operations={"send_input", "interrupt", "terminate", "resume"},
            now=observed_at,
        )
        comparison = compare_session_state_axes(
            legacy=legacy,
            shadow=shadow,
            legacy_commit_seq=int(result["commit_seq"]),
            shadow_commit_seq=shadow.commit_seq,
        )
        assert shadow.activity.state == "executing"
        assert shadow.control is not None and shadow.control.actions.send_input.state == "available"
        assert comparison.status == "different"
        assert comparison.same_commit is True

        hidden = await client.call(
            "session.shadow_state.read.v2",
            {"session_id": session_id, "owner_id": 8},
        )
        assert hidden["found"] is False
        assert hidden["heads"] == []
        ownerless = await client.call(
            "session.shadow_state.read.v2",
            {"session_id": ownerless_session_id, "owner_id": 7},
        )
        assert ownerless["found"] is False
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "session.shadow_state.read.v2",
                {"session_id": session_id, "owner_id": "7"},
            )
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()


def test_shadow_state_read_bounds_combined_rpc_payload(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = "66666666-6666-4666-8666-666666666666"
    padded_value = json.dumps({"padding": "x" * 3_900})
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert().values(
                id=7,
                email="owner@example.com",
                role="ADMIN",
                is_active=True,
            )
        )
        _seed_session(connection, session_id=session_id, device_id="cinder", now=now, owner_id="7")
        connection.execute(
            FactHead.__table__.insert(),
            [
                {
                    "family": "activity",
                    "subject_key": f"run:bounded-{index}",
                    "source": "provider_runtime",
                    "source_epoch": f"epoch-{index}",
                    "session_id": session_id,
                    "ordering_mode": "sequenced",
                    "source_seq": index,
                    "evidence_hash": f"{index:064x}",
                    "observed_at": now,
                    "valid_until": now + timedelta(minutes=2),
                    "value_json": padded_value,
                    "updated_commit_seq": index,
                    "received_at": now,
                }
                for index in range(257)
            ],
        )

    result = CatalogStore(engine).read_shadow_session_state(session_id=session_id, owner_id=7)
    frame = encode_frame(CatalogRpcResponse(id="f" * 32, result=result))
    engine.dispose()

    assert result["found"] is True
    assert result["heads_truncated"] is True
    assert result["head_count"] == 256
    assert len(frame) < HEADER_BYTES + MAX_PAYLOAD_BYTES


@pytest.mark.asyncio
async def test_shadow_state_health_summarizes_bounded_recent_outcomes(daemon_paths, monkeypatch):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = "77777777-7777-4777-8777-777777777777"
    monkeypatch.setenv("LONGHOUSE_SHADOW_REDUCER_INGEST_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_SHADOW_PARITY_ENABLED", "true")
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {
                    "id": 7,
                    "email": "owner@example.com",
                    "role": "ADMIN",
                    "provider": None,
                    "is_active": True,
                },
                {
                    "id": 8,
                    "email": "service@example.com",
                    "role": "USER",
                    "provider": "service",
                    "is_active": True,
                },
            ],
        )
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id=str(uuid4()),
                owner_id=7,
                device_id="cinder",
                token_hash="d" * 64,
                created_at=now,
            )
        )
        _seed_session(connection, session_id=session_id, device_id="cinder", now=now, owner_id="7")
        reduce_fact_batch(
            connection,
            [
                _reducer_fact(family="activity", session_id=session_id, now=now),
                _reducer_fact(family="control", session_id=session_id, now=now),
            ],
            received_at=now,
        )
        connection.execute(
            LiveSession.__table__.insert().values(
                session_id="88888888-8888-4888-8888-888888888888",
                owner_id="8",
                provider="codex",
                device_id="service-device",
                state="attached",
                last_seen_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            FactHead.__table__.insert().values(
                family="activity",
                subject_key="run:service-run",
                source="provider_runtime",
                source_epoch="service-run",
                session_id="88888888-8888-4888-8888-888888888888",
                ordering_mode="sequenced",
                source_seq=1,
                evidence_hash="e" * 64,
                observed_at=now,
                valid_until=now + timedelta(minutes=2),
                value_json="{}",
                updated_commit_seq=1,
                received_at=now,
            )
        )
        connection.execute(
            LiveHeartbeatStamp.__table__.insert(),
            [
                {
                    "device_id": "cinder",
                    "received_at": now,
                    "catalog_result_json": json.dumps(
                        {
                            "shadow_reducer": {
                                "status": "applied",
                                "changed_heads": 2,
                                "duplicates": 1,
                                "stale": 0,
                                "conflicts": 0,
                            },
                            "shadow_parity": {
                                "status": "compared",
                                "deltas": 1,
                                "missing_heads": 0,
                            },
                        }
                    ),
                },
                {
                    "device_id": "cinder",
                    "received_at": now - timedelta(seconds=1),
                    "catalog_result_json": json.dumps(
                        {
                            "shadow_reducer": {"status": "failed"},
                            "shadow_parity": {"status": "failed"},
                        }
                    ),
                },
                {
                    "device_id": "cinder",
                    "received_at": now - timedelta(seconds=2),
                    "catalog_result_json": json.dumps(
                        {
                            "shadow_reducer": {"status": "applied", "changed_heads": True},
                            "shadow_parity": {"status": "compared"},
                        }
                    ),
                },
                {
                    "device_id": "service-device",
                    "received_at": now,
                    "catalog_result_json": json.dumps(
                        {
                            "shadow_reducer": {"status": "applied", "changed_heads": 99},
                            "shadow_parity": {"status": "compared", "deltas": 99},
                        }
                    ),
                },
            ],
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call("session.shadow_state.health.v2", {"owner_id": 7})
        assert result["found"] is True
        assert result["ingest_enabled"] is True and result["parity_enabled"] is True
        assert result["storage"]["head_counts"] == {"activity": 1, "control": 1}
        assert result["storage"]["head_capacity_per_family"] == 2_048
        assert result["recent_batches"]["sample_size"] == 3
        assert result["recent_batches"]["sample_limit"] == 100
        assert result["recent_batches"]["window_seconds"] == 900
        assert result["recent_batches"]["truncated"] is False
        assert result["recent_batches"]["oldest_received_at"] is not None
        assert result["recent_batches"]["malformed_results"] == 1
        assert result["recent_batches"]["reducer_status_counts"] == {"applied": 1, "failed": 1}
        assert result["recent_batches"]["parity_status_counts"] == {"compared": 1, "failed": 1}
        assert result["recent_batches"]["changed_heads"] == 2
        assert result["recent_batches"]["duplicates"] == 1
        assert result["recent_batches"]["parity_deltas"] == 1
        assert (await client.call("session.shadow_state.health.v2", {"owner_id": 8}))["found"] is False
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_session_read_validation_and_prefix_missing_ambiguous_found(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    first_id = "aaaaaaaa-1111-4111-8111-111111111111"
    second_id = "aaaaaaaa-2222-4222-8222-222222222222"
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert(),
            [
                {
                    "email": "david010@example.com",
                    "display_name": " David Rose ",
                    "is_active": True,
                },
                {
                    "email": "other@example.com",
                    "display_name": "Other User",
                    "is_active": True,
                },
            ],
        )
        _seed_session(connection, session_id=first_id, device_id="cinder", now=now)
        _seed_session(connection, session_id=second_id, device_id="cinder", now=now)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        missing_prefix = await client.call("session.prefix.resolve.v2", {"prefix": "bbbb"})
        assert missing_prefix["status"] == "missing"
        assert missing_prefix["session"] is None and missing_prefix["owner"] is None
        ambiguous = await client.call("session.prefix.resolve.v2", {"prefix": "aaaaaaaa"})
        assert ambiguous["status"] == "ambiguous" and ambiguous["session_id"] is None
        assert ambiguous["session"] is None and ambiguous["owner"] is None
        found = await client.call("session.prefix.resolve.v2", {"prefix": "aaaaaaaa-1111"})
        assert found["status"] == "unique" and found["session_id"] == first_id
        assert found["session"] == {
            "session_id": first_id,
            "provider": "codex",
            "device_name": "Cinder",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "ended_at": None,
        }
        assert found["owner"] == {"display_name": "David Rose", "email_local": "david010"}
        assert set(found["session"]) == {"session_id", "provider", "device_name", "started_at", "ended_at"}
        missing = await client.call("session.read.v2", {"session_id": str(uuid4())})
        assert missing["found"] is False and missing["facts"] is None
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.read.v2", {"session_id": "not-a-uuid"})
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_enrollment_excludes_revoked_and_workspaces_are_owner_device_scoped(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveDeviceToken.__table__.insert(),
            [
                {
                    "id": str(uuid4()),
                    "owner_id": 7,
                    "device_id": "cinder",
                    "token_hash": "a" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
                {
                    "id": str(uuid4()),
                    "owner_id": 7,
                    "device_id": "old",
                    "token_hash": "b" * 64,
                    "created_at": now,
                    "revoked_at": now,
                },
                {
                    "id": str(uuid4()),
                    "owner_id": 8,
                    "device_id": "private",
                    "token_hash": "c" * 64,
                    "created_at": now,
                    "revoked_at": None,
                },
            ],
        )
        _seed_session(connection, session_id=str(uuid4()), device_id="cinder", now=now)
        _seed_session(connection, session_id=str(uuid4()), device_id="private", now=now, project="secret")
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        enrollments = await client.call("machine.enrollment.list.v2", {"owner_id": 7})
        assert [row["device_id"] for row in enrollments["enrollments"]] == ["cinder"]
        workspaces = await client.call(
            "machine.workspace.list.v2",
            {"owner_id": 7, "device_id": "cinder", "limit": 12, "days_back": 45},
        )
        assert [row["path"] for row in workspaces["workspaces"]] == ["/Users/david/git/zerg"]
        assert workspaces["workspaces"][0]["label"] == "longhouse (main)"
        assert (
            await client.call(
                "machine.workspace.list.v2",
                {"owner_id": 7, "device_id": "private", "limit": 12, "days_back": 45},
            )
        )["workspaces"] == []
        assert (
            await client.call(
                "machine.workspace.list.v2",
                {"owner_id": 7, "device_id": "old", "limit": 12, "days_back": 45},
            )
        )["workspaces"] == []
    finally:
        await client.close()
        await daemon.close()


def test_maximum_timeline_page_fits_one_protocol_frame(daemon_paths):
    database_path, _socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    oversized = "🚀" * 40_000
    session_id = "ffffffff-1111-4111-8111-ffffffffffff"
    with engine.begin() as connection:
        _seed_session(connection, session_id=session_id, device_id="cinder", now=now)
        connection.execute(
            LiveSessionCatalog.__table__.update()
            .where(LiveSessionCatalog.session_id == session_id)
            .values(
                cwd=oversized,
                git_repo=oversized,
                summary=oversized,
                first_user_message_preview=oversized,
            )
        )
        connection.execute(
            LiveTimelineCard.__table__.update()
            .where(LiveTimelineCard.session_id == session_id)
            .values(cwd=oversized, first_user_message_preview=oversized)
        )
        connection.execute(
            LiveRuntimeState.__table__.update()
            .where(LiveRuntimeState.runtime_key == f"codex:{session_id}")
            .values(
                pending_interaction_id=oversized,
                pending_interaction_kind="structured_question",
                pending_interaction_projection_json={
                    "id": oversized,
                    "request_key": oversized,
                    "summary": oversized,
                    "questions": [
                        {
                            "id": oversized,
                            "header": oversized,
                            "question": oversized,
                            "options": [{"label": oversized, "description": oversized, "value": oversized} for _ in range(20)],
                        }
                        for _ in range(20)
                    ],
                },
            )
        )
        connection.execute(
            LiveLaunchReadiness.__table__.update().where(LiveLaunchReadiness.session_id == session_id).values(error_message=oversized)
        )
        connection.execute(
            LiveSessionLivePreview.__table__.update()
            .where(LiveSessionLivePreview.session_id == session_id)
            .values(preview_text=oversized, provisional_cursor=oversized)
        )
        thread_id = connection.execute(
            LiveSessionCatalog.__table__.select()
            .with_only_columns(LiveSessionCatalog.primary_thread_id)
            .where(LiveSessionCatalog.session_id == session_id)
        ).scalar_one()
        run_id = connection.execute(
            LiveSessionRun.__table__.select().with_only_columns(LiveSessionRun.id).where(LiveSessionRun.thread_id == thread_id)
        ).scalar_one()
        for connection_index in range(7):
            connection.execute(
                LiveSessionConnection.__table__.insert().values(
                    run_id=run_id,
                    control_plane=f"plane-{connection_index}-{oversized}",
                    acquisition_kind="spawned_control",
                    state="attached",
                    device_id=oversized,
                    can_send_input=1,
                    can_interrupt=1,
                    can_terminate=1,
                    can_tail_output=1,
                    can_resume=1,
                    acquired_at=now,
                    last_health_at=now,
                )
            )

    result = CatalogStore(engine).list_session_timeline(
        project=None,
        provider=None,
        environment=None,
        include_test=False,
        hide_autonomous=False,
        include_automation=True,
        device_id=None,
        days_back=90,
        limit=100,
        offset=0,
    )
    detail = CatalogStore(engine).read_session(session_id=session_id)
    result["rows"] *= 100
    result["total"] = 100
    response = CatalogRpcResponse(id="0" * 32, result=result)
    payload_bytes = len(json.dumps(response.to_wire(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    batch_response = CatalogRpcResponse(
        id="1" * 32,
        result={
            "commit_seq": detail["commit_seq"],
            "observed_at": detail["observed_at"],
            "facts": [detail["facts"]] * 20,
        },
    )
    batch_payload_bytes = len(
        json.dumps(batch_response.to_wire(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    )
    row_sizes = {
        key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        for key, value in result["rows"][0]["facts"].items()
    }
    assert payload_bytes < MAX_PAYLOAD_BYTES, (payload_bytes, row_sizes)
    assert batch_payload_bytes < MAX_PAYLOAD_BYTES, batch_payload_bytes
    frame = encode_frame(response)
    engine.dispose()

    assert len(result["rows"]) == 100
    assert len(result["rows"][0]["facts"]["runtime"]["pending_interaction_projection_json"]["questions"]) == 3
    assert len(detail["facts"]["runtime"]["pending_interaction_projection_json"]["questions"]) == 3
    assert len(frame) - HEADER_BYTES < MAX_PAYLOAD_BYTES
