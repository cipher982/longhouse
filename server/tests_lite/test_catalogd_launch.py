from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.services.live_archive_outbox import REMOTE_LAUNCH_KIND
from zerg.services.live_archive_outbox import REMOTE_LAUNCH_OUTCOME_KIND


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-launch-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_launch_intent_idempotency_and_outcome(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = uuid4()
    launch = {
        "session_id": str(session_id),
        "primary_thread_id": str(uuid4()),
        "run_id": str(uuid4()),
        "owner_id": 7,
        "device_id": "cinder",
        "machine_id": "cinder",
        "provider": "codex",
        "cwd": "/workspace/longhouse",
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "project": "longhouse",
        "display_name": "Catalog launch",
        "initial_prompt": "finish the migration",
        "execution_lifetime": "live_control",
        "client_request_id": "launch-request-1",
        "command_id": f"launch-{session_id}",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
        "launch_actor": "human_ui",
        "launch_surface": "web",
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("session.launch.intent.create.v2", {"launch": launch})
        assert created["created"] is True
        assert created["launch"]["session_id"] == str(session_id)
        replay = await client.call("session.launch.intent.create.v2", {"launch": launch})
        assert replay["exact_replay"] is True
        identity = await client.call(
            "session.launch.idempotency.v2",
            {
                "owner_id": 7,
                "device_id": "cinder",
                "provider": "codex",
                "client_request_id": "launch-request-1",
            },
        )
        assert identity["found"] is True
        assert identity["launch"]["session_id"] == str(session_id)
        outcome = {"state": "dispatched", "error_code": None, "error_message": None}
        applied = await client.call(
            "session.launch.outcome.apply.v2",
            {"launch": launch, "outcome": outcome},
        )
        assert applied["changed"] is True
        outcome_replay = await client.call(
            "session.launch.outcome.apply.v2",
            {"launch": launch, "outcome": outcome},
        )
        assert outcome_replay["changed"] is False
        adopted = await client.call(
            "session.launch.outcome.apply.v2",
            {
                "launch": launch,
                "outcome": {"state": "adopted", "error_code": None, "error_message": None},
            },
        )
        assert adopted["launch"]["launch_state"] == "live"
        stale_dispatch = await client.call(
            "session.launch.outcome.apply.v2",
            {"launch": launch, "outcome": outcome},
        )
        assert stale_dispatch["changed"] is False
        assert stale_dispatch["launch"]["launch_state"] == "live"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with Session(engine) as db:
        assert db.get(LiveSessionCatalog, str(session_id)) is not None
        assert db.get(LiveLaunchReadiness, str(session_id)).state == "adopted"
        attempt = db.query(LiveSessionLaunchAttempt).filter_by(command_id=f"launch-{session_id}").one()
        assert attempt.state == "adopted"
        kinds = [row.kind for row in db.query(LiveArchiveOutbox).order_by(LiveArchiveOutbox.id)]
        assert kinds == [REMOTE_LAUNCH_KIND, REMOTE_LAUNCH_OUTCOME_KIND, REMOTE_LAUNCH_OUTCOME_KIND]
    engine.dispose()
