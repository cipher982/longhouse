from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import UUID
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveSessionCatalog
from zerg.routers import agents_sessions
from zerg.services.session_views import SessionActionRequest
from zerg.services.session_views import SessionLoopModeRequest
from zerg.services.session_views import SessionNotificationWatchRequest


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-preferences-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_session_preference_routes_are_catalog_owned_without_db(daemon_paths, monkeypatch):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    session_id = str(uuid4())
    now = datetime.now(UTC).replace(microsecond=0)
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="dev",
                started_at=now,
                user_state="active",
                loop_mode="assist",
                notification_muted=0,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    monkeypatch.setattr(agents_sessions.database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr("zerg.services.catalogd_supervisor.get_catalogd_client", lambda: client)
    try:
        action = await agents_sessions.set_session_action(
            session_id=UUID(session_id),
            body=SessionActionRequest(action="snooze"),
            db=None,
            _auth=None,
            _single=None,
        )
        loop = await agents_sessions.set_session_loop_mode(
            session_id=UUID(session_id),
            body=SessionLoopModeRequest(loop_mode="autopilot"),
            db=None,
            _auth=None,
            _single=None,
        )
        watch = await agents_sessions.set_session_notification_watch(
            session_id=UUID(session_id),
            body=SessionNotificationWatchRequest(notification_muted=True),
            db=None,
            _auth=None,
            _single=None,
        )
        assert action.user_state == "snoozed"
        assert loop.loop_mode.value == "autopilot"
        assert watch.notification_muted is True

        snapshot = await client.call("session.read.v2", {"session_id": session_id})
        catalog = snapshot["facts"]["catalog"]
        assert catalog["user_state"] == "snoozed"
        assert catalog["loop_mode"] == "autopilot"
        assert catalog["notification_muted"] is True
        assert snapshot["commit_seq"] == "3"
        replay = await client.call(
            "session.preferences.update.v2",
            {
                "session_id": session_id,
                "user_state": None,
                "loop_mode": None,
                "notification_muted": True,
                "user_hidden_from_timeline": None,
                "observed_at": now.isoformat(),
            },
        )
        assert replay["updated"] is False
        assert replay["commit_seq"] == "3"
    finally:
        await client.close()
        await daemon.close()
