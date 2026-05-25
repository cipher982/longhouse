"""Tests for the multipart input + attachment blob fetch endpoints.

Mirrors the structure of test_session_inputs_api.py for fixtures, but
exercises POST /sessions/{id}/inputs-multipart and the machine-token
GET /agents/sessions/.../attachments/{aid}/blob path.
"""

from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

from tests_lite._kernel_test_helpers import seed_managed_kernel_rows
from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.models.agents import SessionInput
from zerg.models.agents import SessionInputAttachment
from zerg.models.agents import SessionRuntimeState
from zerg.models.enums import UserRole
from zerg.models.models import Runner
from zerg.models.user import User
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.runner_connection_manager import get_runner_connection_manager
from zerg.services.session_continuity import session_lock_manager
from zerg.services.session_input_attachments import cleanup_stale_blobs
from zerg.services.session_input_attachments import store_attachment_blob
from zerg.services.session_inputs import INPUT_STATUS_DELIVERED
from zerg.services.session_inputs import INPUT_STATUS_FAILED
from zerg.services.session_inputs import create_session_input
from zerg.services.session_inputs import requeue_stuck_delivering
from zerg.services.session_runtime import phase_freshness_ms
from zerg.services.session_runtime import runtime_key_for_session

# A 1x1 PNG (~70 bytes) — enough to hash, well under the 2MB cap.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00"
    b"\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c"
    b"c\xfc\xcf\xc0P\x0f\x00\x05\x01\x01\x02\xb4\x9d\xb1\xa6\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def _make_db(tmp_path):
    db_path = tmp_path / "test_session_inputs_attachments.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
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
    state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == key).first()
    if state is None:
        state = SessionRuntimeState(
            runtime_key=key,
            session_id=session.id,
            provider=str(session.provider or "codex"),
            device_id=session.device_id,
        )
        db.add(state)
    state.phase = phase
    state.phase_source = "semantic"
    state.phase_started_at = now
    state.last_runtime_signal_at = now
    state.last_progress_at = now
    state.last_live_at = now
    state.timeline_anchor_at = now
    state.freshness_expires_at = now + timedelta(milliseconds=freshness_ms)
    state.terminal_state = None
    state.terminal_at = None
    state.runtime_version = int(getattr(state, "runtime_version", 0) or 0) + 1
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

        store = AgentsStore(db)
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


