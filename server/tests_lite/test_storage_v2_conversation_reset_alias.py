"""Conversation-reset ingest makes the rotated native id resolvable.

Raw ``claude --resume`` outside Longhouse rotates the provider-native session
id inside the same transcript. The engine ships a ``conversation_reset``
boundary record carrying both native ids in ``tool_input_json``; storage-v2
ingest must upsert the NEW id as a ``provider_session_id`` thread alias so it
resolves to the session via the group-A path (``session.alias.resolve.v2``).
Before this, both ids were only hashed into the record's event_id — the fork
was visible in the transcript but unrecoverable as identity.

Drives the real ingest path: a real CatalogDaemon, real raw-object workers,
and a real envelope POST — not a stub of the code under test. The resolve
assertion fails if the alias write in ``CatalogStore.commit_raw_object`` is
removed.
"""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.routers.agents_storage_v2 as storage_router
import zerg.services.storage_session_titles as storage_titles
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


class _InlineRenderPool:
    def __init__(self, root):
        self.root = root

    @asynccontextmanager
    async def admission(self, _lane):
        yield

    async def seal(self, spec, *, lane):
        assert lane in {"live", "repair"}
        return seal_render_object(self.root, spec)

    async def read(self, object_path, expected_object_hash, *, lane):
        assert lane == "user"
        return read_render_object(self.root, object_path, expected_object_hash=expected_object_hash)


def _render_record(**overrides) -> dict:
    record = {
        "event_id": "user-1",
        "order_time_us": 1_722_500_000_000_000,
        "source_position": 0,
        "event_subordinal": 0,
        "role": "user",
        "content_text": "hello",
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "tool_call_id": None,
        "thread_id": None,
        "branch_kind": None,
        "raw_record_ordinal": 0,
    }
    record.update(overrides)
    return record


def _reset_envelope(
    *,
    tenant_id: str,
    machine_id: str,
    session_id: str,
    previous_native_id: str,
    new_native_id: str,
    reset_payload: object | None = "default",
) -> dict:
    """A rotation-shaped envelope: reset boundary first, then the new records.

    Mirrors what the engine ships after detecting a native-id rotation
    (storage_v2_shipper.rs insert_conversation_reset_boundary): a system record
    with branch_kind conversation_reset ordered before the resumed source
    records at the same source_position.
    """

    data = b'{"role":"user"}\n'
    epoch = uuid4()
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider="claude",
        opaque_source_id=f"{new_native_id}.jsonl",
        source_epoch=epoch,
        range_kind="byte_offset",
        range_start=0,
        range_end=len(data),
        record_hashes=hash_records((data,)),
    )
    if reset_payload == "default":
        reset_payload = {
            "previous_provider_session_id": previous_native_id,
            "provider_session_id": new_native_id,
        }
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": machine_id,
        "session_id": session_id,
        "provider": "claude",
        "opaque_source_id": f"{new_native_id}.jsonl",
        "source_epoch": str(epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": "2026-08-01T12:00:00+00:00",
        "range_kind": "byte_offset",
        "range_start": 0,
        "range_end": len(data),
        "render": {
            "generation_id": str(uuid4()),
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                _render_record(
                    event_id=str(uuid4()),
                    order_time_us=1_722_499_999_999_999,
                    role="system",
                    content_text="Conversation reset",
                    branch_kind="conversation_reset",
                    tool_input_json=reset_payload,
                ),
                _render_record(event_subordinal=1),
            ],
        },
        "media": [],
        "session": {
            "environment": "local",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": "2026-08-01T11:00:00+00:00",
            "last_activity_at": "2026-08-01T12:00:00+00:00",
            "ended_at": None,
            "origin_kind": "helm",
            "hidden_from_default_timeline": False,
            "launch_actor": "user",
            "launch_surface": "terminal",
            "provider_session_id": new_native_id,
        },
        "records": [{"source_position": 0, "data_b64": base64.b64encode(data).decode("ascii")}],
        "expected_envelope_id": envelope_id(identity),
    }


