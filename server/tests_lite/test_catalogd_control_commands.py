from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
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
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-control-command-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_control_grant(engine):
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
                device_id="cinder",
                started_at=now,
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
        connection = LiveSessionConnection(
            run_id=str(run_id),
            control_plane="codex_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_terminate=1,
            acquired_at=now,
            last_health_at=now,
        )
        db.add(connection)
        db.commit()
        return session_id, run_id, connection.id


@pytest.mark.asyncio
async def test_catalogd_prepares_and_finishes_control_command_atomically(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id, run_id, connection_id = _seed_control_grant(engine)
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    operation_id = str(uuid4())
    command_id = f"managed-control:{session_id}:session.send_text:req-1"
    params = {
        "operation_id": operation_id,
        "owner_id": 7,
        "session_id": str(session_id),
        "device_id": "cinder",
        "provider": "codex",
        "command_type": "session.send_text",
        "command_id": command_id,
        "capability": "send",
        "request_payload": {"session_id": str(session_id), "payload": {"text": "continue"}},
        "timeout_secs": 15,
    }
    try:
        prepared = await client.call("control.command.prepare.v2", params)
        assert prepared["allowed"] is True
        assert prepared["operation_id"] == operation_id
        assert prepared["grant"]["connection_id"] == connection_id
        assert prepared["grant"]["run_id"] == str(run_id)
        assert prepared["grant"]["lease_generation"].startswith(f"{connection_id}:")
        replay = await client.call("control.command.prepare.v2", params)
        assert replay["allowed"] is True
        assert replay["exact_replay"] is True
        assert replay["operation_id"] == operation_id
        assert replay["grant"] == prepared["grant"]
        finished = await client.call(
            "control.operation.finish.v2",
            {
                "operation_id": operation_id,
                "status": "succeeded",
                "result": {"exit_code": 0, "stdout": "accepted", "stderr": ""},
                "error": None,
            },
        )
        assert finished["found"] is True
        assert finished["changed"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        operation = db.get(LiveMachineControlOperation, operation_id)
        assert operation.status == "succeeded"
        assert json.loads(operation.request_json)["longhouse_control_grant"]["run_id"] == str(run_id)
        assert json.loads(operation.result_json)["stdout"] == "accepted"
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_control_prepare_fails_closed_without_current_grant(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        result = await client.call(
            "control.command.prepare.v2",
            {
                "operation_id": str(uuid4()),
                "owner_id": 7,
                "session_id": str(uuid4()),
                "device_id": "cinder",
                "provider": "codex",
                "command_type": "session.send_text",
                "command_id": f"managed-control:{uuid4()}:session.send_text:req-2",
                "capability": "send",
                "request_payload": {},
                "timeout_secs": 15,
            },
        )
        assert result["allowed"] is False
        assert result["reason"] == "control_unavailable"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_owns_provider_live_operation_lifecycle(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    operation_id = str(uuid4())
    params = {
        "operation_id": operation_id,
        "owner_id": 7,
        "device_id": "cinder",
        "provider": "claude",
        "command_type": "provider.live_proof",
        "command_id": f"machine-op:{operation_id}",
        "request_payload": {"provider": "claude", "publish": True},
        "timeout_secs": 135,
    }
    try:
        prepared = await client.call("machine.operation.prepare.v2", params)
        assert prepared["created"] is True
        assert prepared["operation"]["status"] == "running"
        replay = await client.call("machine.operation.prepare.v2", params)
        assert replay["exact_replay"] is True
        assert replay["commit_seq"] == prepared["commit_seq"]

        conflicting = {**params, "operation_id": str(uuid4())}
        conflicting["command_id"] = f"machine-op:{conflicting['operation_id']}"
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("machine.operation.prepare.v2", conflicting)
        assert exc_info.value.code == "conflict"

        running = await client.call(
            "machine.operation.read.v2",
            {"owner_id": 7, "operation_id": operation_id},
        )
        assert running["operation"]["request"]["provider"] == "claude"
        await client.call(
            "control.operation.finish.v2",
            {
                "operation_id": operation_id,
                "status": "failed",
                "result": None,
                "error": {"code": "proof_failed", "message": "no proof"},
            },
        )
        failed = await client.call(
            "machine.operation.read.v2",
            {"owner_id": 7, "operation_id": operation_id},
        )
        assert failed["operation"]["status"] == "failed"
        assert failed["operation"]["error"]["code"] == "proof_failed"
    finally:
        await client.close()
        await daemon.close()
