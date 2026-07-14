from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

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
from zerg.services.raw_object_workers import RawObjectWorkerBusy
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import RenderDetailCursor
from zerg.storage_v2.contracts import envelope_id
from zerg.storage_v2.contracts import hash_records
from zerg.storage_v2.contracts import render_detail_cursor_token
from zerg.storage_v2.raw_objects import read_raw_object
from zerg.storage_v2.render_objects import read_render_object
from zerg.storage_v2.render_objects import seal_render_object


class _AdmissionOnlyPool:
    @asynccontextmanager
    async def admission(self, _lane):
        yield

    async def seal(self, *_args, **_kwargs):
        raise AssertionError("rejected request reached storage worker")


class _BusyPool:
    @asynccontextmanager
    async def admission(self, lane):
        raise RawObjectWorkerBusy(f"{lane} full")
        yield


class _ForbiddenRenderAdmission:
    @asynccontextmanager
    async def admission(self, _lane):
        raise AssertionError("request-level render admission must not be reserved")
        yield


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
        "render": {
            "generation_id": "018f0c3a-7b2d-7f10-8a11-423456789abc",
            "parser_revision": "engine-parser-v2",
            "ordering_revision": "semantic-order-v2",
            "records": [
                {
                    "event_id": "user-1",
                    "order_time_us": 1_720_780_400_000_000,
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
                },
                {
                    "event_id": "assistant-1",
                    "order_time_us": 1_720_780_400_000_001,
                    "source_position": 1,
                    "event_subordinal": 0,
                    "role": "assistant",
                    "content_text": "world",
                    "tool_name": None,
                    "tool_input_json": None,
                    "tool_output_text": None,
                    "tool_call_id": None,
                    "thread_id": None,
                    "branch_kind": None,
                    "raw_record_ordinal": 0,
                },
            ],
        },
        "media": [],
        "session": {
            "environment": "local",
            "project": "longhouse",
            "cwd": "/workspace/longhouse",
            "git_repo": "cipher982/longhouse",
            "git_branch": "main",
            "started_at": "2026-07-12T11:00:00+00:00",
            "last_activity_at": "2026-07-12T12:00:00+00:00",
            "ended_at": None,
            "origin_kind": "shadow",
            "hidden_from_default_timeline": False,
            "launch_actor": None,
            "launch_surface": None,
        },
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
    render_workers = _InlineRenderPool(object_root)
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: render_workers)

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
            assert capabilities.json()["cutover"] is True
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
                "commit_seq": "1",
                "raw_state": "durable",
                "render_state": "ready",
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

            timeline = await client.get("/agents/storage/v2/sessions")
            assert timeline.status_code == 200, timeline.text
            assert [row["session_id"] for row in timeline.json()["sessions"]] == [payload["session_id"]]
            assert timeline.json()["sessions"][0]["raw_state"] == "durable"

            raw_page = await client.get(f"/agents/storage/v2/sessions/{payload['session_id']}/raw")
            assert raw_page.status_code == 200, raw_page.text
            assert raw_page.json()["object"]["envelope_id"] == payload["expected_envelope_id"]
            assert base64.b64decode(raw_page.json()["records"][0]["data_b64"]) == b"hello\n"
            assert raw_page.json()["has_more"] is False

            detail = await client.get(f"/agents/storage/v2/sessions/{payload['session_id']}/events?limit=1")
            assert detail.status_code == 200, detail.text
            page = detail.json()
            assert page["generation_id"] == payload["render"]["generation_id"]
            assert page["events"][0]["event_id"] == "user-1"
            assert page["events"][0]["content_text"] == "hello"
            assert page["events"][0]["raw_locator"]["source_envelope_id"] == payload["expected_envelope_id"]
            assert page["has_more"] is True
            assert page["next_cursor"]
            second = await client.get(
                f"/agents/storage/v2/sessions/{payload['session_id']}/events",
                params={"cursor": page["next_cursor"], "limit": 1},
            )
            assert second.status_code == 200, second.text
            assert second.json()["events"][0]["event_id"] == "assistant-1"
            assert second.json()["has_more"] is False

            tail = await client.get(
                f"/agents/storage/v2/sessions/{payload['session_id']}/events",
                params={"anchor": "tail", "limit": 1},
            )
            assert tail.status_code == 200, tail.text
            assert tail.json()["events"][0]["event_id"] == "assistant-1"
            assert tail.json()["has_more"] is True
            older = await client.get(
                f"/agents/storage/v2/sessions/{payload['session_id']}/events",
                params={"anchor": "tail", "cursor": tail.json()["next_cursor"], "limit": 1},
            )
            assert older.status_code == 200, older.text
            assert older.json()["events"][0]["event_id"] == "user-1"
            assert older.json()["has_more"] is False

            stale_cursor = render_detail_cursor_token(
                RenderDetailCursor(
                    session_id=UUID(payload["session_id"]),
                    render_generation=uuid4(),
                    order_time_us=1_720_780_400_000_000,
                    machine_id="cinder",
                    provider="codex",
                    opaque_source_id="history.jsonl",
                    source_epoch=UUID(payload["source_epoch"]),
                    source_position=0,
                    event_subordinal=0,
                )
            )
            stale = await client.get(
                f"/agents/storage/v2/sessions/{payload['session_id']}/events",
                params={"cursor": stale_cursor},
            )
            assert stale.status_code == 409
            assert stale.json()["detail"]["code"] == "stale_generation"
            assert stale.json()["detail"]["details"]["current_generation_id"] == payload["render"]["generation_id"]

        manifest = await catalog.call(
            "storage.raw_object.exists.batch.v2",
            {"envelope_ids": [payload["expected_envelope_id"]]},
        )
        session = await catalog.call(
            "storage.session.read.v2",
            {"session_id": payload["session_id"]},
        )
        assert session["session"]["owner_id"] == "1"
        assert session["session"]["project"] == "longhouse"
        assert session["session"]["current_render_generation"] == payload["render"]["generation_id"]
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
async def test_storage_v2_media_must_be_durable_before_complete_receipt(monkeypatch):
    tempdir = TemporaryDirectory(prefix="lh2-media-", dir="/tmp")
    root = Path(tempdir.name)
    daemon = CatalogDaemon(database_path=root / "catalog.db", socket_path=root / "catalogd.sock")
    await daemon.start()
    catalog = CatalogClient(root / "catalogd.sock")
    workers = RawObjectWorkerPool(root / "objects", live_workers=1, repair_workers=1, queue_multiplier=1)
    render_workers = _InlineRenderPool(root / "objects")
    await workers.start()
    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: catalog)
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", lambda: workers)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", lambda: render_workers)

    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    tenant_id = get_settings().archive_primary_tenant_id
    payload = _payload(
        tenant_id=tenant_id,
        machine_id="cinder",
        epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
        data=b"media-ref\n",
    )
    media_bytes = b"\x89PNG\r\n\x1a\nexact-media"
    media_hash = hashlib.sha256(media_bytes).hexdigest()
    payload["media"] = [
        {
            "sha256": media_hash,
            "source_position": 0,
            "ref_key": f"inline_data_url:0:{'a' * 64}:0",
            "availability": "available",
        }
    ]

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            premature = await client.post(
                "/agents/storage/v2/envelopes",
                json=payload,
                headers={"X-Longhouse-Storage-Lane": "live"},
            )
            assert premature.status_code == 409, premature.text
            assert premature.json()["detail"]["code"] == "media_unavailable"

            claim = await client.post(
                "/agents/storage/v2/media/claims",
                json={"items": [{"sha256": media_hash, "mime_type": "image/png", "byte_size": len(media_bytes)}]},
            )
            assert claim.status_code == 200
            assert claim.json() == {"needed": [media_hash], "present": [], "rejected": []}

            bad_upload = await client.put(
                f"/agents/storage/v2/media/{media_hash}",
                content=b"wrong",
                headers={"Content-Type": "image/png", "X-Longhouse-Storage-Lane": "live"},
            )
            assert bad_upload.status_code == 422

            upload = await client.put(
                f"/agents/storage/v2/media/{media_hash}",
                content=media_bytes,
                headers={"Content-Type": "image/png", "X-Longhouse-Storage-Lane": "live"},
            )
            assert upload.status_code == 200, upload.text
            assert upload.json()["sha256"] == media_hash

            alias_claim = await client.post(
                "/agents/storage/v2/media/claims",
                json={
                    "items": [
                        {
                            "sha256": media_hash,
                            "mime_type": "application/octet-stream",
                            "byte_size": len(media_bytes),
                        }
                    ]
                },
            )
            assert alias_claim.json() == {"needed": [], "present": [media_hash], "rejected": []}

            committed = await client.post(
                "/agents/storage/v2/envelopes",
                json=payload,
                headers={"X-Longhouse-Storage-Lane": "live"},
            )
            assert committed.status_code == 200, committed.text
            receipt = committed.json()
            assert receipt["media_state"] == "complete"
            assert receipt["missing_media_hashes"] == []

            replay = await client.post(
                "/agents/storage/v2/envelopes",
                json=payload,
                headers={"X-Longhouse-Storage-Lane": "live"},
            )
            assert replay.json() == receipt

            head = await client.head(f"/agents/storage/v2/media/{media_hash}")
            assert head.status_code == 200
            assert head.headers["content-length"] == str(len(media_bytes))
            fetched = await client.get(f"/agents/storage/v2/media/{media_hash}/blob")
            assert fetched.status_code == 200
            assert fetched.content == media_bytes

            manifest = await catalog.call(
                "storage.media.read.v2",
                {"media_hash": media_hash, "session_id": payload["session_id"], "limit": 10},
            )
            assert manifest["media"]["state"] == "present"
            assert manifest["refs"][0]["envelope_id"] == payload["expected_envelope_id"]
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
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", _AdmissionOnlyPool)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", _AdmissionOnlyPool)
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
async def test_storage_v2_busy_lane_returns_typed_backpressure(monkeypatch):
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", _BusyPool)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", _AdmissionOnlyPool)
    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    payload = _payload(
        tenant_id=get_settings().archive_primary_tenant_id,
        machine_id="cinder",
        epoch=UUID("018f0c3a-7b2d-7f10-8a11-323456789abc"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            json=payload,
            headers={"X-Longhouse-Storage-Lane": "repair"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_lane_busy"
    assert response.headers["x-longhouse-storage-backpressure"] == "storage_lane_busy"
    assert response.headers["x-longhouse-storage-lane"] == "repair"
    assert response.headers["retry-after"] == "5"


@pytest.mark.asyncio
async def test_storage_v2_request_admission_does_not_reserve_optional_render_lane(monkeypatch):
    async def commit_stub(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", _AdmissionOnlyPool)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", _ForbiddenRenderAdmission)
    monkeypatch.setattr(storage_router, "_commit_admitted_envelope", commit_stub)
    app = FastAPI()
    app.include_router(storage_router.router)
    app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="cinder", owner_id=1)
    app.dependency_overrides[require_single_tenant] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/agents/storage/v2/envelopes",
            content=b"{}",
            headers={"X-Longhouse-Storage-Lane": "live"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_storage_v2_rejects_oversized_body_before_catalog_work(monkeypatch):
    class ForbiddenCatalog:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("oversized request reached catalogd")

    monkeypatch.setattr(storage_router, "get_catalogd_client", lambda: ForbiddenCatalog())
    monkeypatch.setattr(storage_router, "get_raw_object_worker_pool", _AdmissionOnlyPool)
    monkeypatch.setattr(storage_router, "get_render_object_worker_pool", _AdmissionOnlyPool)
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
