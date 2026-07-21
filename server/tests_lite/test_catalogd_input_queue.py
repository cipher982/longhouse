from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.fact_reducer import ReducerFact
from zerg.catalogd.fact_reducer import canonical_evidence_hash
from zerg.catalogd.fact_reducer import reduce_fact_batch
from zerg.catalogd.models import FactHead
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputAttachment
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_session_inputs import upsert_live_input_receipt


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-input-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_queue(engine, *, client_request_id="queued-1"):
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    adapter_connection_id = f"connection-{session_id}"
    lease_generation = f"lease-{session_id}"
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="codex",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/workspace/longhouse",
                started_at=now,
                last_activity_at=now,
                primary_thread_id=str(thread_id),
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider="codex",
                branch_kind="root",
                is_primary=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LiveSessionRun(
                id=str(run_id),
                thread_id=str(thread_id),
                provider="codex",
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=now,
            )
        )
        db.add(
            LiveSessionConnection(
                run_id=str(run_id),
                control_plane="codex_bridge",
                acquisition_kind="spawned_control",
                adapter_connection_id=adapter_connection_id,
                lease_generation=lease_generation,
                state="attached",
                device_id="cinder",
                can_send_input=1,
                acquired_at=now,
                last_health_at=now,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                provider="codex",
                device_id="cinder",
                phase="thinking",
                phase_source="test",
                timeline_anchor_at=now,
                runtime_version=1,
                updated_at=now,
            )
        )
        receipt = upsert_live_input_receipt(
            db,
            owner_id=7,
            session_id=session_id,
            provider="codex",
            text="continue the migration",
            intent="auto",
            status="queued",
            client_request_id=client_request_id,
            now=now,
        )
        db.commit()
        activity = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run_id),
            "kind": "idle",
            "raw_kind": "idle",
            "source": "provider_runtime",
            "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=2)).isoformat(),
        }
        control = {
            "authority_class": "provider_control",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run_id),
            "connection_id": adapter_connection_id,
            "lease_generation": lease_generation,
            "granted_operations": ["send_input"],
            "state": "attached",
            "lease_ttl_ms": 120_000,
            "source": "provider_control",
            "observed_at": now.isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{run_id}",
                    source="provider_runtime",
                    source_epoch=str(run_id),
                    source_seq=1,
                    dedupe_key="a" * 64,
                    evidence_hash=canonical_evidence_hash(activity),
                    value=activity,
                    observed_at=now,
                    session_id=str(session_id),
                    valid_until=now + timedelta(minutes=2),
                ),
                ReducerFact(
                    family="control",
                    subject_key=f"connection:{adapter_connection_id}:{lease_generation}",
                    source="provider_control",
                    source_epoch=lease_generation,
                    source_seq=1,
                    dedupe_key="b" * 64,
                    evidence_hash=canonical_evidence_hash(control),
                    value=control,
                    observed_at=now,
                    session_id=str(session_id),
                    valid_until=now + timedelta(minutes=2),
                ),
            ],
            received_at=now,
        )
        db.commit()
        return session_id, str(receipt.id)


def _replace_activity_head(engine, session_id, *, kind: str):
    observed_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    with Session(engine) as db:
        run = db.query(LiveSessionRun).one()
        value = {
            "authority_class": "provider_runtime",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run.id),
            "kind": kind,
            "raw_kind": kind,
            "source": "provider_runtime",
            "observed_at": observed_at.isoformat(),
            "valid_until": (observed_at + timedelta(minutes=2)).isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="activity",
                    subject_key=f"run:{run.id}",
                    source="provider_runtime",
                    source_epoch=str(run.id),
                    source_seq=2,
                    dedupe_key=canonical_evidence_hash({"kind": kind, "seq": 2}),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=str(session_id),
                    valid_until=observed_at + timedelta(minutes=2),
                )
            ],
            received_at=observed_at,
        )
        db.commit()


def _replace_control_grants(engine, session_id, *, granted_operations: list[str]):
    observed_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    with Session(engine) as db:
        run = db.query(LiveSessionRun).one()
        connection = db.query(LiveSessionConnection).one()
        value = {
            "authority_class": "provider_control",
            "provider": "codex",
            "session_id": str(session_id),
            "run_id": str(run.id),
            "connection_id": connection.adapter_connection_id,
            "lease_generation": connection.lease_generation,
            "granted_operations": granted_operations,
            "state": "attached",
            "lease_ttl_ms": 120_000,
            "source": "provider_control",
            "observed_at": observed_at.isoformat(),
        }
        reduce_fact_batch(
            db.connection(),
            [
                ReducerFact(
                    family="control",
                    subject_key=f"connection:{connection.adapter_connection_id}:{connection.lease_generation}",
                    source="provider_control",
                    source_epoch=str(connection.lease_generation),
                    source_seq=2,
                    dedupe_key=canonical_evidence_hash({"grants": granted_operations, "seq": 2}),
                    evidence_hash=canonical_evidence_hash(value),
                    value=value,
                    observed_at=observed_at,
                    session_id=str(session_id),
                    valid_until=observed_at + timedelta(minutes=2),
                )
            ],
            received_at=observed_at,
        )
        db.commit()


