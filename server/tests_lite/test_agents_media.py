"""Tests for the agents archive media API."""

from __future__ import annotations

import hashlib
import os

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import MediaObject


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

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)

    return factory, blob_root, _cleanup


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


def test_media_upload_rejects_hash_mismatch(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    wrong_digest = hashlib.sha256(b"expected").hexdigest()

    try:
        response = client.put(f"/agents/media/{wrong_digest}", content=b"actual", headers={"Content-Type": "image/png"})
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "sha256 mismatch"
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
