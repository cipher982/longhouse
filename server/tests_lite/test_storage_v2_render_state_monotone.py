"""A commit that carries no render cannot demote a session that already has one.

``sessions.render_state`` answers one question: does this session publish a
render? Cursor ships two sources for one conversation. ``store.db`` carries a
render; the ``agent-transcripts`` JSONL projection deliberately ships raw bytes
only, because a lossy render must never take authority from the store. Every
such raw-only commit used to write ``pending`` over the session, so a rendered
session read as "archive pending" and "lagging" for good: nothing ever
promotes a raw-only source, and the write-once flag had no way back.

Drives the real ingest path -- a real CatalogDaemon, real raw-object workers
and real envelope POSTs -- and reads the served facts, not just the column.
"""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

import zerg.routers.agents_storage_v2 as storage_router
import zerg.services.storage_session_titles as storage_titles
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.server import CatalogDaemon
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object

STORE_SOURCE = "cursor-store-v1:6f1c2a52-71d2-4b62-9c9f-2d0e4a7c1a11"
JSONL_SOURCE = "path-sha256:" + "ab" * 32


class _InlineRenderPool:
    def __init__(self, root):
        self.root = root

    @asynccontextmanager
    async def admission(self, _lane):
        yield

    async def seal(self, spec, *, lane):
        return seal_render_object(self.root, spec)

    async def read(self, object_path, expected_object_hash, *, lane):
        return read_render_object(self.root, object_path, expected_object_hash=expected_object_hash)


def _render_records() -> list[dict]:
    base = {
        "order_time_us": 1_790_000_000_000_000,
        "event_subordinal": 0,
        "tool_name": None,
        "tool_input_json": None,
        "tool_output_text": None,
        "tool_call_id": None,
        "thread_id": None,
        "branch_kind": None,
        "raw_record_ordinal": 0,
    }
    return [
        {**base, "event_id": "user-1", "source_position": 0, "role": "user", "content_text": "hello"},
        {**base, "event_id": "asst-1", "source_position": 0, "event_subordinal": 1, "role": "assistant", "content_text": "hi"},
    ]


def _envelope(
    *,
    session_id: str,
    machine_id: str,
    opaque_source_id: str,
    source_epoch: str | None = None,
    predecessor: str | None = None,
    opened_at: str = "2026-09-01T12:00:00+00:00",
    with_render: bool,
) -> dict:
    tenant_id = get_settings().archive_primary_tenant_id
    data = b'{"role":"user"}\n'
    epoch = source_epoch or str(uuid4())
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider="cursor",
        opaque_source_id=opaque_source_id,
        source_epoch=UUID(epoch),
        range_kind="byte_offset",
        range_start=0,
        range_end=len(data),
        record_hashes=hash_records((data,)),
    )
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": machine_id,
        "session_id": session_id,
        "provider": "cursor",
        "opaque_source_id": opaque_source_id,
        "source_epoch": epoch,
        "predecessor_source_epoch": predecessor,
        "epoch_opened_at": opened_at,
        "range_kind": "byte_offset",
        "range_start": 0,
        "range_end": len(data),
        "render": (
            {
                "generation_id": str(uuid4()),
                "parser_revision": "cursor-store-render-test",
                "ordering_revision": "cursor-root-order-v1",
                "records": _render_records(),
            }
            if with_render
            else None
        ),
        "media": [],
        "session": {
            "environment": "local",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": "2026-09-01T11:00:00+00:00",
            "last_activity_at": "2026-09-01T12:00:00+00:00",
            "ended_at": None,
            "origin_kind": None,
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
            "provider_session_id": None,
        },
        "records": [{"source_position": 0, "data_b64": base64.b64encode(data).decode("ascii")}],
        "expected_envelope_id": envelope_id(identity),
    }


