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
        },
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.local.create.v2", {"launch": launch})
        assert created["created"] is True
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
