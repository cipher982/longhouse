"""Tests for guarded media backfill from legacy inline data URLs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace

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
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import MediaObject
from zerg.models.agents import SessionMediaRef
from zerg.services.ingest_health import compute_ingest_health_from_catalog_facts


def _setup_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test_agents_media_backfill.db"
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


def _create_session_and_source_line(factory, payload: bytes, *, encoded: str | None = None):
    encoded = encoded or base64.b64encode(payload).decode("ascii")
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{encoded}",
                    }
                ]
            },
        }
    )

    with factory() as db:
        session = AgentSession(
            provider="codex",
            environment="test",
            started_at=datetime.now(timezone.utc),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        line = AgentSourceLine(
            session_id=session.id,
            source_path="/tmp/legacy-codex.jsonl",
            source_offset=77,
            branch_id=0,
            raw_json=raw,
            raw_json_z=None,
            raw_json_codec=0,
            line_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )
        db.add(line)
        db.commit()
        return session.id, line.id


def test_inline_media_backfill_dry_run_does_not_write(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nlegacy-inline"
    _create_session_and_source_line(factory, payload)

    try:
        response = client.post("/agents/media/backfill-inline-data-urls?dry_run=true&max_rows=10&max_bytes=1024")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is True
        assert body["scanned_source_lines"] == 1
        assert body["candidate_refs"] == 1
        assert body["decoded_bytes"] == len(payload)
        assert body["stored_objects"] == 0
        assert body["refs_upserted"] == 0

        with factory() as db:
            assert db.query(MediaObject).count() == 0
            assert db.query(SessionMediaRef).count() == 0
    finally:
        cleanup()


def test_inline_media_backfill_requires_gate_then_writes_object_and_ref(tmp_path, monkeypatch):
    factory, blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nlegacy-write"
    digest = hashlib.sha256(payload).hexdigest()
    session_id, line_id = _create_session_and_source_line(factory, payload)

    try:
        denied = client.post("/agents/media/backfill-inline-data-urls?dry_run=false")
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == "confirmed_backup_gate is required when dry_run=false"

        response = client.post(
            "/agents/media/backfill-inline-data-urls"
            "?dry_run=false&confirmed_backup_gate=true&disk_floor_bytes=0&max_rows=10&max_bytes=1024"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dry_run"] is False
        assert body["last_source_line_id"] == line_id
        assert body["candidate_refs"] == 1
        assert body["decoded_bytes"] == len(payload)
        assert body["stored_objects"] == 1
        assert body["refs_upserted"] == 1

        with factory() as db:
            row = db.query(MediaObject).filter(MediaObject.sha256 == digest).one()
            assert row.mime_type == "image/png"
            assert row.byte_size == len(payload)
            assert row.first_seen_session_id == session_id
            assert (blob_root / row.storage_path).read_bytes() == payload
            ref = db.query(SessionMediaRef).filter(SessionMediaRef.media_sha256 == digest).one()
            assert ref.session_id == session_id
            assert ref.source_path == "/tmp/legacy-codex.jsonl"
            assert ref.source_offset == 77
            assert ref.json_pointer == "/payload/content/0/image_url"
            assert ref.original_kind == "data_url_backfill"
            assert ref.media_state == "present"
    finally:
        cleanup()


def test_inline_media_backfill_accepts_line_wrapped_base64(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)
    payload = b"\x89PNG\r\nlegacy-wrapped"
    encoded = base64.b64encode(payload).decode("ascii")
    wrapped = f"{encoded[:8]}\n{encoded[8:]}"
    _create_session_and_source_line(factory, payload, encoded=wrapped)

    try:
        response = client.post(
            "/agents/media/backfill-inline-data-urls"
            "?dry_run=false&confirmed_backup_gate=true&disk_floor_bytes=0&max_rows=10&max_bytes=1024"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["candidate_refs"] == 1
        assert body["decoded_bytes"] == len(payload)
        assert body["rejected"] == 0
    finally:
        cleanup()


def test_ingest_health_reports_media_repair_debt_separately(tmp_path, monkeypatch):
    factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    client = TestClient(api_app)

    try:
        with factory() as db:
            session = AgentSession(
                provider="codex",
                environment="test",
                started_at=datetime.now(timezone.utc),
            )
            db.add(session)
            db.commit()
            db.refresh(session)
            db.add(
                SessionMediaRef(
                    session_id=session.id,
                    source_path="/tmp/pending.jsonl",
                    source_offset=1,
                    original_kind="inline_data_url",
                    media_sha256=hashlib.sha256(b"pending").hexdigest(),
                    media_state="pending",
                )
            )
            db.commit()

        response = client.get("/agents/ingest-health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["media_repair_refs"] == 1
        assert body["media_repair_bytes"] == 0
    finally:
        cleanup()


def test_catalog_ingest_health_route_does_not_read_legacy_tables(tmp_path, monkeypatch):
    _factory, _blob_root, cleanup = _setup_app(tmp_path, monkeypatch)
    from zerg.routers import agents_backfill as route_module

    class Catalog:
        async def call(self, method, params):
            assert method == "storage.health.v2"
            assert params == {"owner_id": "42"}
            return {
                "session_count": 17_901,
                "last_session_at": datetime.now(timezone.utc).isoformat(),
                "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "media_repair_refs": 10,
                "media_repair_bytes": 0,
            }

    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(owner_id=42)
    monkeypatch.setattr(route_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(route_module, "get_catalogd_client", lambda: Catalog())
    client = TestClient(api_app)
    try:
        response = client.get("/agents/ingest-health")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"
        assert response.json()["session_count"] == 17_901
        assert response.json()["media_repair_refs"] == 10
    finally:
        cleanup()


def test_catalog_ingest_health_distinguishes_online_stale_from_offline():
    now = datetime.now(timezone.utc)
    facts = {
        "session_count": 1,
        "last_session_at": (now - timedelta(hours=8)).isoformat(),
        "last_heartbeat_at": (now - timedelta(minutes=1)).isoformat(),
    }
    assert compute_ingest_health_from_catalog_facts(facts, now=now)["status"] == "stale"
    facts["last_heartbeat_at"] = (now - timedelta(hours=1)).isoformat()
    assert compute_ingest_health_from_catalog_facts(facts, now=now)["status"] == "device_offline"