def _seed_claude_session(session_local):
    """Seed a Claude channel session — attach_images gate should reject."""
    session_id = uuid4()
    provider_session_id = f"claude-noattach-{uuid4().hex[:8]}"
    with session_local() as db:
        user = User(email=f"noattach-{uuid4().hex[:6]}@test.local", role=UserRole.USER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

        store = AgentsStore(db)
        started_at = datetime.now(timezone.utc)
        store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="Cinder",
                project="claude-attach",
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
        session.managed_transport = "claude_channel_bridge"
        session.source_runner_id = 1
        session.source_runner_name = "cinder"
        session.managed_session_name = "lh-noattach"
        seed_managed_kernel_rows(db, session, control_plane="claude_channel_bridge")
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
        _seed_live_runtime_state(db, session, phase="idle")
        user_id = user.id

    return session_id, user_id


def _stub_dispatch(monkeypatch):
    calls: list[dict] = []

    async def fake_send_text(
        *,
        db,
        owner_id,
        session,
        text,
        commis_id=None,
        timeout_secs=15,
        verify_turn_started=False,
        verification_timeout_secs=None,
        attachments=None,
    ):
        calls.append(
            {
                "session_id": str(session.id),
                "text": text,
                "commis_id": commis_id,
                "attachments": list(attachments or []),
            }
        )
        return SimpleNamespace(
            ok=True,
            exit_code=0,
            error=None,
            verified_turn_started=True,
            verified_user_event_id=None,
        )

    monkeypatch.setattr("zerg.services.live_session_dispatch.send_text_to_live_session", fake_send_text)
    monkeypatch.setattr("zerg.services.session_chat_impl._schedule_managed_local_lock_release", lambda **_: None)
    monkeypatch.setattr(
        "zerg.services.session_chat_impl._schedule_managed_local_active_phase_observation",
        lambda **_: None,
    )
    return calls


def _set_blob_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_ATTACHMENT_BLOB_ROOT", str(tmp_path / "blobs"))


def test_multipart_upload_succeeds_on_codex(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    calls = _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "look at this", "intent": "auto"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "sent"
        assert body["intent"] == "auto"
        assert len(calls) == 1
        assert calls[0]["text"] == "look at this"
        forwarded = calls[0]["attachments"]
        assert len(forwarded) == 1
        ref = forwarded[0]
        assert ref["mime_type"] == "image/png"
        assert len(ref["sha256"]) == 64
        assert ref["blob_url"].startswith(f"/api/agents/sessions/{session_id}/inputs/")
        assert ref["blob_url"].endswith("/blob")

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.session_id == session_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            attachments = (
                db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == row.id).all()
            )
            assert len(attachments) == 1
            attach = attachments[0]
            assert attach.mime_type == "image/png"
            assert attach.byte_size == len(_PNG_BYTES)
            assert len(attach.sha256) == 64
            assert ref["id"] == str(attach.id)
            assert ref["sha256"] == attach.sha256
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_multipart_rejects_non_codex_transport(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_claude_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "blocked", "intent": "auto"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert resp.status_code == 409, resp.text
        assert "codex" in resp.json()["detail"].lower()

        with session_local() as db:
            assert db.query(SessionInput).count() == 0
            assert db.query(SessionInputAttachment).count() == 0
    finally:
        api_app_ref.dependency_overrides = {}


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
            data={"text": "queue?", "intent": "queue"},
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
            data={"text": "bad type", "intent": "auto"},
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
    _stub_dispatch(monkeypatch)

    big = b"\x00" * (3 * 1024 * 1024)  # 3 MB > 2 MB cap

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        resp = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "huge", "intent": "auto"},
            files=[("attachments", ("big.png", io.BytesIO(big), "image/png"))],
        )
        assert resp.status_code == 400, resp.text
        assert "MB" in resp.json()["detail"] or "exceed" in resp.json()["detail"].lower()
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_machine_blob_fetch_streams_bytes(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )

    def override_verify():
        return SimpleNamespace(device_id="cinder", id="token-1", owner_id=user_id)

    def override_single():
        return None

    api_app_ref.dependency_overrides[verify_agents_token] = override_verify
    api_app_ref.dependency_overrides[require_single_tenant] = override_single

    try:
        upload = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "look", "intent": "auto"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert upload.status_code == 200, upload.text
        input_id = upload.json()["input_id"]

        with session_local() as db:
            attach = db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == input_id).one()
            attach_id = attach.id
            sha = attach.sha256

        resp = client.get(
            f"/api/agents/sessions/{session_id}/inputs/{input_id}/attachments/{attach_id}/blob",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == _PNG_BYTES
        assert resp.headers["X-Attachment-Sha256"] == sha
        assert resp.headers["X-Attachment-Bytes"] == str(len(_PNG_BYTES))
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_machine_blob_fetch_404_on_session_mismatch(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    api_app_ref.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(
        device_id="cinder", id="token-1", owner_id=user_id
    )
    api_app_ref.dependency_overrides[require_single_tenant] = lambda: None

    try:
        upload = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "look", "intent": "auto"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert upload.status_code == 200
        input_id = upload.json()["input_id"]

        with session_local() as db:
            attach = db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == input_id).one()
            attach_id = attach.id

        bogus_session = uuid4()
        resp = client.get(
            f"/api/agents/sessions/{bogus_session}/inputs/{input_id}/attachments/{attach_id}/blob",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 404, resp.text
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


def test_cleanup_stale_blobs_removes_terminal_aged_rows(monkeypatch, tmp_path):
    _set_blob_root(monkeypatch, tmp_path)
    session_local = _make_db(tmp_path)
    session_id, user_id = _seed_codex_session(session_local)
    _stub_dispatch(monkeypatch)

    client, api_app_ref = _make_client(
        session_local,
        SimpleNamespace(id=user_id, email="x@y", role=UserRole.USER.value),
    )
    try:
        upload = client.post(
            f"/api/sessions/{session_id}/inputs-multipart",
            data={"text": "look", "intent": "auto"},
            files=[("attachments", ("a.png", io.BytesIO(_PNG_BYTES), "image/png"))],
        )
        assert upload.status_code == 200, upload.text
        input_id = upload.json()["input_id"]

        with session_local() as db:
            row = db.query(SessionInput).filter(SessionInput.id == input_id).one()
            assert row.status == INPUT_STATUS_DELIVERED
            attach = db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == input_id).one()
            blob_path = tmp_path / "blobs" / attach.blob_path
            assert blob_path.exists()

            # Age the row beyond retention.
            attach.created_at = datetime.now(timezone.utc) - timedelta(days=2)
            db.add(attach)
            db.commit()

            removed = cleanup_stale_blobs(db)
            assert removed == 1
            assert not blob_path.exists()
            assert (
                db.query(SessionInputAttachment).filter(SessionInputAttachment.session_input_id == input_id).count()
                == 0
            )
    finally:
        asyncio.run(session_lock_manager.release(str(session_id)))
        api_app_ref.dependency_overrides = {}


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
