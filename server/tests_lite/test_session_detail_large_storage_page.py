from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from datetime import UTC
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.routers.agents_storage_v2 as storage_router
import zerg.services.storage_v2_workspace as workspace_service
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.main import api_app
from zerg.services.session_workspace import get_legacy_workspace_session_factory
from zerg.storage_v2.render_objects import DecodedRenderObject
from zerg.storage_v2.render_objects import RenderObjectSpec
from zerg.storage_v2.render_objects import RenderRecord


_EVENT_COUNT = 2_500
_PAGE_SIZE = 200
_MACHINE_ID = "large-session-machine"
_PROVIDER = "codex"
_OPAQUE_SOURCE_ID = "history.jsonl"
_SOURCE_EPOCH = UUID("018f0c3a-7b2d-7f10-8a11-123456789abc")
_BASE_TIME_US = 1_720_000_000_000_000


def _object_hash(index: int) -> str:
    return hashlib.sha256(f"render-{index}".encode()).hexdigest()


def _source_envelope_id(index: int) -> str:
    return hashlib.sha256(f"source-{index}".encode()).hexdigest()


def _order_key(index: int) -> str:
    return json.dumps(
        [
            _BASE_TIME_US + index,
            _MACHINE_ID,
            _PROVIDER,
            _OPAQUE_SOURCE_ID,
            str(_SOURCE_EPOCH),
            index,
            0,
        ],
        separators=(",", ":"),
    )


