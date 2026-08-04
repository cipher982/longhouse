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
from zerg.catalogd.schema import read_catalog_meta
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveAPNSDeviceRegistration
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveNotificationEvent
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLivePreview
from zerg.models.live_store import LiveUser


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-runtime-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _event(*, session_id: str, runtime_key: str, dedupe_key: str, occurred_at: datetime) -> dict:
    return {
        "runtime_key": runtime_key,
        "session_id": session_id,
        "thread_id": None,
        "run_id": None,
        "provider": "codex",
        "device_id": "cinder",
        "source": "codex_bridge",
        "kind": "phase_signal",
        "phase": "running",
        "tool_name": "Shell",
        "occurred_at": occurred_at.isoformat(),
        "freshness_ms": 60_000,
        "dedupe_key": dedupe_key,
        "payload": {},
    }


@pytest.mark.asyncio
async def test_runtime_apply_owns_state_resume_preview_and_commit_sequence(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    preview_session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="dev",
                started_at=now,
                user_state="snoozed",
            )
        )
    engine.dispose()


    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    event = _event(
        session_id=session_id,
        runtime_key="codex:catalog-runtime",
        dedupe_key="catalog-runtime-1",
        occurred_at=now,
    )
    try:
        first = await client.call("session.runtime.apply.v2", {"events": [event]})
        replay = await client.call("session.runtime.apply.v2", {"events": [event]})
        assert first == {
            "accepted": 1,
            "duplicates": 0,
            "updated_runtime_keys": ["codex:catalog-runtime"],
            "commit_seq": "1",
        }
        assert replay["accepted"] == 1
        assert replay["updated_runtime_keys"] == []
        assert replay["commit_seq"] == "2"

        preview_event = {
            **_event(
                session_id=preview_session_id,
                runtime_key="codex:catalog-preview",
                dedupe_key="catalog-preview-1",
                occurred_at=now,
            ),
            "source": "codex_bridge_live",
            "kind": "progress_signal",
            "phase": None,
            "tool_name": None,
            "payload": {
                "progress_kind": "bridge_live_transcript_delta",
                "live_text": "streaming output",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "seq": 4,
            },
        }
        preview = await client.call("session.runtime.apply.v2", {"events": [preview_event]})
        assert preview["updated_runtime_keys"] == ["codex:catalog-preview"]
        assert preview["commit_seq"] == "3"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        state = connection.execute(
            LiveRuntimeState.__table__.select().where(LiveRuntimeState.runtime_key == "codex:catalog-runtime")
        ).mappings().one()
        assert state["phase"] == "running"
        assert state["active_tool"] == "Shell"
        catalog = connection.execute(
            LiveSessionCatalog.__table__.select().where(LiveSessionCatalog.session_id == session_id)
        ).mappings().one()
        assert catalog["user_state"] == "active"
        assert connection.execute(LiveArchiveOutbox.__table__.select()).first() is None
        live_preview = connection.execute(
            LiveSessionLivePreview.__table__.select().where(LiveSessionLivePreview.session_id == preview_session_id)
        ).mappings().one()
        assert live_preview["preview_text"] == "streaming output"
        assert read_catalog_meta(engine).commit_seq == 3
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_apply_rejects_invalid_batch_without_catalog_commit(daemon_paths):
    database_path, socket_path = daemon_paths
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.runtime.apply.v2", {"events": []})
        assert exc_info.value.code == "invalid_request"
        with pytest.raises(CatalogRemoteError) as exc_info:
            await client.call("session.runtime.apply.v2", {"events": [{"runtime_key": "missing-fields"}]})
        assert exc_info.value.code == "invalid_request"
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    assert read_catalog_meta(engine).commit_seq == 0
    with engine.connect() as connection:
        assert connection.execute(LiveRuntimeState.__table__.select()).first() is None
        assert connection.execute(LiveArchiveOutbox.__table__.select()).first() is None
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_apply_prepares_catalog_stall_attention_and_rollback(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveUser.__table__.insert().values(id=7, email="catalog@example.com", prefs={})
        )
        connection.execute(
            LiveSession.__table__.insert().values(
                session_id=session_id,
                owner_id="7",
                provider="codex",
                started_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="dev",
                project="zerg",
                summary_title="Catalog stalled session",
                started_at=now,
            )
        )
        connection.execute(
            LiveAPNSDeviceRegistration.__table__.insert().values(
                id=str(uuid4()),
                owner_id=7,
                platform="ios",
                device_token="c" * 64,
                push_environment="sandbox",
                created_at=now,
                updated_at=now,
                last_seen_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        stalled_event = {
            **_event(
                session_id=session_id,
                runtime_key=f"codex:{session_id}",
                dedupe_key="catalog-stalled-1",
                occurred_at=now,
            ),
            "phase": "stalled",
            "tool_name": "Shell",
            "payload": {"stall_notification": True},
        }
        stalled = await client.call("session.runtime.apply.v2", {"events": [stalled_event]})
        assert stalled["attention_actions"][0]["kind"] == "attention"
        assert stalled["attention_actions"][0]["state"] == "stalled"
        notification_event_id = stalled["attention_actions"][0]["notification_event_id"]
        retry_event = {
            **stalled_event,
            "occurred_at": (now + timedelta(seconds=30)).isoformat(),
            "dedupe_key": "catalog-stalled-retry-1",
        }
        retry = await client.call("session.runtime.apply.v2", {"events": [retry_event]})
        assert retry["attention_actions"][0]["notification_event_id"] == notification_event_id

        resolved_event = {
            **_event(
                session_id=session_id,
                runtime_key=f"codex:{session_id}",
                dedupe_key="catalog-stalled-resolved-1",
                occurred_at=now + timedelta(seconds=60),
            ),
            "phase": "idle",
            "tool_name": None,
        }
        resolved = await client.call("session.runtime.apply.v2", {"events": [resolved_event]})
        assert resolved.get("attention_actions"), resolved
        assert resolved["attention_actions"][0]["kind"] == "resolution"
        assert resolved["attention_actions"][0]["previous_state"] == "stalled:pending"
        resolution_event_id = resolved["attention_actions"][0]["notification_event_id"]

        rolled_back = await client.call(
            "notification.apns.attention.rollback.v2",
            {
                "session_id": session_id,
                "action": "resolution",
                "state": "stalled",
                "previous_state": "stalled:pending",
                "notification_event_id": resolution_event_id,
                "occurred_at": (now + timedelta(minutes=1)).isoformat(),
                "attention_push_at": now.isoformat(),
            },
        )
        assert rolled_back["rolled_back"] is True

        committed = await client.call(
            "notification.apns.attention.commit.v2",
            {
                "session_id": session_id,
                "action": "attention",
                "state": "stalled",
                "previous_state": "",
                "notification_event_id": notification_event_id,
                "occurred_at": now.isoformat(),
                "attention_push_at": now.isoformat(),
            },
        )
        assert committed["committed"] is True

        resolved_again_event = {
            **resolved_event,
            "dedupe_key": "catalog-stalled-resolved-2",
            "occurred_at": (now + timedelta(seconds=120)).isoformat(),
        }
        resolved_again = await client.call("session.runtime.apply.v2", {"events": [resolved_again_event]})
        assert resolved_again["attention_actions"][0]["previous_state"] == "stalled"
        resolution_event_id = resolved_again["attention_actions"][0]["notification_event_id"]
        rolled_back_again = await client.call(
            "notification.apns.attention.rollback.v2",
            {
                "session_id": session_id,
                "action": "resolution",
                "state": "stalled",
                "previous_state": "stalled",
                "notification_event_id": resolution_event_id,
                "occurred_at": (now + timedelta(minutes=2)).isoformat(),
                "attention_push_at": now.isoformat(),
            },
        )
        assert rolled_back_again["rolled_back"] is True
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        catalog = connection.execute(
            LiveSessionCatalog.__table__.select().where(LiveSessionCatalog.session_id == session_id)
        ).mappings().one()
        assert catalog["last_attention_push_state"] == "stalled"
        assert catalog["last_attention_notification_id"] == notification_event_id
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_apply_audits_catalog_attention_suppression_without_targets(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(LiveUser.__table__.insert().values(id=8, email="audit@example.com", prefs={}))
        connection.execute(
            LiveSession.__table__.insert().values(
                session_id=session_id,
                owner_id="8",
                provider="codex",
                started_at=now,
                last_seen_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="codex",
                environment="dev",
                started_at=now,
            )
        )
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        event = {
            **_event(
                session_id=session_id,
                runtime_key=f"codex:{session_id}",
                dedupe_key="catalog-stalled-no-targets-1",
                occurred_at=now,
            ),
            "phase": "stalled",
            "payload": {"stall_notification": True},
        }
        result = await client.call("session.runtime.apply.v2", {"events": [event]})
        assert "attention_actions" not in result
    finally:
        await client.close()
        await daemon.close()

    engine = create_catalog_engine(database_path)
    with engine.connect() as connection:
        audit = connection.execute(LiveNotificationEvent.__table__.select()).mappings().one()
        assert audit["channel_results"] == {"suppressed": "no_ios_targets"}
    engine.dispose()
