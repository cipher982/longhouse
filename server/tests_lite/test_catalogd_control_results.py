from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveMachineControlOperation


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-control-result-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_control_result_reconciles_operation_without_api_sqlite(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    operation_id = str(uuid4())
    operation_command_id = "managed-control:test-operation"
    with Session(engine) as db:
        db.add(
            LiveMachineControlOperation(
                id=operation_id,
                owner_id=7,
                device_id="cinder",
                command_type="session.send_text",
                command_id=operation_command_id,
                status="running",
                request_json="{}",
                timeout_secs=30,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=30),
            )
        )
        db.commit()
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        operation_params = {
            "owner_id": 7,
            "device_id": "cinder",
            "message": {
                "type": "command_result",
                "command_id": operation_command_id,
                "ok": True,
                "result": {"exit_code": 0, "stdout": "sent"},
            },
        }
        operation = await client.call("control.command_result.apply.v2", operation_params)
        operation_replay = await client.call("control.command_result.apply.v2", operation_params)
        assert operation == {"matched": True, "match_kind": "operation", "commit_seq": "1"}
        assert operation_replay == operation
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        operation_row = db.get(LiveMachineControlOperation, operation_id)
        assert operation_row.status == "succeeded"
        assert json.loads(operation_row.result_json)["stdout"] == "sent"
    engine.dispose()


@pytest.mark.asyncio
async def test_control_result_rejects_unbounded_message(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "control.command_result.apply.v2",
                {
                    "owner_id": 7,
                    "device_id": "cinder",
                    "message": {"command_id": "x" * 97, "ok": True},
                },
            )
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()