def _seed_large_storage_session(database_path, *, session_id: UUID, generation_id: UUID) -> None:
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            StorageSession.__table__.insert().values(
                session_id=str(session_id),
                tenant_id="tenant",
                owner_id="1",
                provider=_PROVIDER,
                environment="local",
                machine_id=_MACHINE_ID,
                project="large-session-test",
                started_at=now,
                last_activity_at=now,
                user_messages=_EVENT_COUNT,
                assistant_messages=0,
                tool_calls=0,
                transcript_revision=_EVENT_COUNT,
                semantic_projection_version=1,
                current_render_generation=str(generation_id),
                raw_state="durable",
                render_state="ready",
                media_state="complete",
                missing_media_hashes_json="[]",
                user_state="active",
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            RenderGeneration.__table__.insert().values(
                generation_id=str(generation_id),
                session_id=str(session_id),
                parser_revision="test-parser",
                ordering_revision="test-ordering",
                state="current",
                source_chain_hash="a" * 64,
                object_count=_EVENT_COUNT,
                event_count=_EVENT_COUNT,
                first_order_key=_order_key(0),
                last_order_key=_order_key(_EVENT_COUNT - 1),
                commit_seq=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            RenderObject.__table__.insert(),
            [
                {
                    "object_id": _object_hash(index),
                    "generation_id": str(generation_id),
                    "session_id": str(session_id),
                    "source_envelope_id": _source_envelope_id(index),
                    "object_hash": _object_hash(index),
                    "payload_hash": "b" * 64,
                    "object_path": f"render/{index}",
                    "uncompressed_size": 1,
                    "compressed_size": 1,
                    "event_count": 1,
                    "user_messages": 1,
                    "assistant_messages": 0,
                    "tool_calls": 0,
                    "semantic_projection_version": 1,
                    "first_order_key": _order_key(index),
                    "last_order_key": _order_key(index),
                    "first_order_time_us": _BASE_TIME_US + index,
                    "first_machine_id": _MACHINE_ID,
                    "first_provider": _PROVIDER,
                    "first_opaque_source_id": _OPAQUE_SOURCE_ID,
                    "first_source_epoch": str(_SOURCE_EPOCH),
                    "first_source_position": index,
                    "first_event_subordinal": 0,
                    "last_order_time_us": _BASE_TIME_US + index,
                    "last_machine_id": _MACHINE_ID,
                    "last_provider": _PROVIDER,
                    "last_opaque_source_id": _OPAQUE_SOURCE_ID,
                    "last_source_epoch": str(_SOURCE_EPOCH),
                    "last_source_position": index,
                    "last_event_subordinal": 0,
                    "commit_seq": 1,
                    "created_at": now,
                }
                for index in range(_EVENT_COUNT)
            ],
        )
    engine.dispose()


class _RenderPool:
    def __init__(self, *, session_id: UUID, generation_id: UUID) -> None:
        self.session_id = session_id
        self.generation_id = generation_id
        self.read_count = 0

    async def read(self, object_path: str, object_hash: str, *, lane: str) -> DecodedRenderObject:
        assert lane == "user"
        self.read_count += 1
        index = int(object_path.rsplit("/", 1)[-1])
        assert object_hash == _object_hash(index)
        return DecodedRenderObject(
            object_hash=object_hash,
            payload_hash="b" * 64,
            spec=RenderObjectSpec(
                session_id=self.session_id,
                render_generation=self.generation_id,
                parser_revision="test-parser",
                ordering_revision="test-ordering",
                machine_id=_MACHINE_ID,
                provider=_PROVIDER,
                opaque_source_id=_OPAQUE_SOURCE_ID,
                source_epoch=_SOURCE_EPOCH,
                source_envelope_id=_source_envelope_id(index),
                records=(
                    RenderRecord(
                        event_id=f"event-{index}",
                        order_time_us=_BASE_TIME_US + index,
                        source_position=index,
                        event_subordinal=0,
                        role="user",
                        content_text=f"message {index}",
                        interaction_kind="durable_user_message",
                    ),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_large_session_workspace_tail_is_bounded_and_independent_of_catalog_writes(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lhsd-", dir="/tmp")
    root = Path(tempdir.name)
    database_path = root / "catalog.db"
    socket_path = root / "catalogd.sock"
    session_id = uuid4()
    generation_id = uuid4()
    _seed_large_storage_session(database_path, session_id=session_id, generation_id=generation_id)

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    catalog = CatalogClient(socket_path)
    render_pool = _RenderPool(session_id=session_id, generation_id=generation_id)
    mutation_started = threading.Event()
    release_mutation = threading.Event()

    def block_mutations() -> None:
        mutation_started.set()
        release_mutation.wait(timeout=5)

    blocked = asyncio.create_task(daemon._run_store(block_mutations))
    fake_session = SimpleNamespace(
        provider=_PROVIDER,
        origin_kind="imported",
        runtime_display=SimpleNamespace(lifecycle="closed"),
        capabilities=SimpleNamespace(live_control_available=False, can_start_turn=False),
        model_dump=lambda **_kwargs: {"id": str(session_id), "provider": _PROVIDER},
    )
    monkeypatch.setattr(workspace_service, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: render_pool)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: None)
    monkeypatch.setattr(
        workspace_service,
        "read_live_catalog_session",
        lambda requested_session_id, *, owner_id: (fake_session, None, "1")
        if requested_session_id == session_id and owner_id == 1
        else (None, None, "1"),
    )
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1)
    api_app.dependency_overrides[get_legacy_workspace_session_factory] = lambda: None

    try:
        assert await asyncio.to_thread(mutation_started.wait, 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
            response = await client.get(f"/timeline/sessions/{session_id}/workspace", params={"limit": _PAGE_SIZE})

        assert response.status_code == 200, response.text
        payload = response.json()
        events = [item["event"] for item in payload["projection"]["items"]]
        assert [event["id"] for event in events] == [
            f"event-{index}" for index in range(_EVENT_COUNT - _PAGE_SIZE, _EVENT_COUNT)
        ]
        assert payload["projection"]["has_more"] is True
        assert render_pool.read_count <= _PAGE_SIZE + 2
        assert not blocked.done()
    finally:
        api_app.dependency_overrides.pop(get_current_browser_user, None)
        api_app.dependency_overrides.pop(get_legacy_workspace_session_factory, None)
        release_mutation.set()
        await blocked
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()
