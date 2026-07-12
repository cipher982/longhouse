from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveUser


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhci-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _local_params(**overrides):
    params = {
        "email": "owner@example.com",
        "provider": "password",
        "provider_user_id": None,
        "role": "USER",
        "adopt_existing": True,
        "require_email_match": False,
        "max_users": None,
        "promote_role": False,
    }
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_local_user_resolution_is_atomic_bounded_and_idempotent(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("auth.user.resolve_local.v2", _local_params())
        replay = await client.call("auth.user.resolve_local.v2", _local_params())
        promoted = await client.call(
            "auth.user.resolve_local.v2",
            _local_params(role="ADMIN", promote_role=True),
        )
        fetched = await client.call(
            "auth.user.get.v2",
            {"user_id": created["user"]["id"], "touch_last_login": True},
        )
        fetched_again = await client.call(
            "auth.user.get.v2",
            {"user_id": created["user"]["id"], "touch_last_login": True},
        )

        assert (created["created"], created["changed"], created["commit_seq"]) == (True, True, "1")
        assert (replay["created"], replay["changed"], replay["commit_seq"]) == (False, False, "1")
        assert promoted["user"]["role"] == "ADMIN"
        assert promoted["commit_seq"] == "2"
        assert fetched["changed"] is True
        assert fetched["commit_seq"] == "3"
        assert fetched_again["changed"] is False
        assert fetched_again["commit_seq"] == "3"

        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.user.resolve_local.v2",
                _local_params(
                    email="different@example.com",
                    adopt_existing=False,
                    require_email_match=True,
                ),
            )
        assert exc_info.value.code == "conflict"
        assert exc_info.value.details == {"reason": "owner_email_mismatch"}
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_cp_resolution_preserves_link_conflicts_and_email_collision_rule(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        local = await client.call("auth.user.resolve_local.v2", _local_params())
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.user.resolve_cp.v2",
                {
                    "cp_user_id": 41,
                    "email": "owner@example.com",
                    "email_verified": False,
                    "display_name": None,
                    "avatar_url": None,
                },
            )
        assert exc_info.value.details == {"reason": "email_unverified_link"}

        linked = await client.call(
            "auth.user.resolve_cp.v2",
            {
                "cp_user_id": 41,
                "email": "owner@example.com",
                "email_verified": True,
                "display_name": "Owner",
                "avatar_url": None,
            },
        )
        assert linked["user"]["id"] == local["user"]["id"]
        assert linked["user"]["provider_user_id"] == "cp:41"
        assert linked["commit_seq"] == "2"

        other = await client.call(
            "auth.user.resolve_local.v2",
            _local_params(email="other@example.com", adopt_existing=False),
        )
        updated = await client.call(
            "auth.user.resolve_cp.v2",
            {
                "cp_user_id": 41,
                "email": "other@example.com",
                "email_verified": True,
                "display_name": "Renamed",
                "avatar_url": "https://example.com/a.png",
            },
        )
        assert other["user"]["email"] == "other@example.com"
        assert updated["user"]["email"] == "owner@example.com"
        assert updated["user"]["display_name"] == "Renamed"
        assert updated["commit_seq"] == "4"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_device_resolution_joins_active_owner_and_debounces_touch(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert().values(
                id=7,
                email="owner@example.com",
                is_active=True,
                email_verified=True,
                role="USER",
                prefs={},
                context={},
                created_at=now,
            )
        )
        connection.execute(
            LiveDeviceToken.__table__.insert().values(
                id="device-token",
                owner_id=7,
                device_id="cinder",
                token_hash="a" * 64,
                created_at=now,
            )
        )
    engine.dispose()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        params = {"token_hash": "a" * 64, "touch_last_used": True, "touch_interval_seconds": 60}
        first = await client.call("auth.device.resolve.v2", params)
        replay = await client.call("auth.device.resolve.v2", params)
        assert first["valid"] is True and first["changed"] is True
        assert first["user"]["id"] == 7
        assert first["commit_seq"] == "1"
        assert replay["changed"] is False and replay["commit_seq"] == "1"

        engine = create_catalog_engine(database_path)
        with engine.begin() as connection:
            connection.execute(LiveUser.__table__.update().where(LiveUser.id == 7).values(is_active=False))
        engine.dispose()
        invalid = await client.call("auth.device.resolve.v2", params)
        assert invalid == {"valid": False, "changed": False, "commit_seq": "1"}
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_refresh_rotation_exact_replay_inactive_revoke_and_restart_durability(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    now = datetime.now(UTC)
    try:
        user = (await client.call("auth.user.resolve_local.v2", _local_params()))["user"]
        create_params = {
            "user_id": user["id"],
            "token_hash": "a" * 64,
            "family_id": "family-1",
            "parent_id": None,
            "created_at": now.isoformat(),
            "absolute_expires_at": (now + timedelta(days=90)).isoformat(),
            "idle_expires_at": (now + timedelta(days=30)).isoformat(),
        }
        created = await client.call("auth.refresh.create.v2", create_params)
        replay_create = await client.call("auth.refresh.create.v2", create_params)
        rotate_params = {
            "token_hash": "a" * 64,
            "next_token_hash": "b" * 64,
            "now": (now + timedelta(seconds=1)).isoformat(),
            "idle_expires_at": (now + timedelta(days=30, seconds=1)).isoformat(),
            "reuse_grace_seconds": 10,
        }
        rotated = await client.call("auth.refresh.rotate.v2", rotate_params)
        exact = await client.call("auth.refresh.rotate.v2", rotate_params)
        assert created["commit_seq"] == "2"
        assert replay_create["exact_replay"] is True and replay_create["commit_seq"] == "2"
        assert rotated["status"] == "rotated" and rotated["user"]["id"] == user["id"]
        assert rotated["commit_seq"] == "3"
        assert exact["status"] == "exact_replay" and exact["commit_seq"] == "3"
    finally:
        await client.close()
        await daemon.close()

    # A restart must preserve both the child replay and the global commit sequence.
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        exact = await client.call("auth.refresh.rotate.v2", rotate_params)
        assert exact["status"] == "exact_replay" and exact["commit_seq"] == "3"
        engine = create_catalog_engine(database_path)
        with engine.begin() as connection:
            connection.execute(LiveUser.__table__.update().where(LiveUser.id == user["id"]).values(is_active=False))
        engine.dispose()
        revoked = await client.call(
            "auth.refresh.rotate.v2",
            {
                **rotate_params,
                "token_hash": "b" * 64,
                "next_token_hash": "c" * 64,
                "now": (now + timedelta(seconds=2)).isoformat(),
            },
        )
        assert revoked["status"] == "family_revoked"
        assert revoked["revoked_count"] == 2
        assert revoked["commit_seq"] == "4"
    finally:
        await client.close()
        await daemon.close()


@pytest.mark.asyncio
async def test_profile_update_mask_distinguishes_omitted_and_null(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        user = (await client.call("auth.user.resolve_local.v2", _local_params()))["user"]
        updated = await client.call(
            "auth.user.update.v2",
            {
                "user_id": user["id"],
                "display_name": "David",
                "avatar_url": None,
                "prefs": {"theme": "dark"},
                "update_mask": ["display_name", "prefs"],
            },
        )
        replay = await client.call(
            "auth.user.update.v2",
            {
                "user_id": user["id"],
                "display_name": "David",
                "avatar_url": "ignored",
                "prefs": {"theme": "dark"},
                "update_mask": ["display_name", "prefs"],
            },
        )
        assert updated["changed"] is True and updated["commit_seq"] == "2"
        assert updated["user"]["avatar_url"] is None
        assert replay["changed"] is False and replay["commit_seq"] == "2"

        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call(
                "auth.user.update.v2",
                {
                    "user_id": user["id"],
                    "display_name": None,
                    "avatar_url": None,
                    "prefs": None,
                    "update_mask": ["prefs", "prefs"],
                },
            )
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()
