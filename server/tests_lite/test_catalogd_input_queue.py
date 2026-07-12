from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
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


def _seed_queue(engine):
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
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
                phase="idle",
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
            client_request_id="queued-1",
            now=now,
        )
        db.commit()
        return session_id, str(receipt.id)


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
        assert claimed["claimed"] is True
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
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        assert db.get(LiveSessionInputReceipt, receipt_id).status == "delivered"
        assert db.query(LiveArchiveOutbox).count() == 1
    engine.dispose()