def _seed_helm_session(database_path: Path, *, session_id: str, previous_native_id: str) -> None:
    """A Helm-shaped live-catalog session whose native id predates the rotation."""

    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    now = datetime.now(UTC).replace(microsecond=0)
    thread_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            LiveSessionCatalog.__table__.insert().values(
                session_id=session_id,
                provider="claude",
                environment="local",
                project="longhouse",
                device_id="cinder",
                device_name="Cinder",
                cwd="/workspace/longhouse",
                started_at=now - timedelta(hours=1),
                last_activity_at=now,
                primary_thread_id=thread_id,
            )
        )
        connection.execute(
            LiveSessionThread.__table__.insert().values(
                id=thread_id,
                session_id=session_id,
                provider="claude",
                is_primary=1,
                created_at=now - timedelta(hours=1),
                updated_at=now,
            )
        )
        connection.execute(
            LiveSessionThreadAlias.__table__.insert().values(
                thread_id=thread_id,
                provider="claude",
                alias_kind="provider_session_id",
                alias_value=previous_native_id,
                first_seen_at=now - timedelta(hours=1),
                last_seen_at=now - timedelta(hours=1),
            )
        )
    engine.dispose()


async def _ingest(client_app: FastAPI, payload: dict) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client_app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            json=payload,
            headers={"X-Longhouse-Storage-Lane": "live"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["raw_state"] == "durable"


@pytest.mark.asyncio
async def test_conversation_reset_ingest_makes_the_new_native_id_resolve_to_the_session(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lh2-reset-", dir="/tmp")
    root = Path(tempdir.name)
    session_id = str(uuid4())
    previous_native_id = str(uuid4())
    new_native_id = str(uuid4())
    _seed_helm_session(root / "catalog.db", session_id=session_id, previous_native_id=previous_native_id)

    daemon = CatalogDaemon(database_path=root / "catalog.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    catalog = CatalogClient(root / "catalogd.sock")
    workers = RawObjectWorkerPool(root / "objects", live_workers=1, repair_workers=1, queue_multiplier=1)
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: _InlineRenderPool(root / "objects"))
    # The commit fires a background AI-title task against the global (unpatched)
    # catalogd supervisor; silence it so an unawaited task cannot flake the test.
    monkeypatch.setattr(storage_titles, "schedule_storage_session_title", lambda candidate: None)

    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None

    try:
        # Negative control: the rotated id resolves nowhere before ingest, so
        # the positive assertion below can only pass via the ingest alias write.
        before = await catalog.call("session.alias.resolve.v2", {"provider_session_id": new_native_id})
        assert before["found"] is False

        payload = _reset_envelope(
            tenant_id=get_settings().archive_primary_tenant_id,
            machine_id="cinder",
            session_id=session_id,
            previous_native_id=previous_native_id,
            new_native_id=new_native_id,
        )
        # Isolate the reset-capture path: session facts may also carry the new
        # id as the current provider head (covered by the existing facts-alias
        # tests), and leaving it in would let that path mask a regression in
        # the conversation_reset capture this test exists to pin.
        payload["session"].pop("provider_session_id")
        await _ingest(app, payload)

        after = await catalog.call("session.alias.resolve.v2", {"provider_session_id": new_native_id})
        assert after["found"] is True, "conversation_reset ingest must alias the new native id"
        assert after["session_id"] == session_id
        # The pre-rotation id keeps resolving too — rotation adds identity, it
        # never destroys it.
        old = await catalog.call("session.alias.resolve.v2", {"provider_session_id": previous_native_id})
        assert old["found"] is True
        assert old["session_id"] == session_id
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()


@pytest.mark.asyncio
async def test_reset_record_without_structured_ids_is_ignored_not_fatal(monkeypatch):
    """Old-engine reset records (tool_input_json null) must still ingest cleanly."""

    tempdir = TemporaryDirectory(prefix="lh2-reset-null-", dir="/tmp")
    root = Path(tempdir.name)
    session_id = str(uuid4())
    previous_native_id = str(uuid4())
    new_native_id = str(uuid4())
    _seed_helm_session(root / "catalog.db", session_id=session_id, previous_native_id=previous_native_id)

    daemon = CatalogDaemon(database_path=root / "catalog.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    catalog = CatalogClient(root / "catalogd.sock")
    workers = RawObjectWorkerPool(root / "objects", live_workers=1, repair_workers=1, queue_multiplier=1)
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: _InlineRenderPool(root / "objects"))
    # The commit fires a background AI-title task against the global (unpatched)
    # catalogd supervisor; silence it so an unawaited task cannot flake the test.
    monkeypatch.setattr(storage_titles, "schedule_storage_session_title", lambda candidate: None)

    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None

    try:
        payload = _reset_envelope(
            tenant_id=get_settings().archive_primary_tenant_id,
            machine_id="cinder",
            session_id=session_id,
            previous_native_id=previous_native_id,
            new_native_id=new_native_id,
            reset_payload=None,
        )
        # Without structured ids the reset contributes no alias, and the facts
        # head is removed too, so nothing may invent one.
        payload["session"].pop("provider_session_id")
        await _ingest(app, payload)
        resolved = await catalog.call("session.alias.resolve.v2", {"provider_session_id": new_native_id})
        assert resolved["found"] is False
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()


@pytest.mark.asyncio
async def test_rotation_claimed_by_another_session_survives_the_routing_index(monkeypatch):
    """Cross-branch seam: the alias write must not trip the unique routing index.

    The routing index (schema v4) makes (provider, alias_value) unique across
    threads for provider_session_id. A rotation claiming a native id already
    held by another session's thread must resolve existing-thread-wins with a
    warning — an ON CONFLICT keyed on the per-thread constraint would raise
    IntegrityError and fail the whole storage commit.
    """

    tempdir = TemporaryDirectory(prefix="lh2-reset-collide-", dir="/tmp")
    root = Path(tempdir.name)
    session_id = str(uuid4())
    other_session_id = str(uuid4())
    previous_native_id = str(uuid4())
    stolen_native_id = str(uuid4())
    _seed_helm_session(root / "catalog.db", session_id=session_id, previous_native_id=previous_native_id)
    # The colliding id already routes to another session's thread.
    _seed_helm_session(root / "catalog.db", session_id=other_session_id, previous_native_id=stolen_native_id)

    daemon = CatalogDaemon(database_path=root / "catalog.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    catalog = CatalogClient(root / "catalogd.sock")
    workers = RawObjectWorkerPool(root / "objects", live_workers=1, repair_workers=1, queue_multiplier=1)
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: _InlineRenderPool(root / "objects"))
    monkeypatch.setattr(storage_titles, "schedule_storage_session_title", lambda candidate: None)

    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None

    try:
        payload = _reset_envelope(
            tenant_id=get_settings().archive_primary_tenant_id,
            machine_id="cinder",
            session_id=session_id,
            previous_native_id=previous_native_id,
            new_native_id=stolen_native_id,
        )
        payload["session"].pop("provider_session_id")
        # The commit itself must succeed — the collision may not fail the batch.
        await _ingest(app, payload)

        # Existing thread wins: the stolen id keeps routing to its first owner.
        resolved = await catalog.call("session.alias.resolve.v2", {"provider_session_id": stolen_native_id})
        assert resolved["found"] is True
        assert resolved["session_id"] == other_session_id
        # The ingesting session's own prior identity is untouched.
        own = await catalog.call("session.alias.resolve.v2", {"provider_session_id": previous_native_id})
        assert own["found"] is True
        assert own["session_id"] == session_id
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()


def test_router_extracts_rotation_ids_from_reset_records():
    """The extraction feeding the commit RPC reads exactly the engine's payload."""

    tenant = "tenant"
    payload = _reset_envelope(
        tenant_id=tenant,
        machine_id="cinder",
        session_id=str(uuid4()),
        previous_native_id="native-old",
        new_native_id="native-new",
    )
    _spec, parsed = storage_router._parse_envelope(payload, tenant_id=tenant, machine_id="cinder", lane="live")
    assert storage_router._conversation_resets(parsed["render_spec"]) == [
        {"previous_provider_session_id": "native-old", "provider_session_id": "native-new"}
    ]
    # Malformed or id-less payloads are dropped, never fatal.
    degraded = _reset_envelope(
        tenant_id=tenant,
        machine_id="cinder",
        session_id=str(uuid4()),
        previous_native_id="native-old",
        new_native_id="native-new",
        reset_payload={"provider_session_id": "  "},
    )
    _spec, parsed = storage_router._parse_envelope(degraded, tenant_id=tenant, machine_id="cinder", lane="live")
    assert storage_router._conversation_resets(parsed["render_spec"]) == []
    assert storage_router._conversation_resets(None) == []
