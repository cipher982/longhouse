from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.schema import read_catalog_meta
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLivePreview


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
async def test_runtime_apply_owns_state_outbox_resume_preview_and_commit_sequence(daemon_paths):
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
        outbox_rows = connection.execute(LiveArchiveOutbox.__table__.select()).mappings().all()
        assert len(outbox_rows) == 1
        assert json.loads(outbox_rows[0]["payload_json"])["event"]["dedupe_key"] == "catalog-runtime-1"
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
