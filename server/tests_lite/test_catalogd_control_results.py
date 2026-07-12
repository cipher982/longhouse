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
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.services.live_archive_outbox import REMOTE_LAUNCH_OUTCOME_KIND
from zerg.services.live_archive_outbox import enqueue_remote_launch_outbox
from zerg.services.live_catalog_launch import create_live_launch_catalog_shell
from zerg.services.live_launch_readiness import upsert_live_launch_readiness


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-control-result-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_control_result_reconciles_operation_and_late_launch_without_api_sqlite(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    operation_id = str(uuid4())
    operation_command_id = "managed-control:test-operation"
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    launch_command_id = f"launch-{session_id}"
    launch = {
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "run_id": str(run_id),
        "owner_id": 7,
        "provider": "codex",
        "device_id": "cinder",
        "machine_id": "cinder",
        "cwd": "/tmp/project",
        "project": "project",
        "execution_lifetime": "live_control",
        "launch_origin": "longhouse_spawned",
        "mode": "new",
    }
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
        create_live_launch_catalog_shell(
            db,
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            owner_id=7,
            provider="codex",
            device_id="cinder",
            device_name="cinder",
            cwd="/tmp/project",
            project="project",
            git_repo=None,
            git_branch=None,
            display_name="Catalog launch",
            initial_prompt="launch it",
            execution_lifetime="live_control",
            client_request_id="request-1",
            command_id=launch_command_id,
            started_at=now,
            expires_at=now + timedelta(minutes=5),
            launch_actor="user",
            launch_surface="web",
        )
        upsert_live_launch_readiness(
            db,
            session_id=session_id,
            owner_id=7,
            device_id="cinder",
            provider="codex",
            execution_lifetime="live_control",
            state="dispatched",
            command_id=launch_command_id,
            client_request_id="request-1",
            machine_id="cinder",
            project="project",
            expires_at=now + timedelta(minutes=5),
            now=now,
        )
        enqueue_remote_launch_outbox(db, launch=launch)
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

        launch_result = await client.call(
            "control.command_result.apply.v2",
            {
                "owner_id": 7,
                "device_id": "cinder",
                "message": {
                    "type": "command_result",
                    "command_id": launch_command_id,
                    "ok": True,
                    "result": {
                        "pid": 1234,
                        "provider_session_id": "provider-thread-1",
                        "thread_path": "/tmp/provider-thread.jsonl",
                    },
                },
            },
        )
        assert launch_result == {"matched": True, "match_kind": "launch", "commit_seq": "2"}
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with Session(engine) as db:
        operation_row = db.get(LiveMachineControlOperation, operation_id)
        assert operation_row.status == "succeeded"
        assert json.loads(operation_row.result_json)["stdout"] == "sent"
        readiness = db.get(LiveLaunchReadiness, str(session_id))
        assert readiness.state == "adopted"
        attempt = db.query(LiveSessionLaunchAttempt).filter_by(command_id=launch_command_id).one()
        assert attempt.state == "adopted"
        connection = db.query(LiveSessionConnection).filter_by(run_id=str(run_id)).one()
        assert connection.state == "attached"
        assert connection.can_send_input == 1
        outcome = db.query(LiveArchiveOutbox).filter_by(kind=REMOTE_LAUNCH_OUTCOME_KIND).one()
        assert json.loads(outcome.payload_json)["outcome"]["state"] == "adopted"
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
