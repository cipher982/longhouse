"""A parent with a retired duplicate adopts its subagents on its next commit.

Drives the real ingest path (CatalogDaemon, raw-object workers, an envelope
POST), seeded the way david010 looked on 2026-10-08: a retired session row that
shares the parent's provider-native id, and subagents that name that id. Before
the fix their parent resolved as ambiguous, they never bound, and every append
re-resolved them inside the catalog writer.
"""

from __future__ import annotations

import base64
import os
import sqlite3
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
from tests_lite.test_storage_v2_conversation_reset_alias import _InlineRenderPool
from tests_lite.test_storage_v2_conversation_reset_alias import _render_record
from tests_lite.test_storage_v2_conversation_reset_alias import _seed_helm_session
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records

_LINE = 120


def _append(*, tenant: str, session_id: str, native_id: str, epoch, generation_id: str, position: int) -> dict:
    data = (b'{"role":"user","n":%d}\n' % position).ljust(_LINE, b" ")
    identity = EnvelopeIdentity(
        tenant_id=tenant,
        machine_id="cinder",
        provider="claude",
        opaque_source_id=f"{native_id}.jsonl",
        source_epoch=epoch,
        range_kind="byte_offset",
        range_start=position * _LINE,
        range_end=(position + 1) * _LINE,
        record_hashes=hash_records((data,)),
    )
    return {
        "protocol_version": 2,
        "tenant_id": tenant,
        "machine_id": "cinder",
        "session_id": session_id,
        "provider": "claude",
        "opaque_source_id": f"{native_id}.jsonl",
        "source_epoch": str(epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": "2026-08-01T12:00:00+00:00",
        "range_kind": "byte_offset",
        "range_start": position * _LINE,
        "range_end": (position + 1) * _LINE,
        "render": {
            "generation_id": generation_id,
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                _render_record(
                    event_id=str(uuid4()),
                    order_time_us=1_722_500_000_000_000 + position,
                    source_position=position * _LINE,
                    content_text=f"message {position}",
                )
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
            "provider_session_id": native_id,
        },
        "records": [{"source_position": position * _LINE, "data_b64": base64.b64encode(data).decode("ascii")}],
        "expected_envelope_id": envelope_id(identity),
    }


def _seed_retired_duplicate_and_children(database: Path, *, session_id: str, native_id: str, children: int) -> list[str]:
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    base = dict(connection.execute("select * from sessions where session_id = ?", (session_id,)).fetchone())
    columns = list(base)

    def insert(row: dict) -> None:
        connection.execute(
            f"insert into sessions ({','.join(columns)}) values ({','.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )

    insert(
        dict(
            base,
            session_id=native_id,
            provider_session_id=native_id,
            raw_state="retired",
            render_state="retired",
            hidden_from_default_timeline=1,
            current_render_generation=None,
        )
    )
    child_ids = []
    for _ in range(children):
        child_id = str(uuid4())
        insert(
            dict(
                base,
                session_id=child_id,
                provider_session_id=str(uuid4()),
                is_subagent=1,
                subagent_parent_provider_session_id=native_id,
                subagent_parent_session_id=None,
                current_render_generation=None,
            )
        )
        child_ids.append(child_id)
    connection.commit()
    connection.close()
    return child_ids


@pytest.mark.asyncio
async def test_a_parent_with_a_retired_duplicate_adopts_its_subagents(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lh2-retired-parent-", dir="/tmp")
    root = Path(tempdir.name)
    session_id = str(uuid4())
    native_id = str(uuid4())
    _seed_helm_session(root / "catalog.db", session_id=session_id, previous_native_id=native_id)

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
    tenant = get_settings().archive_primary_tenant_id
    epoch = uuid4()
    generation_id = str(uuid4())

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def post(position: int) -> None:
                response = await client.post(
                    "/agents/storage/v2/envelopes",
                    json=_append(
                        tenant=tenant,
                        session_id=session_id,
                        native_id=native_id,
                        epoch=epoch,
                        generation_id=generation_id,
                        position=position,
                    ),
                    headers={"X-Longhouse-Storage-Lane": "live"},
                )
                assert response.status_code == 200, response.text

            await post(0)
            child_ids = _seed_retired_duplicate_and_children(root / "catalog.db", session_id=session_id, native_id=native_id, children=3)
            await post(1)

        check = sqlite3.connect(root / "catalog.db")
        bound = {
            row[0]: row[1]
            for row in check.execute(
                f"select session_id, subagent_parent_session_id from sessions where session_id in ({','.join('?' for _ in child_ids)})",
                child_ids,
            )
        }
        check.close()
        assert bound == {child: session_id for child in child_ids}
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()