@asynccontextmanager
async def _stack(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lh2-render-state-", dir="/tmp")
    root = Path(tempdir.name)
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
        yield app, catalog, root
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()


async def _ingest(app: FastAPI, payload: dict) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            json=payload,
            headers={"X-Longhouse-Storage-Lane": "live"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["raw_state"] == "durable"
    return response.json()


def _session_row(root: Path, session_id: str) -> dict:
    engine = create_catalog_engine(root / "catalog.db")
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT render_state, current_render_generation FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
                .mappings()
                .one()
            )
        return dict(row)
    finally:
        engine.dispose()


async def _served(catalog: CatalogClient, session_id: str) -> tuple[str, str]:
    """What a client is told: the card's archive state and the transcript convergence."""

    read = await catalog.call("session.read.v2", {"session_id": session_id})
    assert read["found"] is True
    projected = project_catalog_session_facts(read["facts"], observed_at=datetime.fromisoformat(read["observed_at"]))
    return read["facts"]["card"]["archive_state"], projected.session_state.transcript.convergence


@pytest.mark.asyncio
async def test_raw_only_commit_does_not_demote_a_rendered_session(monkeypatch):
    session_id = str(uuid4())
    async with _stack(monkeypatch) as (app, catalog, root):
        await _ingest(app, _envelope(session_id=session_id, machine_id="cinder", opaque_source_id=STORE_SOURCE, with_render=True))
        rendered = _session_row(root, session_id)
        assert rendered["render_state"] == "ready"
        assert rendered["current_render_generation"] is not None
        assert await _served(catalog, session_id) == ("current", "current")

        # The JSONL projection: another source of the same session, raw bytes only.
        receipt = await _ingest(
            app, _envelope(session_id=session_id, machine_id="cinder", opaque_source_id=JSONL_SOURCE, with_render=False)
        )
        # The wire receipt still says this envelope carried no render; only the
        # session-level claim changed meaning.
        assert receipt["render_state"] == "pending"

        after = _session_row(root, session_id)
        assert after["render_state"] == "ready"
        assert after["current_render_generation"] == rendered["current_render_generation"]
        # The served facts, not just the column: a newer raw commit that has no
        # render to attach is not render debt.
        assert await _served(catalog, session_id) == ("current", "current")


@pytest.mark.asyncio
async def test_raw_only_session_is_pending_until_a_render_publishes(monkeypatch):
    session_id = str(uuid4())
    async with _stack(monkeypatch) as (app, catalog, root):
        await _ingest(app, _envelope(session_id=session_id, machine_id="cinder", opaque_source_id=JSONL_SOURCE, with_render=False))
        raw_only = _session_row(root, session_id)
        # Nothing publishes a render yet, and saying "ready" would be a lie.
        assert raw_only["render_state"] == "pending"
        assert raw_only["current_render_generation"] is None
        archive_state, _convergence = await _served(catalog, session_id)
        assert archive_state == "pending"

        await _ingest(app, _envelope(session_id=session_id, machine_id="cinder", opaque_source_id=STORE_SOURCE, with_render=True))
        rendered = _session_row(root, session_id)
        assert rendered["render_state"] == "ready"
        assert await _served(catalog, session_id) == ("current", "current")

        # ...and a later raw-only append leaves it there.
        await _ingest(app, _envelope(session_id=session_id, machine_id="cinder", opaque_source_id=JSONL_SOURCE + "0", with_render=False))
        assert _session_row(root, session_id)["render_state"] == "ready"
        assert await _served(catalog, session_id) == ("current", "current")


@pytest.mark.asyncio
async def test_raw_only_replacement_that_retires_the_only_render_is_pending_again(monkeypatch):
    """Stays truthful: 'ready' means a live render exists, not that one once did."""

    session_id = str(uuid4())
    first_epoch = str(uuid4())
    async with _stack(monkeypatch) as (app, catalog, root):
        await _ingest(
            app,
            _envelope(
                session_id=session_id,
                machine_id="cinder",
                opaque_source_id=STORE_SOURCE,
                source_epoch=first_epoch,
                with_render=True,
            ),
        )
        assert _session_row(root, session_id)["render_state"] == "ready"

        # The same source is rewritten as a raw-only replacement epoch; the
        # predecessor's raw and render objects retire with it.
        await _ingest(
            app,
            _envelope(
                session_id=session_id,
                machine_id="cinder",
                opaque_source_id=STORE_SOURCE,
                predecessor=first_epoch,
                opened_at="2026-09-01T13:00:00+00:00",
                with_render=False,
            ),
        )
        assert _session_row(root, session_id)["render_state"] == "pending"
