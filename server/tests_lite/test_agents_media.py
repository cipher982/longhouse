"""Tests for the agents archive media API."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef


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
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_route_user] = lambda: object()

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)
        api_app.dependency_overrides.pop(get_current_browser_route_user, None)

    return factory, blob_root, _cleanup


def _create_session(db, session_id):
    db.add(
        AgentSession(
            id=session_id,
            provider="codex",
            environment="test",
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def test_media_claim_upload_claim_and_fetch(tmp_path, monkeypatch):
    factory, blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nlonghouse-media"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        claim = client.post(
            "/agents/media/claims",
            json={"items": [{"sha256": digest, "mime_type": "image/png", "byte_size": len(payload)}]},
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
            json={"items": [{"sha256": digest, "mime_type": "image/png", "byte_size": len(payload)}]},
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


def test_browser_media_read_requires_session_ref(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nvisible-browser-media"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        uploaded = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 200, uploaded.text

        denied = client.get(f"/media/{digest}/blob")
        assert denied.status_code == 404, denied.text

        session_id = uuid4()
        with factory() as db:
            _create_session(db, session_id)

        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                        "source_path": "/tmp/codex.jsonl",
                        "source_offset": 9,
                    }
                ]
            },
        )
        assert claim.status_code == 200, claim.text

        head = client.head(f"/media/{digest}")
        assert head.status_code == 200, head.text
        assert head.headers["content-length"] == str(len(payload))
        assert head.headers["x-media-sha256"] == digest

        fetched = client.get(f"/media/{digest}/blob")
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == payload
        assert fetched.headers["content-type"].startswith("image/png")
        assert fetched.headers["x-media-sha256"] == digest
    finally:
        cleanup()


def test_browser_media_thumbnail_streams_authorized_derivative(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    session_id = uuid4()
    payload = b"\x89PNG\r\noriginal-media"
    digest = hashlib.sha256(payload).hexdigest()
    thumb_payload = b"RIFFwebp-thumbnail"
    thumb_digest = hashlib.sha256(thumb_payload).hexdigest()

    try:
        with factory() as db:
            _create_session(db, session_id)

        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                        "source_path": "/tmp/codex.jsonl",
                        "source_offset": 11,
                    }
                ]
            },
        )
        assert claim.status_code == 200, claim.text

        uploaded = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 200, uploaded.text

        missing_thumb = client.get(f"/media/{digest}/thumb")
        assert missing_thumb.status_code == 404, missing_thumb.text
        assert missing_thumb.json()["detail"] == "media thumbnail not found"

        uploaded_thumb = client.put(
            f"/agents/media/{thumb_digest}",
            content=thumb_payload,
            headers={"Content-Type": "image/webp"},
        )
        assert uploaded_thumb.status_code == 200, uploaded_thumb.text

        with factory() as db:
            row = db.query(MediaObject).filter(MediaObject.sha256 == digest).first()
            assert row is not None
            row.thumbnail_sha256 = thumb_digest
            db.commit()

        fetched = client.get(f"/media/{digest}/thumb")
        assert fetched.status_code == 200, fetched.text
        assert fetched.content == thumb_payload
        assert fetched.headers["content-type"].startswith("image/webp")
        assert fetched.headers["x-media-sha256"] == thumb_digest
    finally:
        cleanup()


def test_session_events_project_media_refs_from_source_coordinates(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    session_id = uuid4()
    source_path = "/tmp/codex/session.jsonl"
    source_offset = 123
    payload = b"\x89PNG\r\nredacted-event-media"
    digest = hashlib.sha256(payload).hexdigest()

    try:
        with factory() as db:
            _create_session(db, session_id)

        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": digest,
                        "mime_type": "image/png",
                        "byte_size": len(payload),
                        "session_id": str(session_id),
                        "source_path": source_path,
                        "source_offset": source_offset,
                        "source_line_hash": hashlib.sha256(b"source-line").hexdigest(),
                        "json_pointer": "/message/content/0/image_url",
                        "provider": "codex",
                        "original_kind": "inline_data_url",
                    }
                ]
            },
        )
        assert claim.status_code == 200, claim.text

        uploaded = client.put(f"/agents/media/{digest}", content=payload, headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 200, uploaded.text

        with factory() as db:
            event = AgentEvent(
                session_id=session_id,
                role="user",
                content_text="[media redacted: image/png sha256=%s]" % digest,
                timestamp=datetime.now(timezone.utc),
                source_path=source_path,
                source_offset=source_offset,
                event_hash=hashlib.sha256(b"event").hexdigest(),
            )
            db.add(event)
            db.commit()

        response = client.get(f"/agents/sessions/{session_id}/events")
        assert response.status_code == 200, response.text
        events = response.json()["events"]
        assert len(events) == 1

        refs = events[0]["media_refs"]
        assert refs == [
            {
                "sha256": digest,
                "media_state": "present",
                "mime_type": "image/png",
                "byte_size": len(payload),
                "blob_url": f"/api/media/{digest}/blob",
                "thumb_url": None,
                "source_path": source_path,
                "source_offset": source_offset,
                "json_pointer": "/message/content/0/image_url",
                "original_kind": "inline_data_url",
            }
        ]
    finally:
        cleanup()


def test_events_and_projection_project_event_id_media_refs(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    session_id = uuid4()
    pending_digest = hashlib.sha256(b"pending-media").hexdigest()
    present_payload = b"\x89PNG\r\npresent-event-media"
    present_digest = hashlib.sha256(present_payload).hexdigest()
    base = datetime.now(timezone.utc)

    try:
        with factory() as db:
            _create_session(db, session_id)
            first = AgentEvent(
                session_id=session_id,
                role="user",
                content_text="[pending media redacted]",
                timestamp=base,
                source_path="/tmp/events-only.jsonl",
                source_offset=1,
                event_hash=hashlib.sha256(b"pending-event").hexdigest(),
            )
            second = AgentEvent(
                session_id=session_id,
                role="user",
                content_text="[present media redacted]",
                timestamp=base + timedelta(milliseconds=1),
                source_path="/tmp/events-only.jsonl",
                source_offset=2,
                event_hash=hashlib.sha256(b"present-event").hexdigest(),
            )
            db.add_all([first, second])
            db.commit()
            first_id = first.id
            second_id = second.id

        claim = client.post(
            "/agents/media/claims",
            json={
                "items": [
                    {
                        "sha256": pending_digest,
                        "mime_type": "image/png",
                        "byte_size": 13,
                        "session_id": str(session_id),
                        "event_id": first_id,
                        "source_path": "/tmp/ref-only.jsonl",
                        "source_offset": 501,
                        "json_pointer": "/pending",
                        "provider": "codex",
                        "original_kind": "inline_data_url",
                    },
                    {
                        "sha256": present_digest,
                        "mime_type": "image/png",
                        "byte_size": len(present_payload),
                        "session_id": str(session_id),
                        "event_id": second_id,
                        "source_path": "/tmp/ref-only.jsonl",
                        "source_offset": 502,
                        "json_pointer": "/present",
                        "provider": "codex",
                        "original_kind": "inline_data_url",
                    },
                ]
            },
        )
        assert claim.status_code == 200, claim.text
        assert claim.json()["needed"] == [pending_digest, present_digest]

        uploaded = client.put(
            f"/agents/media/{present_digest}",
            content=present_payload,
            headers={"Content-Type": "image/png"},
        )
        assert uploaded.status_code == 200, uploaded.text

        events_response = client.get(f"/agents/sessions/{session_id}/events")
        assert events_response.status_code == 200, events_response.text
        events_by_id = {item["id"]: item for item in events_response.json()["events"]}
        assert set(events_by_id) == {first_id, second_id}

        pending_ref = events_by_id[first_id]["media_refs"][0]
        assert pending_ref["sha256"] == pending_digest
        assert pending_ref["media_state"] == "pending"
        assert pending_ref["mime_type"] is None
        assert pending_ref["byte_size"] is None
        assert pending_ref["blob_url"] == f"/api/media/{pending_digest}/blob"
        assert pending_ref["source_path"] == "/tmp/ref-only.jsonl"
        assert pending_ref["source_offset"] == 501

        present_ref = events_by_id[second_id]["media_refs"][0]
        assert present_ref["sha256"] == present_digest
        assert present_ref["media_state"] == "present"
        assert present_ref["mime_type"] == "image/png"
        assert present_ref["byte_size"] == len(present_payload)
        assert present_ref["blob_url"] == f"/api/media/{present_digest}/blob"
        assert present_ref["source_path"] == "/tmp/ref-only.jsonl"
        assert present_ref["source_offset"] == 502

        projection_response = client.get(f"/agents/sessions/{session_id}/projection?limit=50")
        assert projection_response.status_code == 200, projection_response.text
        projection_events = {
            item["event"]["id"]: item["event"]
            for item in projection_response.json()["items"]
            if item["kind"] == "event"
        }
        assert projection_events[first_id]["media_refs"] == [pending_ref]
        assert projection_events[second_id]["media_refs"] == [present_ref]
    finally:
        cleanup()


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
