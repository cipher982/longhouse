from __future__ import annotations

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
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.services.live_archive_outbox import MANAGED_LOCAL_LAUNCH_KIND


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-launch-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_managed_local_launch_transaction(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    launch = {
        "owner_id": 7,
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "plan": {
            "session_id": str(session_id),
            "provider": "claude",
            "provider_session_id": "claude-thread-1",
            "source_name": "cinder",
            "source_runner_id": None,
            "cwd": "/workspace/longhouse",
            "project": "longhouse",
            "display_name": "Managed local",
            "managed_session_name": "claude-managed-1",
            "loop_mode": "assist",
            "permission_mode": "bypass",
            "launch_actor": "human_ui",
            "launch_surface": "cli",
            "managed_transport": "claude_channel",
            "attach_command": "longhouse claude --resume claude-thread-1",
            "provider_config": {"permission_mode": "bypass"},
        },
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert created["created"] is True
        assert created["provider_session_id"] == "claude-thread-1"
        replay = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert replay["exact_replay"] is True
        conflicting = {**launch, "plan": {**launch["plan"], "cwd": "/different"}}
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.create.v2", {"launch": conflicting})
        assert exc_info.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        assert db.get(LiveSessionCatalog, str(session_id)) is not None
        connections = db.query(LiveSessionConnection).order_by(LiveSessionConnection.acquired_at).all()
        assert [row.state for row in connections] == ["detached"]
        runs = db.query(LiveSessionRun).order_by(LiveSessionRun.started_at).all()
        assert len(runs) == 1
        assert runs[0].ended_at is None
        assert db.get(LiveLaunchReadiness, str(session_id)).state == "pending"
        outbox = db.query(LiveArchiveOutbox).order_by(LiveArchiveOutbox.id).all()
        assert [row.kind for row in outbox] == [MANAGED_LOCAL_LAUNCH_KIND]
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_rejects_provider_session_identity_bound_to_another_thread(daemon_paths):
    database_path, socket_path = daemon_paths
    first_session_id = uuid4()
    second_session_id = uuid4()
    provider_session_id = "claude-native-session-1"
    first = _local_launch_payload(
        session_id=first_session_id,
        provider="claude",
        managed_transport="claude_channel",
        attach_command="longhouse claude --resume claude-native-session-1",
        provider_session_id=provider_session_id,
    )
    second = _local_launch_payload(
        session_id=second_session_id,
        provider="claude",
        managed_transport="claude_channel",
        attach_command="longhouse claude --resume claude-native-session-1",
        provider_session_id=provider_session_id,
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": first})
        assert created["created"] is True
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.create.v2", {"launch": second})
        assert exc_info.value.code == "conflict"
        assert "provider session identity" in str(exc_info.value)
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        assert db.get(LiveSessionCatalog, str(first_session_id)) is not None
        assert db.get(LiveSessionCatalog, str(second_session_id)) is None
        assert db.query(LiveSessionLaunchAttempt).count() == 1
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_state", "terminal_reason"),
    [("process_gone", "provider_exit"), ("session_ended", "user_closed")],
)
async def test_catalogd_resumes_ended_managed_thread_with_one_new_run(daemon_paths, terminal_state, terminal_reason):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    provider_thread_id = str(uuid4())
    launch = _local_launch_payload(
        session_id=session_id,
        provider="codex",
        managed_transport="codex_app_server",
        attach_command=f"longhouse codex attach --session-id {session_id}",
        provider_session_id=provider_thread_id,
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        initial = await client.call("session.launch.local.create.v2", {"launch": launch})
        ended_at = datetime.now(UTC)
        await client.call(
            "session.runtime.apply.v2",
            {
                "events": [
                    {
                        "runtime_key": f"codex:{session_id}",
                        "session_id": str(session_id),
                        "thread_id": None,
                        "run_id": initial["run_id"],
                        "provider": "codex",
                        "device_id": "cinder",
                        "source": "codex_bridge",
                        "kind": "terminal_signal",
                        "phase": "finished",
                        "tool_name": None,
                        "occurred_at": ended_at.isoformat(),
                        "freshness_ms": 60_000,
                        "dedupe_key": f"provider-exit:{initial['run_id']}",
                        "payload": {
                            "terminal_state": terminal_state,
                            "terminal_reason": terminal_reason,
                        },
                    }
                ]
            },
        )
        receipt_id = str(uuid4())
        await client.call(
            "session.input.receipt.upsert.v2",
            {
                "receipt": {
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "provider": "codex",
                    "text": "possibly stale input",
                    "intent": "queue",
                    "status": "queued",
                    "client_request_id": receipt_id,
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
        delivering_receipt_id = str(uuid4())
        await client.call(
            "session.input.receipt.upsert.v2",
            {
                "receipt": {
                    "owner_id": 7,
                    "session_id": str(session_id),
                    "provider": "codex",
                    "text": "input claimed by the crashed run",
                    "intent": "queue",
                    "status": "delivering",
                    "client_request_id": delivering_receipt_id,
                    "device_id": "cinder",
                    "thread_id": None,
                    "archive_session_input_id": None,
                    "control_command_id": None,
                    "delivery_request_id": "old-run-delivery",
                    "enqueue_archive_projection": False,
                    "error": None,
                    "expires_at": None,
                }
            },
        )
        attempt_id = uuid4()
        resume = {
            "owner_id": 7,
            "session_id": str(session_id),
            "provider": "codex",
            "provider_thread_id": provider_thread_id,
            "device_id": "cinder",
            "cwd": "/workspace/longhouse",
            "resume_attempt_id": str(attempt_id),
            "started_at": (ended_at + timedelta(seconds=1)).isoformat(),
            "expires_at": (ended_at + timedelta(minutes=5)).isoformat(),
        }
        resumed = await client.call("session.launch.local.resume.v2", {"resume": resume})
        assert resumed["created"] is True
        assert resumed["run_id"] != initial["run_id"]
        assert resumed["provider_session_id"] == provider_thread_id
        replay = await client.call("session.launch.local.resume.v2", {"resume": resume})
        assert replay["exact_replay"] is True
        assert replay["provider_session_id"] == provider_thread_id

        second = {**resume, "resume_attempt_id": str(uuid4())}
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.resume.v2", {"resume": second})
        assert exc_info.value.code == "conflict"
        assert "current run" in str(exc_info.value)
        confirmed = await client.call(
            "session.launch.local.finish.v2",
            {
                "outcome": {
                    "session_id": str(session_id),
                    "run_id": resumed["run_id"],
                    "owner_id": 7,
                    "device_id": "cinder",
                    "state": "adopted",
                    "error_code": None,
                    "error_message": None,
                    "observed_at": (ended_at + timedelta(seconds=2)).isoformat(),
                }
            },
        )
        assert confirmed["launch"]["launch_state"] == "live"

        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "session.launch.local.finish.v2",
                {
                    "outcome": {
                        "session_id": str(session_id),
                        "run_id": initial["run_id"],
                        "owner_id": 7,
                        "device_id": "cinder",
                        "state": "failed",
                        "error_code": "provider_launch_failed",
                        "error_message": "delayed retry from crashed generation",
                        "observed_at": (ended_at + timedelta(seconds=3)).isoformat(),
                    }
                },
            )
        assert exc_info.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        runs = db.query(LiveSessionRun).order_by(LiveSessionRun.started_at).all()
        assert [run.id for run in runs] == [initial["run_id"], resumed["run_id"]]
        assert runs[0].ended_at is not None
        assert runs[1].ended_at is None
        assert runs[1].launch_origin == "longhouse_continued"
        connections = db.query(LiveSessionConnection).order_by(LiveSessionConnection.id).all()
        assert [row.run_id for row in connections] == [initial["run_id"], resumed["run_id"]]
        assert [row.state for row in connections] == ["ended", "detached"]
        attempts = db.query(LiveSessionLaunchAttempt).order_by(LiveSessionLaunchAttempt.id).all()
        assert attempts[1].client_request_id == str(attempt_id)
        assert attempts[1].run_id == resumed["run_id"]
        assert attempts[1].state == "adopted"
        assert db.get(LiveLaunchReadiness, str(session_id)).state == "adopted"
        assert db.get(LiveSessionCatalog, str(session_id)).ended_at is None
        receipts = {row.client_request_id: row for row in db.query(LiveSessionInputReceipt).all()}
        assert receipts[receipt_id].status == "queued"
        assert receipts[receipt_id].error_json is None
        assert receipts[delivering_receipt_id].status == "failed"
        assert '"code": "delivery_unknown"' in receipts[delivering_receipt_id].error_json
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_resume_rejects_provider_thread_mismatch(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    launch = _local_launch_payload(
        session_id=session_id,
        provider="codex",
        managed_transport="codex_app_server",
        attach_command="",
        provider_session_id=str(uuid4()),
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        initial = await client.call("session.launch.local.create.v2", {"launch": launch})
        now = datetime.now(UTC)
        await client.call(
            "session.launch.local.finish.v2",
            {
                "outcome": {
                    "session_id": str(session_id),
                    "run_id": initial["run_id"],
                    "owner_id": 7,
                    "device_id": "cinder",
                    "state": "failed",
                    "error_code": "provider_launch_failed",
                    "error_message": None,
                    "observed_at": now.isoformat(),
                }
            },
        )
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "session.launch.local.resume.v2",
                {
                    "resume": {
                        "owner_id": 7,
                        "session_id": str(session_id),
                        "provider": "codex",
                        "provider_thread_id": str(uuid4()),
                        "device_id": "cinder",
                        "cwd": "/workspace/longhouse",
                        "resume_attempt_id": str(uuid4()),
                        "started_at": (now + timedelta(seconds=1)).isoformat(),
                        "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    }
                },
            )
        assert exc_info.value.code == "conflict"
        assert "primary thread" in str(exc_info.value)
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_confirms_registered_local_launch_exactly_once(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    launch = _local_launch_payload(
        session_id=session_id,
        provider="cursor",
        managed_transport="cursor_helm",
        attach_command="",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        outcome = {
            "session_id": str(session_id),
            "run_id": created["run_id"],
            "owner_id": 7,
            "device_id": "cinder",
            "state": "adopted",
            "error_code": None,
            "error_message": None,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        confirmed = await client.call("session.launch.local.finish.v2", {"outcome": outcome})
        assert confirmed["launch"]["launch_state"] == "live"
        replay = await client.call("session.launch.local.finish.v2", {"outcome": outcome})
        assert replay["exact_replay"] is True
        conflicting = {**outcome, "state": "failed", "error_code": "provider_launch_failed"}
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.finish.v2", {"outcome": conflicting})
        assert exc_info.value.code == "conflict"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        attempt = db.query(LiveSessionLaunchAttempt).one()
        assert attempt.state == "adopted"
        assert attempt.run_id == created["run_id"]
        assert db.get(LiveLaunchReadiness, str(session_id)).state == "adopted"
        assert db.get(LiveSessionRun, created["run_id"]).ended_at is None
    engine.dispose()


@pytest.mark.asyncio
async def test_catalogd_aborts_registered_local_launch_and_releases_control(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    launch = _local_launch_payload(
        session_id=session_id,
        provider="opencode",
        managed_transport="opencode_server_bridge",
        attach_command="",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        aborted = await client.call(
            "session.launch.local.finish.v2",
            {
                "outcome": {
                    "session_id": str(session_id),
                    "run_id": created["run_id"],
                    "owner_id": 7,
                    "device_id": "cinder",
                    "state": "failed",
                    "error_code": "provider_launch_failed",
                    "error_message": "scripted provider refused to start",
                    "observed_at": datetime.now(UTC).isoformat(),
                }
            },
        )
        assert aborted["launch"]["launch_state"] == "launch_failed"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        attempt = db.query(LiveSessionLaunchAttempt).one()
        assert attempt.state == "failed"
        assert attempt.error_code == "provider_launch_failed"
        assert db.get(LiveLaunchReadiness, str(session_id)).state == "failed"
        assert db.get(LiveSessionCatalog, str(session_id)).ended_at is not None
        assert db.get(LiveSessionRun, created["run_id"]).ended_at is not None
        connection = db.query(LiveSessionConnection).one()
        assert connection.state == "released"
        assert connection.released_at is not None
    engine.dispose()


def _local_launch_payload(
    *,
    session_id,
    provider: str,
    managed_transport: str,
    attach_command: object,
    provider_session_id: str | None = None,
):
    now = datetime.now(UTC)
    return {
        "owner_id": 7,
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "plan": {
            "session_id": str(session_id),
            "provider": provider,
            "provider_session_id": provider_session_id,
            "source_name": "cinder",
            "source_runner_id": None,
            "cwd": "/workspace/longhouse",
            "project": "longhouse",
            "display_name": "Managed local",
            "managed_session_name": f"{provider}-managed-1",
            "loop_mode": "assist",
            "permission_mode": "bypass",
            "launch_actor": "human_ui",
            "launch_surface": "cli",
            "managed_transport": managed_transport,
            "attach_command": attach_command,
            "provider_config": {},
        },
    }


@pytest.mark.asyncio
async def test_catalogd_local_launch_replays_when_retry_timestamps_differ(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    first = _local_launch_payload(
        session_id=session_id,
        provider="cursor",
        managed_transport="cursor_helm",
        attach_command="",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": first})
        assert created["created"] is True
        later = dict(first)
        later["started_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        later["expires_at"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        replay = await client.call("session.launch.local.create.v2", {"launch": later})
        assert replay["exact_replay"] is True
        assert replay["idempotency_conflict"] is False
        assert replay["launch"]["session_id"] == str(session_id)
        assert replay["provider_session_id"] is None
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_local_launch_replays_after_archive_drains_outbox(daemon_paths):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    launch = _local_launch_payload(
        session_id=session_id,
        provider="claude",
        managed_transport="claude_channel_bridge",
        attach_command="",
        provider_session_id="claude-thread-retry",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert created["created"] is True
        assert created["provider_session_id"] == "claude-thread-retry"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        outbox = db.query(LiveArchiveOutbox).filter_by(kind=MANAGED_LOCAL_LAUNCH_KIND).one()
        db.delete(outbox)
        db.commit()
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        replay = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert replay["created"] is False
        assert replay["exact_replay"] is True
        assert replay["idempotency_conflict"] is False
        assert replay["launch"]["session_id"] == str(session_id)
        assert replay["provider_session_id"] == "claude-thread-retry"

        # Plan fields that only ever lived in the consumed outbox payload must
        # still conflict once the durable fingerprint is the replay contract.
        for field, value in (
            ("attach_command", "longhouse claude --resume other-thread"),
            ("managed_transport", "claude_hook_inbox"),
            ("managed_session_name", "claude-managed-2"),
            ("launch_actor", "scheduler"),
            ("launch_surface", "web"),
            ("source_runner_id", 9),
        ):
            divergent = {**launch, "plan": {**launch["plan"], field: value}}
            with pytest.raises(CatalogRemoteError) as exc_info:
                await client.call("session.launch.local.create.v2", {"launch": divergent})
            assert exc_info.value.code == "conflict", field
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "managed_transport"),
    [
        ("cursor", "cursor_helm"),
        ("antigravity", "antigravity_hook_inbox"),
    ],
)
async def test_catalogd_local_launch_accepts_empty_attach_command(daemon_paths, provider, managed_transport):
    database_path, socket_path = daemon_paths
    session_id = uuid4()
    launch = _local_launch_payload(
        session_id=session_id,
        provider=provider,
        managed_transport=managed_transport,
        attach_command="",
    )
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert created["created"] is True
        replay = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert replay["exact_replay"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        catalog = db.get(LiveSessionCatalog, str(session_id))
        assert catalog is not None
        assert catalog.provider == provider
        attempt = db.query(LiveSessionLaunchAttempt).one()
        assert attempt.command_id == f"managed-local-{session_id}"
        assert db.query(LiveArchiveOutbox).filter_by(kind=MANAGED_LOCAL_LAUNCH_KIND).count() == 1
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attach_command",
    [None, 12, "x" * 4097],
)
async def test_catalogd_local_launch_rejects_invalid_attach_command(daemon_paths, attach_command):
    database_path, socket_path = daemon_paths
    launch = _local_launch_payload(
        session_id=uuid4(),
        provider="cursor",
        managed_transport="cursor_helm",
        attach_command=attach_command if attach_command is not None else "",
    )
    if attach_command is None:
        launch["plan"]["attach_command"] = None
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.create.v2", {"launch": launch})
        assert exc_info.value.code == "invalid_request"
        assert "attach_command" in str(exc_info.value)
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_local_launch_rejects_missing_attach_command(daemon_paths):
    database_path, socket_path = daemon_paths
    launch = _local_launch_payload(
        session_id=uuid4(),
        provider="cursor",
        managed_transport="cursor_helm",
        attach_command="",
    )
    del launch["plan"]["attach_command"]
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.launch.local.create.v2", {"launch": launch})
        assert exc_info.value.code == "invalid_request"
        assert "plan" in str(exc_info.value)
    finally:
        await client.close()
        await daemon.close()
