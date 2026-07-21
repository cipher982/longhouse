"""Catalog RPC + store tests for storage.session.canary.lookup.v2."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.models import SessionTombstone
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveSession


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-canary-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _seed_storage_session(
    connection,
    *,
    session_id: str,
    provider: str,
    last_activity_at: datetime,
    now: datetime,
) -> None:
    connection.execute(
        StorageSession.__table__.insert().values(
            session_id=session_id,
            tenant_id="default",
            owner_id="42",
            provider=provider,
            environment="test",
            machine_id="canary-host",
            project="canary",
            cwd=None,
            git_repo=None,
            git_branch=None,
            started_at=now - timedelta(hours=1),
            last_activity_at=last_activity_at,
            ended_at=None,
            user_messages=1,
            assistant_messages=0,
            tool_calls=0,
            summary_title=None,
            first_user_message_preview="longhouse canary bootstrap",
            last_visible_text_preview=None,
            transcript_revision=1,
            current_render_generation=str(uuid4()),
            raw_state="durable",
            render_state="ready",
            media_state="complete",
            origin_kind="test_or_canary",
            hidden_from_default_timeline=1,
            commit_seq=1,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_live_session(
    connection,
    *,
    session_id: str,
    provider: str,
    state: str,
    last_seen_at: datetime,
    now: datetime,
) -> None:
    connection.execute(
        LiveSession.__table__.insert().values(
            session_id=session_id,
            owner_id="42",
            provider=provider,
            device_id="canary-host",
            state=state,
            started_at=now - timedelta(hours=1),
            last_seen_at=last_seen_at,
            updated_at=last_seen_at,
        )
    )


def test_store_lookup_requires_fresh_live_join_and_prefers_freshest(tmp_path):
    database_path = tmp_path / "catalog.db"
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    fresh_id = str(uuid4())
    stale_id = str(uuid4())
    ended_id = str(uuid4())
    mismatched_provider_id = str(uuid4())
    live_only_id = str(uuid4())
    tombstoned_id = str(uuid4())
    with engine.begin() as connection:
        _seed_storage_session(
            connection,
            session_id=fresh_id,
            provider="canary",
            last_activity_at=now - timedelta(hours=2),
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=fresh_id,
            provider="canary",
            state="observed",
            last_seen_at=now - timedelta(seconds=30),
            now=now,
        )
        _seed_storage_session(
            connection,
            session_id=mismatched_provider_id,
            provider="canary",
            last_activity_at=now,
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=mismatched_provider_id,
            provider="codex",
            state="observed",
            last_seen_at=now,
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=live_only_id,
            provider="canary",
            state="observed",
            last_seen_at=now,
            now=now,
        )
        _seed_storage_session(
            connection,
            session_id=tombstoned_id,
            provider="canary",
            last_activity_at=now,
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=tombstoned_id,
            provider="canary",
            state="observed",
            last_seen_at=now,
            now=now,
        )
        connection.execute(
            SessionTombstone.__table__.insert().values(
                session_id=tombstoned_id,
                deletion_id=str(uuid4()),
                deletion_revision=1,
                deleted_at=now,
                reason="test",
                commit_seq=2,
            )
        )
        _seed_storage_session(
            connection,
            session_id=stale_id,
            provider="canary",
            last_activity_at=now,
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=stale_id,
            provider="canary",
            state="observed",
            last_seen_at=now - timedelta(minutes=10),
            now=now,
        )
        _seed_storage_session(
            connection,
            session_id=ended_id,
            provider="cnary",
            last_activity_at=now,
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=ended_id,
            provider="cnary",
            state="ended",
            last_seen_at=now,
            now=now,
        )
    store = CatalogStore(engine)
    result = store.lookup_storage_canary_session(observed_at=now, max_age_seconds=300)
    assert result["session_id"] == fresh_id
    assert result["provider"] == "canary"
    assert result["runtime_state"] == "observed"
    assert result["hidden_from_default_timeline"] is True
    assert result["max_age_seconds"] == 300
    assert result["commit_seq"] == "0"

    stale_only = store.lookup_storage_canary_session(observed_at=now, max_age_seconds=60)
    assert stale_only["session_id"] == fresh_id

    none_result = store.lookup_storage_canary_session(
        observed_at=now + timedelta(minutes=10),
        max_age_seconds=60,
    )
    assert none_result["session_id"] is None
    engine.dispose()


@pytest.mark.asyncio
async def test_catalog_rpc_canary_lookup_validates_params_and_returns_session(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        _seed_storage_session(
            connection,
            session_id=session_id,
            provider="cnary",
            last_activity_at=now - timedelta(minutes=1),
            now=now,
        )
        _seed_live_session(
            connection,
            session_id=session_id,
            provider="cnary",
            state="observed",
            last_seen_at=now - timedelta(seconds=5),
            now=now,
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as missing:
            await client.call("storage.session.canary.lookup.v2", {"observed_at": now.isoformat()})
        assert missing.value.code == "invalid_request"

        with pytest.raises(CatalogRemoteError) as bad_age:
            await client.call(
                "storage.session.canary.lookup.v2",
                {"observed_at": now.isoformat(), "max_age_seconds": 0},
            )
        assert bad_age.value.code == "invalid_request"

        result = await client.call(
            "storage.session.canary.lookup.v2",
            {"observed_at": now.isoformat(), "max_age_seconds": 300},
        )
        assert result["session_id"] == session_id
        assert result["provider"] == "cnary"
        assert result["commit_seq"] == "0"
        assert result["max_age_seconds"] == 300
    finally:
        await client.close()
        await daemon.close()
