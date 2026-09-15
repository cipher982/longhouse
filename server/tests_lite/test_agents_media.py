"""Tests for the agents archive media API."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.main import api_app
from zerg.models.agents import AgentSession
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.models.device_token import DeviceToken
from zerg.models.user import User
from zerg.routers import agents_media
from zerg.routers import agents_storage_v2


def _setup_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test_agents_media.db"
    blob_root = tmp_path / "media"
    monkeypatch.setenv("LONGHOUSE_MEDIA_BLOB_ROOT", str(blob_root))
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=1)
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: SimpleNamespace(id=1)
    monkeypatch.setattr(
        agents_media,
        "session_batch_snapshot",
        lambda session_ids, *, owner_id: {
            "facts": [{"catalog": {"session_id": session_id}} for session_id in session_ids] if owner_id == 1 else []
        },
    )

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)
        api_app.dependency_overrides.pop(get_current_browser_route_user, None)

    return factory, blob_root, _cleanup


def _create_session(db, session_id, *, device_id=None):
    db.add(
        AgentSession(
            id=session_id,
            provider="codex",
            environment="test",
            device_id=device_id,
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def test_media_claim_upload_claim_and_fetch(tmp_path, monkeypatch):
    factory, blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nlonghouse-media"
    digest = hashlib.sha256(payload).hexdigest()
    session_id = uuid4()

    try:
        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                    }
                ]
            },
        )
        assert claim.status_code == 200, claim.text
        assert claim.json() == {"needed": [digest], "present": [], "rejected": []}

        uploaded = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json() == {
            "sha256": digest,
            "mime_type": "image/png",
            "byte_size": len(payload),
            "created": True,
            "blob_url": f"/api/agents/media/{digest}/blob",
        }

        with factory() as db:
            row = db.query(MediaObject).filter(MediaObject.sha256 == digest).first()
            assert row is not None
            stored_path = blob_root / row.storage_path
            assert stored_path.read_bytes() == payload

        duplicate = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "image/png"})
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["created"] is False

        claim_after = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                    }
                ]
            },
        )
        assert claim_after.status_code == 200, claim_after.text
        assert claim_after.json() == {"needed": [], "present": [digest], "rejected": []}

        head = client.head(f"/agents/media/{digest}")
        assert head.status_code == 200, head.text
        assert head.headers["x-media-sha256"] == digest
        assert head.headers["content-length"] == str(len(payload))

        fetched = client.get(f"/agents/media/{digest}/blob")
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == payload
        assert fetched.headers["content-type"].startswith("image/png")
        assert fetched.headers["x-media-sha256"] == digest
    finally:
        cleanup()


def test_media_claim_does_not_turn_hash_knowledge_into_owner_access(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    owner_one_session = uuid4()
    owner_two_session = uuid4()
    payload = b"\x89PNG\r\nprivate-owner-media"
    digest = hashlib.sha256(payload).hexdigest()

    def _snapshot(session_ids, *, owner_id):
        allowed = owner_one_session if owner_id == 1 else owner_two_session if owner_id == 2 else None
        return {"facts": [{"catalog": {"session_id": session_id}} for session_id in session_ids if session_id == str(allowed)]}

    monkeypatch.setattr(agents_media, "session_batch_snapshot", _snapshot)
    try:
        first_claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(owner_one_session),
                    }
                ]
            },
        )
        assert first_claim.json()["needed"] == [digest]
        assert (
            client.put(
                f"/agents/media/{digest}",
                content=payload,
                headers={"Content-Type": "image/png", "X-Longhouse-Session-Id": str(owner_one_session)},
            ).status_code
            == 200
        )

        api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=2)
        second_claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(owner_two_session),
                    }
                ]
            },
        )
        assert second_claim.status_code == 200
        assert second_claim.json() == {"needed": [digest], "present": [], "rejected": []}
        assert client.get(f"/agents/media/{digest}/blob").status_code == 404
    finally:
        cleanup()


def test_media_claim_registers_ref_and_upload_marks_present(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    session_id = uuid4()
    payload = b"\x89PNG\r\nsource-ref"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                        "source_path": "/tmp/opencode.jsonl",
                        "source_offset": 42,
                        "source_line_hash": hashlib.sha256(b"source-line").hexdigest(),
                        "json_pointer": "/parts/0/content",
                        "provider": "opencode",
                        "original_kind": "inline_data_url",
                    }
                ]
            },
        )
        assert claim.status_code == 200, claim.text
        assert claim.json() == {"needed": [digest], "present": [], "rejected": []}

        with factory() as db:
            ref = db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).first()
            assert ref is not None
            assert ref.session_id == session_id
            assert ref.media_state == "pending"
            assert ref.source_offset == 42

        uploaded = client.put(
            f"/agents/media/{digest}",
            content=payload,
            headers={"Content-Type": "image/png", "X-Longhouse-Session-Id": str(session_id)},
        )
        assert uploaded.status_code == 200, uploaded.text

        with factory() as db:
            row = db.query(MediaObject).filter(MediaObject.sha256 == digest).first()
            assert row is not None
            assert row.first_seen_session_id == session_id
            ref = db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).first()
            assert ref is not None
            assert ref.media_state == "present"
    finally:
        cleanup()


def test_media_claim_reuses_same_source_ref_when_pointer_changes(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    session_id = uuid4()
    digest = hashlib.sha256(b"same-source").hexdigest()

    def _claim(line_hash: str, pointer: str):
        return client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": 11,
                        "session_id": str(session_id),
                        "source_path": "/tmp/provider.jsonl",
                        "source_offset": 7,
                        "source_line_hash": line_hash,
                        "json_pointer": pointer,
                    }
                ]
            },
        )

    try:
        first_hash = hashlib.sha256(b"first").hexdigest()
        second_hash = hashlib.sha256(b"second").hexdigest()
        first = _claim(first_hash, "/old")
        assert first.status_code == 200, first.text

        second = _claim(second_hash, "/new")
        assert second.status_code == 200, second.text

        with factory() as db:
            refs = db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).all()
            assert len(refs) == 1
            assert refs[0].source_line_hash == second_hash
            assert refs[0].json_pointer == "/new"
    finally:
        cleanup()


def test_media_upload_rejects_hash_mismatch(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    wrong_digest = hashlib.sha256(b"expected").hexdigest()

    try:
        response = client.put(f"/agents/media/{wrong_digest}", content=b"actual", headers={"Content-Type": "image/png"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "sha256 mismatch"
    finally:
        cleanup()


class _BrowserMediaCatalog:
    """A catalog that answers the media manifest read the browser route needs."""

    def __init__(self, result):
        self._result = result

    async def call(self, method, params, *, timeout_seconds=None):
        assert method == "storage.media.read.v2"
        return self._result


def _browser_media_catalog(monkeypatch, result):
    monkeypatch.setattr(agents_storage_v2, "get_catalogd_client", lambda: _BrowserMediaCatalog(result))


def test_browser_media_read_denies_a_hash_the_store_does_not_authorize(tmp_path, monkeypatch):
    """A hash the store will not authorize is not-found, never a partial answer."""

    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    digest = hashlib.sha256(b"not-visible-to-this-owner").hexdigest()
    _browser_media_catalog(monkeypatch, {"found": False})

    try:
        denied = client.get(f"/media/{digest}/blob")
        assert denied.status_code == 404, denied.text
        assert denied.json()["detail"]["code"] == "media_not_found"
    finally:
        cleanup()


def test_browser_media_read_streams_the_verified_storage_v2_object(tmp_path, monkeypatch):
    """The browser reads the same store the engine uploaded to, bytes intact."""

    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nvisible-browser-media"
    digest = "a" * 64
    _browser_media_catalog(
        monkeypatch,
        {
            "found": True,
            "media": {
                "media_hash": digest,
                "state": "present",
                "mime_type": "image/png",
                "byte_size": len(payload),
                "object_path": f"media/v2/sha256/aa/aa/{digest}.bin",
            },
        },
    )

    class _Pool:
        async def read_media(self, object_path, media_hash):
            assert media_hash == digest
            return SimpleNamespace(data=payload)

    monkeypatch.setattr(agents_storage_v2, "get_raw_object_worker_pool", lambda: _Pool())

    try:
        allowed = client.get(f"/media/{digest}/blob")
        assert allowed.status_code == 200, allowed.text
        assert allowed.content == payload
        assert allowed.headers["content-type"].startswith("image/png")
        assert allowed.headers["x-media-sha256"] == digest

        head = client.head(f"/media/{digest}")
        assert head.status_code == 200, head.text
        assert head.headers["content-length"] == str(len(payload))
    finally:
        cleanup()


# Event media-ref projection is covered by the storage-v2 workspace tests
# (`tests_lite/test_storage_v2_workspace.py`), where refs ride the session read
# and are placed on the event that owns their source line. The legacy
# claim/ref-by-event-id read path this file once exercised is gone.


def test_media_upload_rejects_empty_and_unsupported_mime(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    empty_digest = hashlib.sha256(b"").hexdigest()
    payload = b"plain text"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        empty = client.put(f"/agents/media/{empty_digest}", content=b"", headers={"Content-Type": "image/png"})
        assert empty.status_code == 400, empty.text
        assert empty.json()["detail"] == "empty media"

        unsupported = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "text/plain"})
        assert unsupported.status_code == 400, unsupported.text
        assert unsupported.json()["detail"] == "unsupported mime: text/plain"
    finally:
        cleanup()


def test_media_claim_rejects_bad_sha_and_unsupported_mime(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    digest = hashlib.sha256(b"jpeg").hexdigest()

    try:
        response = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {"sha256": "not-a-sha", "mime_type": "image/png", "byte_size": 10},
                    {"sha256": digest, "mime_type": "text/plain", "byte_size": 4},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "needed": [],
            "present": [],
            "rejected": [
                {"sha256": "not-a-sha", "reason": "invalid_sha256"},
                {"sha256": digest, "reason": "unsupported_mime_type"},
            ],
        }
    finally:
        cleanup()


def test_media_claim_rejects_bad_size_and_too_many_items(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    digest = hashlib.sha256(b"image").hexdigest()

    try:
        bad_size = client.post(
            "/agents/media/claims",
            json={"items": [{"sha256": digest, "mime_type": "image/png", "byte_size": 0}]},
        )
        assert bad_size.status_code == 200, bad_size.text
        assert bad_size.json() == {
            "needed": [],
            "present": [],
            "rejected": [{"sha256": digest, "reason": "unsupported_byte_size"}],
        }

        too_many = client.post(
            "/agents/media/claims",
            json={"items": [{"sha256": digest, "mime_type": "image/png", "byte_size": 5}] * 513},
        )
        assert too_many.status_code == 400, too_many.text
        assert too_many.json()["detail"] == "too many media claim items"
    finally:
        cleanup()
