from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveUser


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-presence-apns-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_machine_presence_and_apns_registration(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert().values(
                id=7,
                email="owner@example.com",
                role="USER",
                is_active=True,
                prefs={"machine_presence_enabled": False},
            )
        )
    engine.dispose()

    now = datetime.now(UTC).replace(microsecond=0)
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        policy = await client.call("machine.presence.policy.v2", {"owner_id": 7})
        assert policy["enabled"] is False

        presence = await client.call(
            "machine.presence.upsert.v2",
            {
                "owner_id": 7,
                "device_id": "cinder",
                "state": "idle_5m",
                "source": "macos_hid_idle",
                "idle_seconds": 300,
                "measured_at": now.isoformat(),
                "received_at": now.isoformat(),
            },
        )
        assert presence["presence"]["device_id"] == "cinder"
        assert presence["presence"]["idle_seconds"] == 300

        first_id = str(uuid4())
        first = await client.call(
            "notification.apns.device.upsert.v2",
            {
                "registration_id": first_id,
                "owner_id": 7,
                "platform": "ios",
                "device_token": "a" * 64,
                "push_environment": "sandbox",
                "app_build_id": "build-1",
                "observed_at": now.isoformat(),
            },
        )
        replay = await client.call(
            "notification.apns.device.upsert.v2",
            {
                "registration_id": str(uuid4()),
                "owner_id": 7,
                "platform": "ios",
                "device_token": "a" * 64,
                "push_environment": "production",
                "app_build_id": "build-2",
                "observed_at": now.isoformat(),
            },
        )
        assert first["registration"]["id"] == first_id
        assert replay["registration"]["id"] == first_id
        assert replay["registration"]["push_environment"] == "production"

        activity_id = "activity-1"
        activity = await client.call(
            "notification.apns.live_activity.upsert.v2",
            {
                "registration_id": str(uuid4()),
                "owner_id": 7,
                "session_id": str(uuid4()),
                "activity_id": activity_id,
                "push_token": "b" * 64,
                "push_environment": "sandbox",
                "app_build_id": None,
                "observed_at": now.isoformat(),
            },
        )
        assert activity["registration"]["activity_id"] == activity_id
        ended = await client.call(
            "notification.apns.live_activity.end.v2",
            {"owner_id": 7, "activity_id": activity_id, "ended_at": now.isoformat()},
        )
        assert ended["found"] is True
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_catalogd_bootstraps_single_tenant_owner_idempotently(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    params = {
        "email": "owner@example.com",
        "provider": "google",
        "provider_user_id": None,
    }
    try:
        created = await client.call("auth.single_tenant.ensure.v2", params)
        replay = await client.call("auth.single_tenant.ensure.v2", params)
        assert created["created"] is True
        assert created["user"]["role"] == "ADMIN"
        assert replay["created"] is False
        assert replay["user"]["id"] == created["user"]["id"]

        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.single_tenant.ensure.v2",
                {**params, "email": "other@example.com"},
            )
        assert exc_info.value.code == "conflict"
        assert exc_info.value.details == {"reason": "owner_email_mismatch"}
    finally:
        await client.close()
        await daemon.close()
