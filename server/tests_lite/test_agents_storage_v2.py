from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

import zerg.routers.agents_storage_v2 as storage_router
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.server import CatalogDaemon
from zerg.config import get_settings
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.raw_objects import read_raw_object


def _payload(*, tenant_id: str, machine_id: str, epoch: UUID, data: bytes = b"hello\n") -> dict:
    identity = EnvelopeIdentity(
        tenant_id=tenant_id,
        machine_id=machine_id,
        provider="codex",
        opaque_source_id="history.jsonl",
        source_epoch=epoch,
        range_kind="byte_offset",
        range_start=0,
        range_end=len(data),
        record_hashes=hash_records((data,)),
    )
    return {
        "protocol_version": 2,
        "tenant_id": tenant_id,
        "machine_id": machine_id,
        "session_id": "018f0c3a-7b2d-7f10-8a11-123456789abc",
        "provider": "codex",
        "opaque_source_id": "history.jsonl",
        "source_epoch": str(epoch),
        "predecessor_source_epoch": None,
        "epoch_opened_at": "2026-07-12T12:00:00+00:00",
        "range_kind": "byte_offset",
        "range_start": 0,
        "range_end": len(data),
        "records": [{"source_position": 0, "data_b64": base64.b64encode(data).decode("ascii")}],
        "expected_envelope_id": envelope_id(identity),
    }


@pytest.mark.asyncio
async def test_storage_v2_envelope_is_sealed_committed_and_replayed(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lh2-", dir="/tmp")
    root = Path(tempdir.name)
    database_path = root / "catalog.db"
    socket_path = root / "catalogd.sock"
    object_root = root / "objects"
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    catalog = CatalogClient(socket_path)
    workers = RawObjectWorkerPool(object_root, live_workers=1, repair_workers=1, queue_multiplier=1)
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)

    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    tenant_id = get_settings().archive_primary_tenant_id
    payload = _payload(
        tenant_id=tenant_id,
        machine_id="cinder",
        epoch=UUID("018f0c3a-7b2d-7f10-8a11-223456789abc"),
    )

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            capabilities = await client.get("/agents/storage/v2/capabilities")
            assert capabilities.status_code == 200
            assert capabilities.json()["tenant_id"] == tenant_id
            assert capabilities.json()["machine_id"] == "cinder"

            response = await client.post(
                "/agents/storage/v2/envelopes",
                json=payload,
                headers={"X-Longhouse-Storage-Lane": "live"},
            )
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert receipt == {
                "v": 2,
                "envelope_id": payload["expected_envelope_id"],
                "object_hash": receipt["object_hash"],
                "commit_seq": "2",
                "raw_state": "durable",
                "render_state": "pending",
                "media_state": "complete",
                "missing_media_hashes": [],
            }

            replay = await client.post(
                "/agents/storage/v2/envelopes",
                json=payload,
                headers={"X-Longhouse-Storage-Lane": "live"},
            )
            assert replay.status_code == 200
            assert replay.json() == receipt

        manifest = await catalog.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [payload["expected_envelope_id"]]},
        )
        raw = manifest["objects"][0]
        decoded = read_raw_object(
            object_root,
            f"raw/v2/{raw['object_hash'][:2]}/{raw['object_hash']}.zst",
            expected_object_hash=raw["object_hash"],
        )
        assert decoded.envelope_id == payload["expected_envelope_id"]
    finally:
        await workers.close()
        await catalog.close()
        await daemon.close()
        tempdir.cleanup()


@pytest.mark.asyncio
async def test_storage_v2_rejects_identity_mismatch_before_catalog_work(monkeypatch):
    class ForbiddenCatalog:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("identity mismatch reached catalogd")

    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: ForbiddenCatalog())
    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    payload = _payload(
        tenant_id=get_settings().archive_primary_tenant_id,
        machine_id="other-machine",
        epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            json=payload,
            headers={"X-Longhouse-Storage-Lane": "live"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "identity_mismatch"


@pytest.mark.asyncio
async def test_storage_v2_rejects_oversized_body_before_catalog_work(monkeypatch):
    class ForbiddenCatalog:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("oversized request reached catalogd")

    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: ForbiddenCatalog())
    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            content=b"x",
            headers={
                "Content-Length": str(storage_router.MAX_WIRE_BODY_BYTES + 1),
                "X-Longhouse-Storage-Lane": "live",
            },
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "storage_envelope_too_large"
