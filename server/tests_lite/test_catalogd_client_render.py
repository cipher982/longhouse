"""Catalog-owned browser/iOS pixel receipt tests."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.server import CatalogDaemon


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-client-render-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_client_render_receipts_are_idempotent_and_queryable(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    session_id = str(uuid4())
    now = datetime.now(UTC).replace(microsecond=0)
    receipt = {
        "observation_id": f"client_render:web:{session_id}:evt-1:123",
        "session_id": session_id,
        "event_id": "evt-1",
        "surface": "web",
        "payload": {"event_id": "evt-1", "surface": "web", "latency_ms": 123},
        "observed_at": now.isoformat(),
        "received_at": now.isoformat(),
    }
    try:
        first = await client.call("telemetry.client_render.record.v2", {"observations": [receipt]})
        replay = await client.call("telemetry.client_render.record.v2", {"observations": [receipt]})
        listed = await client.call(
            "telemetry.client_render.list.v2",
            {"session_id": session_id, "event_id": "evt-1", "limit": 10},
        )

        assert first["inserted"] == 1
        assert replay["duplicates"] == 1
        assert listed["items"] == [
            {
                "session_id": session_id,
                "event_id": "evt-1",
                "surface": "web",
                "provider": None,
                "payload": receipt["payload"],
                "observed_at": now.isoformat(),
                "received_at": now.isoformat(),
            }
        ]
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_client_render_receipt_rejects_noncanonical_session(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC).isoformat()
    try:
        with pytest.raises(CatalogRemoteError) as rejected:
            await client.call(
                "telemetry.client_render.record.v2",
                {
                    "observations": [
                        {
                            "observation_id": "bad",
                            "session_id": "not-a-session",
                            "event_id": "evt-1",
                            "surface": "ios",
                            "payload": {},
                            "observed_at": now,
                            "received_at": now,
                        }
                    ]
                },
            )
        assert rejected.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()
