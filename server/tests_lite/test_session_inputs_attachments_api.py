"""Tests for the multipart input + attachment blob fetch endpoints.

Mirrors the structure of test_session_inputs_api.py for fixtures, but
exercises POST /sessions/{id}/inputs-multipart and the machine-token
GET /agents/sessions/.../attachments/{aid}/blob path.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi import UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from tests_lite.agents_fixture import SessionFixtureStore
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog as live_catalog
from tests_lite.live_catalog_harness import live_catalog_client as live_catalog_client
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.models.agents import SessionMediaRef
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.machine_control_channel import get_machine_control_channel_registry
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_input_attachments import StoredAttachment
from zerg.services.session_input_attachments import cleanup_stale_blobs
from zerg.services.session_input_attachments import get_catalog_attachment
from zerg.services.session_input_attachments import store_attachment_blob
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import requeue_stuck_delivering
from zerg.services.session_locks import session_lock_manager
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """An override installed here must not outlive this test.

    ``api_app`` is a process-global, so an override left behind keeps
    answering for every later test in the run. This file used to leave
    ``verify_agents_token`` returning device ``usage-stats``, and an unrelated
    storage-v2 test several hundred tests later failed with
    ``identity_mismatch``. Nothing catches that until an edit elsewhere
    reorders the suite, so each test puts back what it found.
    """

    from zerg.main import api_app

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


# A 1x1 PNG (~70 bytes) — enough to hash, well under the 2MB cap.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
    b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c"
    b"c\xfc\xcf\xc0P\x0f\x00\x05\x01\x01\x02\xb4\x9d\xb1\xa6\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_catalog_multipart_uses_live_receipt_without_legacy_db(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    receipt_id = str(uuid4())
    attachment_id = uuid4()
    source_session = SimpleNamespace(
        id=session_id,
        provider="codex",
        device_id="cinder",
        primary_thread_id=uuid4(),
        catalog_facts={
            "connections": [
                {
                    "control_plane": "codex_bridge",
                    "state": "attached",
                    "released_at": None,
                }
            ]
        },
    )
    calls: dict[str, object] = {}

    def load_scoped(db, sid, *, owner_id):
        # The load is owner-scoped: the route must pass the authenticated
        # caller, never an ambient "probably the only user" identity.
        calls["load_owner_id"] = owner_id
        return source_session

    monkeypatch.setattr(route, "_load_session_for_continuation", load_scoped)
    monkeypatch.setattr(route, "_assert_live_session_send_available", lambda *args, **kwargs: None)

    async def record_receipt(**kwargs):
        calls["receipt"] = kwargs
        return receipt_id

    async def store_blob(**kwargs):
        calls["store"] = kwargs
        return StoredAttachment(
            id=attachment_id,
            session_input_id=receipt_id,
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "blob.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def dispatch(**kwargs):
        calls["dispatch"] = kwargs
        return JSONResponse({"accepted": True})

    async def finish(**kwargs):
        calls.setdefault("finishes", []).append(kwargs)

    async def acquire(**kwargs):
        return SimpleNamespace()

    async def load_receipt(**kwargs):
        return None

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)

    monkeypatch.setattr(route, "record_live_input_receipt_best_effort", record_receipt)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "_build_managed_local_chat_response", dispatch)
    monkeypatch.setattr(route, "_finish_catalog_receipt", finish)
    monkeypatch.setattr(route.session_lock_manager, "acquire", acquire)

    upload = UploadFile(
        file=io.BytesIO(_PNG_BYTES),
        filename="a.png",
        headers=Headers({"content-type": "image/png"}),
    )
    response = await route.create_session_input_with_attachments(
        session_id=str(session_id),
        # A native client sends neither Origin nor Sec-Fetch-Site, so the
        # cross-origin form guard lets it through.
        request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
        text="look",
        intent="auto",
        client_request_id="catalog-attachment-1",
        attachments=[upload],
        user_agent="Longhouse-iOS",
        db=None,
        current_user=SimpleNamespace(id=7),
    )

    assert response.input_id is None
    assert calls["load_owner_id"] == 7
    assert response.live_input_id == receipt_id
    assert calls["store"]["input_receipt_id"] == receipt_id
    assert calls["dispatch"]["db"] is None
    assert f"/inputs/{receipt_id}/attachments/{attachment_id}/blob" in calls["dispatch"]["attachments"][0]["blob_url"]
    assert calls["finishes"] == [{"receipt_id": receipt_id, "delivery_request_id": calls["receipt"]["delivery_request_id"]}]


@pytest.mark.asyncio
async def test_catalog_multipart_keeps_group_when_dispatch_outcome_is_unknown(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    receipt_id = str(uuid4())
    source_session = SimpleNamespace(
        id=session_id,
        provider="claude",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="managed_local",
        catalog_facts={},
    )
    calls: dict[str, object] = {}
    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)
    monkeypatch.setattr(route, "_assert_live_session_send_available", lambda *args, **kwargs: None)

    async def load_receipt(**kwargs):
        return None

    async def record_receipt(**kwargs):
        calls["receipt"] = kwargs
        return receipt_id

    async def store_blob(**kwargs):
        return StoredAttachment(
            id=uuid4(),
            session_input_id=receipt_id,
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "blob.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def unknown_dispatch(**kwargs):
        raise RuntimeError("control response lost")

    async def mark_unknown(**kwargs):
        calls["unknown"] = kwargs
        return True

    async def delete_forbidden(**kwargs):
        raise AssertionError("ambiguous dispatch must retain attachment refs")

    async def acquire(**kwargs):
        return SimpleNamespace()

    async def release(*args, **kwargs):
        calls["release"] = kwargs

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)
    monkeypatch.setattr(route, "record_live_input_receipt_best_effort", record_receipt)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "_build_managed_local_chat_response", unknown_dispatch)
    monkeypatch.setattr(route, "_set_catalog_live_receipt_error", mark_unknown)
    monkeypatch.setattr(route, "delete_catalog_attachment_blobs", delete_forbidden)
    monkeypatch.setattr(route.session_lock_manager, "acquire", acquire)
    monkeypatch.setattr(route.session_lock_manager, "release", release)

    with pytest.raises(HTTPException) as excinfo:
        await route.create_session_input_with_attachments(
            session_id=str(session_id),
            request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
            text="look",
            intent="auto",
            client_request_id="catalog-attachment-unknown",
            attachments=[
                UploadFile(
                    file=io.BytesIO(_PNG_BYTES),
                    filename="a.png",
                    headers=Headers({"content-type": "image/png"}),
                )
            ],
            user_agent="Longhouse-iOS",
            db=None,
            current_user=SimpleNamespace(id=7),
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error_code"] == "delivery_unknown"
    assert calls["unknown"]["error"]["code"] == "delivery_unknown"


@pytest.mark.asyncio
async def test_catalog_multipart_cleans_group_after_partial_store_failure(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    receipt_id = str(uuid4())
    source_session = SimpleNamespace(
        id=session_id,
        provider="claude",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="managed_local",
        catalog_facts={},
    )
    calls: dict[str, object] = {}
    monkeypatch.setattr(route, "_assert_live_session_send_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)

    async def load_receipt(**kwargs):
        return None

    async def record_receipt(**kwargs):
        return receipt_id

    stored_count = 0

    async def store_blob(**kwargs):
        nonlocal stored_count
        stored_count += 1
        if stored_count == 2:
            raise RuntimeError("disk full")
        return StoredAttachment(
            id=uuid4(),
            session_input_id=receipt_id,
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "blob.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def finish(**kwargs):
        calls["finish"] = kwargs

    async def delete_group(**kwargs):
        calls["deleted_group"] = kwargs["input_receipt_id"]
        return 1

    async def acquire(**kwargs):
        return SimpleNamespace()

    async def release(*args, **kwargs):
        return None

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)
    monkeypatch.setattr(route, "record_live_input_receipt_best_effort", record_receipt)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "_finish_catalog_receipt", finish)
    monkeypatch.setattr(route, "delete_catalog_attachment_blobs", delete_group)
    monkeypatch.setattr(route.session_lock_manager, "acquire", acquire)
    monkeypatch.setattr(route.session_lock_manager, "release", release)

    with pytest.raises(HTTPException) as excinfo:
        await route.create_session_input_with_attachments(
            session_id=str(session_id),
            request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
            text="look",
            intent="auto",
            client_request_id="catalog-attachment-partial",
            attachments=[
                UploadFile(file=io.BytesIO(_PNG_BYTES), filename="a.png", headers=Headers({"content-type": "image/png"})),
                UploadFile(file=io.BytesIO(_PNG_BYTES), filename="b.png", headers=Headers({"content-type": "image/png"})),
            ],
            user_agent="Longhouse-iOS",
            db=None,
            current_user=SimpleNamespace(id=7),
        )

    assert excinfo.value.status_code == 500
    assert calls["deleted_group"] == receipt_id
    assert calls["finish"]["receipt_id"] == receipt_id


@pytest.mark.asyncio
async def test_console_multipart_stores_blobs_then_enqueues_turn_with_refs(monkeypatch, tmp_path):
    """A Console session takes the Console enqueue path: blobs first, then
    the turn carries their refs and a digest; no Helm lock, no live receipt."""
    import zerg.routers.session_inputs_attachments as route
    import zerg.services.console_turns as console_turns

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    attachment_id = uuid4()
    turn_id = uuid4()
    run_id = uuid4()
    source_session = SimpleNamespace(
        id=session_id,
        provider="claude",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="console_turn",
        catalog_facts={},
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)

    async def load_receipt(**kwargs):
        return None

    async def store_blob(**kwargs):
        calls["store"] = kwargs
        return StoredAttachment(
            id=attachment_id,
            session_input_id=kwargs["input_receipt_id"],
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "blob.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def enqueue(**kwargs):
        calls["enqueue"] = kwargs
        return SimpleNamespace(turn_id=turn_id, run_id=run_id, state="active", created=True, error=None, error_code=None)

    async def never_lock(**kwargs):
        raise AssertionError("Console attachments must not take the Helm dispatch lock")

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(console_turns, "enqueue_catalog_console_turn", enqueue)
    monkeypatch.setattr(route.session_lock_manager, "acquire", never_lock)

    upload = UploadFile(file=io.BytesIO(_PNG_BYTES), filename="a.png", headers=Headers({"content-type": "image/png"}))
    response = await route.create_session_input_with_attachments(
        session_id=str(session_id),
        request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
        text="what color",
        intent="auto",
        client_request_id="console-attach-1",
        attachments=[upload],
        user_agent="Longhouse-iOS",
        db=None,
        current_user=SimpleNamespace(id=7),
    )

    assert response.outcome == "sent"
    assert response.turn is not None and response.turn.turn_id == str(turn_id)
    assert response.live_input_id == str(turn_id)
    group_id = calls["store"]["input_receipt_id"]
    UUID(group_id)
    enqueue_call = calls["enqueue"]
    assert enqueue_call["message"] == "what color"
    assert enqueue_call["client_request_id"] == "console-attach-1"
    assert enqueue_call["attachments"][0]["blob_url"] == (
        f"/api/agents/sessions/{session_id}/inputs/{group_id}/attachments/{attachment_id}/blob"
    )
    assert enqueue_call["attachments_digest"]

    # A replay with the same client_request_id stores nothing and passes the
    # digest alone; catalogd compares it against the stored turn.
    async def load_existing(**kwargs):
        return SimpleNamespace(id=str(uuid4()))

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_existing)

    async def store_blob_forbidden(**kwargs):
        raise AssertionError("replay must not store blobs again")

    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob_forbidden)
    replay = await route.create_session_input_with_attachments(
        session_id=str(session_id),
        request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
        text="what color",
        intent="auto",
        client_request_id="console-attach-1",
        attachments=[UploadFile(file=io.BytesIO(_PNG_BYTES), filename="a.png", headers=Headers({"content-type": "image/png"}))],
        user_agent="Longhouse-iOS",
        db=None,
        current_user=SimpleNamespace(id=7),
    )
    assert replay.turn is not None and replay.turn.turn_id == str(turn_id)
    assert calls["enqueue"]["attachments"] == []
    assert calls["enqueue"]["attachments_digest"] == enqueue_call["attachments_digest"]

    # A concurrent identical upload can lose the catalog idempotency race
    # after storing its own group. That losing group must be deleted.
    losing_group: dict[str, str] = {}

    async def load_racing(**kwargs):
        del kwargs
        return None

    async def store_racing(**kwargs):
        losing_group["id"] = kwargs["input_receipt_id"]
        return StoredAttachment(
            id=uuid4(),
            session_input_id=kwargs["input_receipt_id"],
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "race.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def delete_racing(**kwargs):
        calls["deleted_group"] = kwargs["input_receipt_id"]
        return 1

    async def enqueue_existing(**kwargs):
        calls["racing_enqueue"] = kwargs
        return SimpleNamespace(
            turn_id=turn_id,
            run_id=run_id,
            state="active",
            created=False,
            error=None,
            error_code=None,
        )

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_racing)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_racing)
    monkeypatch.setattr(route, "delete_catalog_attachment_blobs", delete_racing)
    monkeypatch.setattr(console_turns, "enqueue_catalog_console_turn", enqueue_existing)
    await route.create_session_input_with_attachments(
        session_id=str(session_id),
        request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
        text="what color",
        intent="auto",
        client_request_id="console-attach-race",
        attachments=[UploadFile(file=io.BytesIO(_PNG_BYTES), filename="a.png", headers=Headers({"content-type": "image/png"}))],
        user_agent="Longhouse-iOS",
        db=None,
        current_user=SimpleNamespace(id=7),
    )
    assert calls["deleted_group"] == losing_group["id"]


@pytest.mark.asyncio
async def test_console_multipart_keeps_group_when_catalog_reply_is_ambiguous(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route
    import zerg.services.console_turns as console_turns

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    source_session = SimpleNamespace(
        id=session_id,
        provider="claude",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="console_turn",
        catalog_facts={},
    )
    group_ids: list[str] = []
    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)

    async def load_receipt(**kwargs):
        return None

    async def store_blob(**kwargs):
        group_ids.append(kwargs["input_receipt_id"])
        return StoredAttachment(
            id=uuid4(),
            session_input_id=kwargs["input_receipt_id"],
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "blob.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def enqueue_ambiguous(**kwargs):
        raise RuntimeError("catalog reply lost after commit")

    async def delete_forbidden(**kwargs):
        raise AssertionError("ambiguous catalog outcome must not delete committed refs")

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "delete_catalog_attachment_blobs", delete_forbidden)
    monkeypatch.setattr(console_turns, "enqueue_catalog_console_turn", enqueue_ambiguous)
    with pytest.raises(HTTPException) as excinfo:
        await route.create_session_input_with_attachments(
            session_id=str(session_id),
            request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
            text="what color",
            intent="auto",
            client_request_id="console-attach-ambiguous",
            attachments=[
                UploadFile(
                    file=io.BytesIO(_PNG_BYTES),
                    filename="a.png",
                    headers=Headers({"content-type": "image/png"}),
                )
            ],
            user_agent="Longhouse-iOS",
            db=None,
            current_user=SimpleNamespace(id=7),
        )
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error_code"] == "input_receipt_unknown"
    assert len(group_ids) == 1


@pytest.mark.asyncio
async def test_console_multipart_cleans_group_after_definite_dispatch_rejection(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route
    import zerg.services.console_turns as console_turns

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    source_session = SimpleNamespace(
        id=session_id,
        provider="claude",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="console_turn",
        catalog_facts={},
    )
    calls: dict[str, object] = {}
    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)

    async def no_receipt(**kwargs):
        return None

    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", no_receipt)

    async def store_blob(**kwargs):
        calls["group_id"] = kwargs["input_receipt_id"]
        return StoredAttachment(
            id=uuid4(),
            session_input_id=kwargs["input_receipt_id"],
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / "reject.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def delete_group(**kwargs):
        calls["deleted_group"] = kwargs["input_receipt_id"]
        return 1

    async def reject(**kwargs):
        return SimpleNamespace(
            turn_id=uuid4(),
            run_id=uuid4(),
            state="failed",
            created=True,
            error_code="provider_launch_failed",
            error="provider refused to start",
        )

    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "delete_catalog_attachment_blobs", delete_group)
    monkeypatch.setattr(console_turns, "enqueue_catalog_console_turn", reject)

    with pytest.raises(HTTPException) as excinfo:
        await route.create_session_input_with_attachments(
            session_id=str(session_id),
            request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
            text="what color",
            intent="auto",
            client_request_id="console-attach-rejected",
            attachments=[
                UploadFile(
                    file=io.BytesIO(_PNG_BYTES),
                    filename="a.png",
                    headers=Headers({"content-type": "image/png"}),
                )
            ],
            user_agent="Longhouse-iOS",
            db=None,
            current_user=SimpleNamespace(id=7),
        )

    assert excinfo.value.status_code == 502
    assert calls["deleted_group"] == calls["group_id"]


@pytest.mark.asyncio
async def test_console_multipart_rejects_unsupported_provider(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    _set_blob_root(monkeypatch, tmp_path)
    source_session = SimpleNamespace(
        id=uuid4(),
        provider="madeup",
        device_id="cinder",
        primary_thread_id=uuid4(),
        command_family="console_turn",
        catalog_facts={},
    )
    monkeypatch.setattr(route, "_load_session_for_continuation", lambda db, sid, *, owner_id: source_session)

    async def store_blob_forbidden(**kwargs):
        raise AssertionError("unsupported provider must be refused before storing")

    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob_forbidden)
    with pytest.raises(HTTPException) as excinfo:
        await route.create_session_input_with_attachments(
            session_id=str(source_session.id),
            request=SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
            text="x",
            intent="auto",
            client_request_id="console-attach-2",
            attachments=[UploadFile(file=io.BytesIO(_PNG_BYTES), filename="a.png", headers=Headers({"content-type": "image/png"}))],
            user_agent="Longhouse-iOS",
            db=None,
            current_user=SimpleNamespace(id=7),
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_catalog_multipart_runtime_draining_replays_same_receipt_and_bytes(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    _set_blob_root(monkeypatch, tmp_path)
    session_id = uuid4()
    receipt_id = str(uuid4())
    source_session = SimpleNamespace(
        id=session_id,
        provider="codex",
        device_id="cinder",
        primary_thread_id=uuid4(),
        catalog_facts={
            "connections": [
                {
                    "control_plane": "codex_bridge",
                    "state": "attached",
                    "released_at": None,
                }
            ]
        },
    )
    state = {"receipt": None, "dispatches": [], "stores": 0}

    def load_scoped(db, sid, *, owner_id):
        del db, sid, owner_id
        return source_session

    async def load_receipt(**kwargs):
        del kwargs
        return state["receipt"]

    async def record_receipt(**kwargs):
        state["receipt"] = SimpleNamespace(
            id=receipt_id,
            status=kwargs["status"],
            payload_digest=kwargs["payload_digest"],
            intent=kwargs["intent"],
            client_request_id=kwargs["client_request_id"],
            error_json=None,
            delivery_request_id=kwargs["delivery_request_id"],
        )
        return receipt_id

    async def mark_error(**kwargs):
        state["receipt"].status = "delivering"
        error = kwargs["error"]
        state["receipt"].error_json = json.dumps(
            {
                "code": error.get("code") or error.get("error_code") or "runtime_draining",
                "message": error.get("message") or error.get("error") or "Runtime is restarting",
            }
        )
        return True

    async def store_blob(**kwargs):
        state["stores"] += 1
        attachment_id = uuid4()
        return StoredAttachment(
            id=attachment_id,
            session_input_id=receipt_id,
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
            blob_path=tmp_path / f"blob-{state['stores']}.bin",
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    async def dispatch(**kwargs):
        state.setdefault("attempts", []).append(kwargs)
        if len(state["attempts"]) == 1:
            return JSONResponse(
                status_code=503,
                content={
                    "error_code": "runtime_draining",
                    "error": "Runtime is restarting",
                    "request_id": kwargs["request_id"],
                },
            )
        state["dispatches"].append(kwargs)
        return JSONResponse({"accepted": True})

    async def finish(**kwargs):
        state["receipt"].status = "delivered" if kwargs.get("error") is None else "failed"

    async def acquire(**kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(route, "_load_session_for_continuation", load_scoped)
    monkeypatch.setattr(route, "_assert_live_session_send_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(route, "load_live_input_receipt_by_client_request", load_receipt)
    monkeypatch.setattr(route, "record_live_input_receipt_best_effort", record_receipt)
    monkeypatch.setattr(route, "_set_catalog_live_receipt_error", mark_error)
    monkeypatch.setattr(route, "store_catalog_attachment_blob", store_blob)
    monkeypatch.setattr(route, "_build_managed_local_chat_response", dispatch)
    monkeypatch.setattr(route, "_finish_catalog_receipt", finish)
    monkeypatch.setattr(route.session_lock_manager, "acquire", acquire)

    def upload():
        return UploadFile(
            file=io.BytesIO(_PNG_BYTES),
            filename="a.png",
            headers=Headers({"content-type": "image/png"}),
        )

    kwargs = {
        "session_id": str(session_id),
        "request": SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http", netloc="testserver")),
        "text": "look",
        "intent": "auto",
        "client_request_id": "catalog-attachment-drain-1",
        "user_agent": "Longhouse-iOS",
        "db": None,
        "current_user": SimpleNamespace(id=7),
    }
    with pytest.raises(HTTPException) as refused:
        await route.create_session_input_with_attachments(attachments=[upload()], **kwargs)
    assert refused.value.status_code == 503
    assert refused.value.detail["error_code"] == "runtime_draining"
    assert state["receipt"].status == "delivering"
    original_delivery_request_id = state["receipt"].delivery_request_id

    replay = await route.create_session_input_with_attachments(attachments=[upload()], **kwargs)
    assert replay.outcome == "sent"
    assert state["receipt"].status == "delivered"
    assert len(state["attempts"]) == 2
    assert len(state["dispatches"]) == 1
    assert state["dispatches"][0]["request_id"] == original_delivery_request_id

    duplicate = await route.create_session_input_with_attachments(attachments=[upload()], **kwargs)
    assert duplicate.outcome == "sent"
    assert len(state["attempts"]) == 2
    assert len(state["dispatches"]) == 1


@pytest.mark.asyncio
async def test_catalog_attachment_blob_fetch_uses_catalog_metadata_without_legacy_db(monkeypatch, tmp_path):
    import zerg.routers.session_inputs_attachments as route

    session_id = uuid4()
    receipt_id = str(uuid4())
    attachment_id = uuid4()
    blob_path = tmp_path / "attachment.bin"
    blob_path.write_bytes(_PNG_BYTES)
    digest = hashlib.sha256(_PNG_BYTES).hexdigest()

    async def get_catalog(**kwargs):
        assert kwargs == {
            "owner_id": 7,
            "session_id": session_id,
            "input_receipt_id": receipt_id,
            "attachment_id": attachment_id,
        }
        return StoredAttachment(
            id=attachment_id,
            session_input_id=receipt_id,
            session_id=session_id,
            mime_type="image/png",
            byte_size=len(_PNG_BYTES),
            sha256=digest,
            blob_path=blob_path,
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )

    monkeypatch.setattr(route, "get_catalog_attachment", get_catalog)
    response = await route.fetch_attachment_blob(
        session_id=str(session_id),
        input_id=receipt_id,
        attachment_id=str(attachment_id),
        db=None,
        device_token=SimpleNamespace(owner_id=7),
        _single=None,
    )

    assert response.headers["x-attachment-sha256"] == digest
    assert response.headers["content-length"] == str(len(_PNG_BYTES))
    assert b"".join([chunk async for chunk in response.body_iterator]) == _PNG_BYTES


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_inputs_attachments.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    initialize_live_database(engine)
    return make_sessionmaker(engine)


def _make_client(session_local, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_browser_route_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def _seed_live_runtime_state(db, session, *, phase: str = "running") -> None:
    now = datetime.now(timezone.utc)
    freshness_ms = phase_freshness_ms(phase) or int(timedelta(minutes=5).total_seconds() * 1000)
    key = runtime_key_for_session(str(session.provider or "codex"), str(session.id))
    ingest_runtime_events(
        db,
        [
            RuntimeEventIngest(
                runtime_key=key,
                session_id=session.id,
                provider=str(session.provider or "codex"),
                device_id=session.device_id,
                source="test_runtime",
                kind="phase_signal",
                phase=phase,
                occurred_at=now,
                freshness_ms=freshness_ms,
                dedupe_key=f"attachment-test:{session.id}:{phase}:{now.isoformat()}",
            )
        ],
    )
    db.commit()


def _seed_codex_session(session_local):
    """Seed a managed-local codex session that satisfies the attach_images gate."""
    session_id = uuid4()
    provider_session_id = f"codex-attach-{uuid4().hex[:8]}"
    with session_local() as db:
        user = User(email=f"attach-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = SessionFixtureStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="codex",
                environment="Cinder",
                project="codex-attach",
                device_id="cinder",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                provider_session_id=provider_session_id,
                started_at=started_at,
                ended_at=started_at,
                events=[
                    EventIngest(
                        role="user",
                        content_text="seed",
                        timestamp=started_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )
        session = store.get_session(session_id)
        assert session is not None
        session.execution_home = "managed_local"
        session.managed_transport = "codex_app_server"
        session.source_runner_id = 1
        session.source_runner_name = "cinder"
        session.managed_session_name = "lh-attach"
        seed_managed_kernel_rows(db, session, control_plane="codex_bridge")
        runner = Runner(
            id=1,
            owner_id=user.id,
            name="cinder",
            status="online",
            auth_secret_hash="test",
        )
        db.merge(runner)
        db.commit()
        get_runner_connection_manager().register(user.id, 1, SimpleNamespace())
        _seed_live_runtime_state(db, session)
        user_id = user.id

    return session_id, user_id


# ---------------------------------------------------------------------------
# Live catalog: a Helm session whose control path is real
#
# The multipart route is catalog-only now -- a receipt through catalogd, blob
# metadata through catalogd, and a dispatch over the machine control channel.
# Nothing below is seeded directly: the launch RPCs create the session, thread,
# run and connection, and one Machine Agent heartbeat carries the control lease
# and the typed facts that bind the adapter identity and report the session
# idle. The capability gates the route checks are derived from those rows.
# ---------------------------------------------------------------------------

LIVE_DEVICE_ID = "cinder"


def _machine_heartbeat(*, device_id: str, now: datetime, raw_json: str | None = None) -> dict:
    """The heartbeat stamp the Machine Agent ships on every tick."""

    return {
        "device_id": device_id,
        "received_at": now.isoformat(),
        "version": "test-engine",
        "last_ship_at": now.isoformat(),
        "last_ship_attempt_at": now.isoformat(),
        "last_ship_result": "ok",
        "last_ship_latency_ms": 5,
        "last_ship_http_status": 200,
        "spool_pending": 0,
        "spool_dead": 0,
        "parse_errors_1h": 0,
        "ship_attempts_1h": 1,
        "ship_successes_1h": 1,
        "ship_rate_limited_1h": 0,
        "ship_server_errors_1h": 0,
        "ship_payload_rejections_1h": 0,
        "ship_payload_too_large_1h": 0,
        "ship_retryable_client_errors_1h": 0,
        "ship_connect_errors_1h": 0,
        "ship_latency_p50_ms_1h": 5,
        "ship_latency_p95_ms_1h": 5,
        "disk_free_bytes": 1_000_000,
        "is_offline": 0,
        "raw_json": raw_json,
        "sessions_digest": None,
        "sessions_sequence": None,
    }


def _machine_evidence(*, provider: str, session_id: str, run_id: str, now: datetime) -> dict:
    """The typed facts the provider adapter reports through the heartbeat.

    The control fact binds an adapter connection identity to the catalog
    connection; without it every command is refused with ``identity_unbound``.
    The activity fact is what makes the session quiescent.
    """

    from zerg.machine_evidence import canonical_evidence_hash

    connection_id = str(uuid4())
    lease_generation = str(uuid4())
    activity = {
        "authority_class": "provider_runtime",
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "kind": "idle",
        "raw_kind": "idle",
        "tool_name": None,
        "source": "provider_runtime",
        "observed_at": now.isoformat(),
        "valid_until": (now + timedelta(minutes=5)).isoformat(),
    }
    control = {
        "authority_class": "provider_control",
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "connection_id": connection_id,
        "lease_generation": lease_generation,
        "granted_operations": ["interrupt", "send_input"],
        "ownership": "managed",
        "state": "attached",
        "lease_ttl_ms": 300_000,
        "source": "provider_control",
        "observed_at": now.isoformat(),
    }
    return {
        "schema_version": 3,
        "activity": [activity],
        "control": [control],
        "identities": [
            {
                "fact_family": "activity",
                "fact_index": 0,
                "subject_key": f"run:{run_id}",
                "source": "provider_runtime",
                "source_epoch": run_id,
                "source_seq": 1,
                "sequenced": True,
                "dedupe_key": hashlib.sha256(f"{run_id}:activity:1".encode()).hexdigest(),
                "evidence_hash": canonical_evidence_hash(activity),
            },
            {
                "fact_family": "control",
                "fact_index": 0,
                "subject_key": f"connection:{connection_id}:{lease_generation}",
                "source": "provider_control",
                "source_epoch": lease_generation,
                "source_seq": None,
                "sequenced": False,
                "dedupe_key": hashlib.sha256(f"{connection_id}:{lease_generation}".encode()).hexdigest(),
                "evidence_hash": canonical_evidence_hash(control),
            },
        ],
    }


def _seed_live_catalog_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    provider: str = "codex",
    device_id: str = LIVE_DEVICE_ID,
) -> str:
    """Launch one Helm session in the live catalog and bring its control online."""

    from zerg.services.managed_provider_contracts import contract_for_provider

    contract = contract_for_provider(provider)
    assert contract is not None
    session_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = live.rpc(
        "session.launch.local.create.v2",
        {
            "launch": {
                "owner_id": owner_id,
                "git_repo": "cipher982/longhouse",
                "git_branch": "main",
                "started_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "plan": {
                    "session_id": session_id,
                    "provider": provider,
                    "provider_session_id": str(uuid4()),
                    "source_name": device_id,
                    "source_runner_id": None,
                    "cwd": "/workspace/longhouse",
                    "project": "session-input-attachments",
                    "display_name": "Session input attachments",
                    "managed_session_name": f"{provider}-attachments",
                    "permission_mode": "bypass",
                    "launch_actor": "user",
                    "launch_surface": "cli",
                    "environment": "test",
                    "origin_kind": None,
                    "hidden_from_default_timeline": 0,
                    "managed_transport": contract.managed_transport.value,
                    "attach_command": "",
                    "provider_config": {},
                },
            }
        },
    )
    run_id = str(created["run_id"])
    live.rpc(
        "session.launch.local.finish.v2",
        {
            "outcome": {
                "session_id": session_id,
                "run_id": run_id,
                "owner_id": owner_id,
                "device_id": device_id,
                "state": "adopted",
                "error_code": None,
                "error_message": None,
                "observed_at": now.isoformat(),
            }
        },
    )
    live.rpc(
        "machine.heartbeat.apply.v2",
        {
            "heartbeat": _machine_heartbeat(device_id=device_id, now=now, raw_json=None),
            "machine_evidence": _machine_evidence(
                provider=provider,
                session_id=session_id,
                run_id=run_id,
                now=now,
            ),
            "managed_leases": [
                {
                    "session_id": session_id,
                    "provider": provider,
                    "machine_id": device_id,
                    "sequence": 1,
                    "state": "attached",
                    "bridge_status": "ready",
                    "thread_subscription_status": "subscribed",
                    "observed_at": now.isoformat(),
                    "lease_ttl_ms": 300_000,
                }
            ],
            "managed_leases_present": True,
            "owner_id": owner_id,
        },
    )
    return session_id


def _live_catalog_receipt(live: LiveCatalog, *, owner_id: int, session_id: str, client_request_id: str) -> dict | None:
    """Read one input receipt back through catalogd, the way production reads it."""

    result = live.rpc(
        "session.input.receipt.read.v2",
        {"owner_id": owner_id, "session_id": session_id, "client_request_id": client_request_id},
    )
    return result["receipt"] if result.get("found") else None


class _AutoCompletingMachineWebSocket:
    """A Machine Agent control channel that accepts every command."""

    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message):
        self.sent.append(message)
        await get_machine_control_channel_registry().complete_command(
            {
                "type": "command_result",
                "command_id": message["command_id"],
                "ok": True,
                "result": {
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "turn_id": "machine-control-turn-1",
                },
            }
        )


async def _clear_machine_control_registry() -> None:
    await get_machine_control_channel_registry().clear_for_tests()


async def _register_fake_machine_control(
    *,
    owner_id: int,
    supports: list[str],
    device_id: str = LIVE_DEVICE_ID,
) -> _AutoCompletingMachineWebSocket:
    websocket = _AutoCompletingMachineWebSocket()
    await get_machine_control_channel_registry().register(
        owner_id=owner_id,
        device_id=device_id,
        machine_name=device_id,
        engine_build="test-engine",
        supports=supports,
        websocket=websocket,
    )
    return websocket


def _set_blob_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_ATTACHMENT_BLOB_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("LONGHOUSE_MEDIA_BLOB_ROOT", str(tmp_path / "media"))


def test_multipart_upload_succeeds_on_codex(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-codex@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["codex.send"]))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/inputs-multipart",
            data={"text": "look at this", "intent": "auto", "client_request_id": "attach-codex-1"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        # The catalog receipt is the whole record; nothing is projected into an
        # archive session_inputs row, so there is no integer id to hand back.
        assert body["input_id"] is None
        assert body["live_input_id"]

        assert len(websocket.sent) == 1
        payload = websocket.sent[0]["payload"]
        assert websocket.sent[0]["command_type"] == "session.send_text"
        assert payload["text"] == "look at this"
        forwarded = payload["attachments"]
        assert len(forwarded) == 1
        ref = forwarded[0]
        assert ref["mime_type"] == "image/png"
        assert ref["sha256"] == hashlib.sha256(_PNG_BYTES).hexdigest()
        assert ref["blob_url"] == (f"/api/agents/sessions/{session_id}/inputs/{body['live_input_id']}/attachments/{ref['id']}/blob")

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="attach-codex-1",
        )
        assert receipt is not None
        assert receipt["id"] == body["live_input_id"]
        assert receipt["status"] == INPUT_STATUS_DELIVERED
        assert receipt["archive_session_input_id"] is None

        stored = asyncio.run(
            get_catalog_attachment(
                owner_id=owner_id,
                session_id=UUID(session_id),
                input_receipt_id=body["live_input_id"],
                attachment_id=UUID(ref["id"]),
            )
        )
        assert stored is not None
        assert stored.mime_type == "image/png"
        assert stored.byte_size == len(_PNG_BYTES)
        assert stored.original_filename == "a.png"
        assert stored.blob_path.read_bytes() == _PNG_BYTES
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_multipart_upload_succeeds_on_claude_helm(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    # The support table, not the transport, decides: a Claude Helm session
    # takes the same refs the engine stages and names in the prompt.
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-claude@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="claude")
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["claude.send"]))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/inputs-multipart",
            data={"text": "what color is this", "intent": "auto", "client_request_id": "attach-claude-1"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert len(websocket.sent) == 1
        payload = websocket.sent[0]["payload"]
        assert websocket.sent[0]["command_type"] == "session.send_text"
        assert payload["provider"] == "claude"
        assert payload["text"] == "what color is this"
        assert payload["attachments"][0]["sha256"] == hashlib.sha256(_PNG_BYTES).hexdigest()
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_multipart_accepts_attachment_only_input(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-only@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["codex.send"]))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/inputs-multipart",
            data={"text": "", "intent": "auto", "client_request_id": "attachment-only-1"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
            cookies=cookies,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert len(websocket.sent) == 1
        payload = websocket.sent[0]["payload"]
        assert payload["text"] == ""
        assert len(payload["attachments"]) == 1

        receipt = _live_catalog_receipt(
            live_catalog,
            owner_id=owner_id,
            session_id=session_id,
            client_request_id="attachment-only-1",
        )
        assert receipt is not None
        assert receipt["status"] == INPUT_STATUS_DELIVERED
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_multipart_rejects_unsupported_helm_provider(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    # Antigravity Helm has no image delivery (its hook inbox carries a plain
    # string), so the explicit support table refuses it before anything is
    # persisted. Every other provider accepts; see the Claude test below.
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-agy@test.local"
    owner_id = live_catalog.create_user(email)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id, provider="antigravity")
    # A control channel that would happily accept the send, so the rejection
    # below is a fact about the support gate, not about the machine.
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["antigravity.send"]))

    try:
        resp = live_catalog_client.post(
            f"/sessions/{session_id}/inputs-multipart",
            data={"text": "blocked", "intent": "auto", "client_request_id": "attach-agy-1"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
            cookies=cookies,
        )
        assert resp.status_code == 409, resp.text
        assert "attachments" in resp.json()["detail"].lower()
        assert websocket.sent == []
        # The gate runs before anything is persisted: no receipt, no blob.
        assert (
            _live_catalog_receipt(
                live_catalog,
                owner_id=owner_id,
                session_id=session_id,
                client_request_id="attach-agy-1",
            )
            is None
        )
        assert list((tmp_path / "blobs").rglob("*.bin")) == []
    finally:
        asyncio.run(_clear_machine_control_registry())


def test_multipart_rejects_queue_intent(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "queue?", "intent": "queue", "client_request_id": "attach-queue-reject"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert resp.status_code == 400, resp.text
        assert "intent" in resp.json()["detail"].lower()
    finally:
        api_app_ref.dependency_overrides = {}


def test_multipart_rejects_unsupported_mime(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "bad type", "intent": "auto", "client_request_id": "attach-mime-reject"},
            files=[("attachments", ("a.txt", io.BytesIO(b"hi"), "text/plain"))],
        )
        assert resp.status_code == 400, resp.text
        assert "unsupported" in resp.json()["detail"].lower()
    finally:
        api_app_ref.dependency_overrides = {}


def test_multipart_rejects_oversize(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    big = b"\x00" * (3 * 1024 * 1024)  # 3 MB > 2 MB cap

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "huge", "intent": "auto", "client_request_id": "attach-oversize-reject"},
            files=[("attachments", ("big.png", io.BytesIO(big), "image/png"))],
        )
        assert resp.status_code == 400, resp.text
        assert "MB" in resp.json()["detail"] or "exceed" in resp.json()["detail"].lower()
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def _upload_one_attachment(live_catalog, live_catalog_client, *, owner_id, email, session_id, websocket):  # noqa: F811
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}
    resp = live_catalog_client.post(
        f"/sessions/{session_id}/inputs-multipart",
        data={"text": "look", "intent": "auto", "client_request_id": f"attach-fetch-{session_id}"},
        files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    ref = websocket.sent[0]["payload"]["attachments"][0]
    return resp.json()["live_input_id"], ref


def test_machine_blob_fetch_streams_bytes(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-fetch@test.local"
    owner_id = live_catalog.create_user(email)
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["codex.send"]))
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=LIVE_DEVICE_ID)

    try:
        input_id, ref = _upload_one_attachment(
            live_catalog,
            live_catalog_client,
            owner_id=owner_id,
            email=email,
            session_id=session_id,
            websocket=websocket,
        )

        resp = live_catalog_client.get(
            f"/agents/sessions/{session_id}/inputs/{input_id}/attachments/{ref['id']}/blob",
            headers={"X-Agents-Token": token},
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == _PNG_BYTES
        assert resp.headers["X-Attachment-Sha256"] == ref["sha256"]
        assert resp.headers["X-Attachment-Bytes"] == str(len(_PNG_BYTES))
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_machine_blob_fetch_404_on_session_mismatch(live_catalog, live_catalog_client, monkeypatch, tmp_path):  # noqa: F811
    _set_blob_root(monkeypatch, tmp_path)
    email = "attach-mismatch@test.local"
    owner_id = live_catalog.create_user(email)
    session_id = _seed_live_catalog_session(live_catalog, owner_id=owner_id)
    websocket = asyncio.run(_register_fake_machine_control(owner_id=owner_id, supports=["codex.send"]))
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=LIVE_DEVICE_ID)

    try:
        input_id, ref = _upload_one_attachment(
            live_catalog,
            live_catalog_client,
            owner_id=owner_id,
            email=email,
            session_id=session_id,
            websocket=websocket,
        )

        resp = live_catalog_client.get(
            f"/agents/sessions/{uuid4()}/inputs/{input_id}/attachments/{ref['id']}/blob",
            headers={"X-Agents-Token": token},
        )
        assert resp.status_code == 404, resp.text
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        asyncio.run(_clear_machine_control_registry())


def test_attachment_store_commit_failure_rolls_back_media_rows(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="look at this",
            owner_id=user_id,
            intent="auto",
            status="delivering",
            client_request_id="commit-failure",
            delivery_request_id="commit-failure-delivery",
        )

        def fail_commit():
            raise RuntimeError("commit failed")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            store_attachment_blob(
                db,
                session_input=row,
                mime_type="image/png",
                data=_PNG_BYTES,
                original_filename="a.png",
                original_byte_size=len(_PNG_BYTES),
            )

        digest = hashlib.sha256(_PNG_BYTES).hexdigest()
        assert db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == row.id).count() == 0
        assert db.query(MediaObject).filter(MediaObject.sha256 == digest).count() == 0
        assert db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).count() == 0
        assert list((tmp_path / "blobs").rglob("*.bin")) == []


def test_cleanup_stale_blobs_preserves_only_remaining_attachment_copy(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="look at this",
            owner_id=user_id,
            intent="auto",
            status=INPUT_STATUS_DELIVERED,
            client_request_id="cleanup-missing-media",
            delivery_request_id="cleanup-missing-media-delivery",
        )
        stored = store_attachment_blob(
            db,
            session_input=row,
            mime_type="image/png",
            data=_PNG_BYTES,
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )
        attach = db.query(SessionInputAttachment).filter(SessionInputAttachment.id == stored.id).one()
        blob_path = tmp_path / "blobs" / attach.blob_path
        media = db.query(MediaObject).filter(MediaObject.sha256 == attach.sha256).one()
        media_path = tmp_path / "media" / media.storage_path
        assert blob_path.exists()
        assert media_path.exists()

        media_path.unlink()
        db.delete(media)
        attach.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()

        removed = cleanup_stale_blobs(db)

        assert removed == 0
        assert blob_path.exists()
        assert db.query(SessionInputAttachment).filter(SessionInputAttachment.id == stored.id).count() == 1


def test_startup_reconciliation_fails_stuck_attachment_rows(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)

    with session_local() as db:
        row = create_session_input(
            db,
            session_id=session_id,
            text="look at this",
            owner_id=user_id,
            intent="auto",
            status="delivering",
            client_request_id="crash-attachment",
            delivery_request_id="crash-attachment-delivery",
        )
        store_attachment_blob(
            db,
            session_input=row,
            mime_type="image/png",
            data=_PNG_BYTES,
            original_filename="a.png",
            original_byte_size=len(_PNG_BYTES),
        )
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        db.commit()

        requeued = requeue_stuck_delivering(db)

        assert requeued == 0
        db.expire_all()
        refreshed = db.query(SessionInput).filter(SessionInput.id == row.id).one()
        assert refreshed.status == INPUT_STATUS_FAILED
        assert refreshed.last_error == "attachment delivery interrupted by restart"
        assert refreshed.client_request_id == "crash-attachment"
        assert refreshed.delivery_request_id == "crash-attachment-delivery"
