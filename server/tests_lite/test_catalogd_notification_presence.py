from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-notification-presence-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_notification_presence_and_visible_read(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    engine.dispose()
    now = datetime.now(UTC).replace(microsecond=0)
    params = {
        "owner_id": 7,
        "client_id": "browser-client-1",
        "client_type": "web",
        "visible": True,
        "route": "/timeline",
        "session_id": None,
        "observed_at": now.isoformat(),
    }

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("notification.presence.upsert.v2", params)
        replay = await client.call("notification.presence.upsert.v2", params)
        assert replay["commit_seq"] == created["commit_seq"]
        assert replay["presence"] == created["presence"]

        recent = await client.call(
            "notification.presence.visible.read.v2",
            {"owner_id": 7, "threshold": (now - timedelta(seconds=90)).isoformat()},
        )
        expired = await client.call(
            "notification.presence.visible.read.v2",
            {"owner_id": 7, "threshold": (now + timedelta(seconds=1)).isoformat()},
        )
        assert recent["visible"] is True
        assert expired["visible"] is False
    finally:
        await client.close()
        await daemon.close()