@pytest.mark.asyncio
async def test_catalogd_claims_and_finishes_queued_input_exactly_once(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        queued = await client.call("session.input.queued.list.v2", {"limit": 100})
        assert queued["session_ids"] == [str(session_id)]
        params = {"session_id": str(session_id), "delivery_request_id": "delivery-1"}
        claimed = await client.call("session.input.claim.v2", params)
        assert claimed["claimed"] is True, claimed
        assert claimed["receipt"]["id"] == receipt_id
        assert claimed["session"]["device_id"] == "cinder"
        replay = await client.call("session.input.claim.v2", params)
        assert replay["exact_replay"] is True
        finished = await client.call(
            "session.input.finish.v2",
            {
                "receipt_id": receipt_id,
                "delivery_request_id": "delivery-1",
                "status": "delivered",
                "error": None,
            },
        )
        assert finished["changed"] is True
        finish_replay = await client.call(
            "session.input.finish.v2",
            {
                "receipt_id": receipt_id,
                "delivery_request_id": "delivery-1",
                "status": "delivered",
                "error": None,
            },
        )
        assert finish_replay["changed"] is False
        upserted = await client.call(
            "session.input.receipt.upsert.v2",
            {
                "receipt": {
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "provider": "codex",
                    "text": "a second queued input",
                    "intent": "queue",
                    "status": "queued",
                    "client_request_id": "queued-2",
                    "device_id": "cinder",
                    "thread_id": None,
                    "archive_session_input_id": None,
                    "control_command_id": None,
                    "delivery_request_id": None,
                    "enqueue_archive_projection": False,
                    "error": None,
                    "expires_at": None,
                }
            },
        )
        second_id = upserted["receipt"]["id"]
        read = await client.call(
            "session.input.receipt.read.v2",
            {
                "owner_id": 7,
                "session_id": str(session_id),
                "client_request_id": "queued-2",
            },
        )
        assert read["receipt"]["id"] == second_id
        recent = await client.call("session.input.recent.list.v2", {"session_id": str(session_id)})
        assert recent["queued_count"] == 1
        assert [receipt["id"] for receipt in recent["receipts"]] == [second_id]
        cancelled = await client.call(
            "session.input.cancel.v2",
            {"session_id": str(session_id), "receipt_id": second_id},
        )
        assert cancelled["cancelled"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(LiveSessionInputReceipt, receipt_id).status == "delivered"
        assert db.query(LiveArchiveOutbox).count() == 0
    engine.dispose()


@pytest.fixture
def mutates_queue_db(tmp_path):
    engine = create_catalog_engine(tmp_path / "queue-denial.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine)
    yield engine, session_id
    engine.dispose()


def _delete_control_heads(engine):
    with Session(engine) as db:
        db.query(FactHead).filter(FactHead.family == "control").delete(synchronize_session=False)
        db.commit()


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (
            lambda engine, session_id: _replace_activity_head(engine, session_id, kind="running"),
            "activity_not_drainable",
        ),
        (lambda engine, _session_id: _delete_control_heads(engine), "control_unavailable"),
        (
            lambda engine, session_id: _replace_control_grants(engine, session_id, granted_operations=[]),
            "control_unavailable",
        ),
    ],
)
def test_catalogd_queue_claim_uses_canonical_activity_and_control(mutates_queue_db, mutate, expected_reason):
    engine, session_id = mutates_queue_db
    mutate(engine, session_id)

    result = CatalogStore(engine).claim_queued_input(
        session_id=str(session_id),
        delivery_request_id=f"denied-{expected_reason}",
    )

    assert result["claimed"] is False
    assert result["reason"] == expected_reason
    assert result["commit_seq"].isdigit()


def test_catalogd_blocked_activity_does_not_drain_collaboration_message(tmp_path):
    engine = create_catalog_engine(tmp_path / "blocked-collaboration.db")
    initialize_catalog_schema(engine)
    session_id, _receipt_id = _seed_queue(engine, client_request_id="session-message-42")
    _replace_activity_head(engine, session_id, kind="blocked")

    result = CatalogStore(engine).claim_queued_input(
        session_id=str(session_id),
        delivery_request_id="blocked-collaboration",
    )

    assert result["claimed"] is False
    assert result["reason"] == "activity_not_drainable"
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_attachment_metadata_is_receipt_scoped_and_bounded(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, receipt_id = _seed_queue(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    attachment_id = str(uuid4())
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    try:
        created = await client.call(
            "session.input.attachment.create.v2",
            {
                "attachment": {
                    "id": attachment_id,
                    "input_receipt_id": receipt_id,
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "mime_type": "image/png",
                    "byte_size": 67,
                    "sha256": "a" * 64,
                    "blob_path": f"{session_id}/{attachment_id}.bin",
                    "original_filename": "image.png",
                    "original_byte_size": 67,
                    "expires_at": expires_at.isoformat(),
                }
            },
        )
        assert created["created"] is True
        assert created["attachment"]["input_receipt_id"] == receipt_id

        lookup = {
            "owner_id": 7,
            "session_id": str(session_id),
            "input_receipt_id": receipt_id,
            "attachment_id": attachment_id,
        }
        found = await client.call("session.input.attachment.read.v2", lookup)
        assert found["found"] is True
        assert found["attachment"]["sha256"] == "a" * 64
        wrong_owner = await client.call("session.input.attachment.read.v2", {**lookup, "owner_id": 8})
        assert wrong_owner["found"] is False
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(LiveSessionInputAttachment, attachment_id).input_receipt_id == receipt_id
    engine.dispose()
