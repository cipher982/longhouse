from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveMachineControlOperation


def test_catalog_store_reaps_expired_control_operations(tmp_path: Path):
    database_path = tmp_path / "live.db"
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    stale_id = str(uuid4())
    fresh_id = str(uuid4())
    with Session(engine) as db:
        db.add(
            LiveMachineControlOperation(
                id=stale_id,
                owner_id=7,
                device_id="cinder",
                command_type="session.send_text",
                command_id="managed-control:stale",
                status="running",
                request_json="{}",
                timeout_secs=30,
                created_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=5),
                expires_at=now - timedelta(seconds=1),
            )
        )
        db.add(
            LiveMachineControlOperation(
                id=fresh_id,
                owner_id=7,
                device_id="cinder",
                command_type="session.send_text",
                command_id="managed-control:fresh",
                status="running",
                request_json="{}",
                timeout_secs=30,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        db.commit()

    result = store.reap_stale_control_operations(now=now)
    assert result["reaped_count"] == 1

    with Session(engine) as db:
        stale = db.get(LiveMachineControlOperation, stale_id)
        fresh = db.get(LiveMachineControlOperation, fresh_id)
        assert stale is not None and stale.status == "timed_out"
        assert stale.expires_at is None
        assert fresh is not None and fresh.status == "running"
        assert fresh.expires_at is not None

    engine.dispose()
