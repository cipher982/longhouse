from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from zerg.routers.timeline import get_timeline_session_workspace
from zerg.services.storage_v2_export import build_storage_v2_raw_export
from zerg.storage_v2.raw_objects import RawRecord


@pytest.mark.asyncio
async def test_raw_export_streams_verified_objects_in_source_order(monkeypatch):
    session_id = UUID("11111111-2222-3333-4444-555555555555")
    manifest_item = {
        "machine_id": "cinder",
        "provider": "codex",
        "opaque_source_id": "source",
        "source_epoch": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "range_start": "0",
        "envelope_id": "envelope",
        "object_path": "raw/v2/aa/object.zst",
        "object_hash": "a" * 64,
        "tenant_id": "tenant-seven",
    }

    class Catalog:
        async def call(self, method, params):
            if method == "storage.session.read.v2":
                return {
                    "session": {
                        "owner_id": "7",
                        "provider": "codex",
                        "cwd": "/workspace",
                        "project": "longhouse",
                    }
                }
            assert method == "storage.session.raw_manifest.v2"
            return {"objects": [manifest_item], "objects_truncated": False}

    class Workers:
        async def read(self, object_path, object_hash, tenant_id):
            assert object_path == manifest_item["object_path"]
            assert object_hash == manifest_item["object_hash"]
            assert tenant_id == manifest_item["tenant_id"]
            return SimpleNamespace(
                envelope_id="envelope",
                spec=SimpleNamespace(
                    session_id=session_id,
                    records=(
                        RawRecord(source_position=0, data=b'{"one":1}'),
                        RawRecord(source_position=1, data=b'{"two":2}\n'),
                    ),
                ),
            )

    monkeypatch.setattr("zerg.services.storage_v2_export.get_catalogd_client", lambda: Catalog())
    monkeypatch.setattr("zerg.services.storage_v2_export.get_raw_object_worker_pool", lambda: Workers())

    response = await build_storage_v2_raw_export(session_id=session_id, owner_id=7, branch_mode="head")
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b'{"one":1}\n{"two":2}\n'
    assert response.headers["x-longhouse-storage"] == "v2"


@pytest.mark.asyncio
async def test_production_workspace_never_falls_back_to_legacy_sqlite(monkeypatch):
    session_id = UUID("11111111-2222-3333-4444-555555555555")

    async def missing_workspace(**_kwargs):
        return None

    monkeypatch.setattr("zerg.routers.timeline.build_storage_v2_workspace", missing_workspace)
    monkeypatch.setattr("zerg.routers.timeline.get_settings", lambda: SimpleNamespace(testing=False))

    def forbidden_legacy_factory():
        raise AssertionError("production must not open legacy SQLite")

    with pytest.raises(HTTPException) as error:
        await get_timeline_session_workspace(
            session_id=session_id,
            response=Response(),
            branch_mode="head",
            limit=100,
            cursor=None,
            shared_by=None,
            share_token=None,
            legacy_session_factory=forbidden_legacy_factory,
            current_user=SimpleNamespace(id=7),
        )

    assert error.value.status_code == 404
