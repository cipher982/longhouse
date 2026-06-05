from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import ArchiveChunk
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.archive_shadow import source_lines_from_ingest
from zerg.services.archive_shadow import write_ingest_shadow_archive
from zerg.services.archive_store import FilesystemArchiveStore


def test_shadow_archive_disabled_does_not_write(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    settings = SimpleNamespace(archive_shadow_write_enabled=False)
    data = _session_ingest()
    result = _ingest_result()

    with SessionLocal() as db:
        shadow = write_ingest_shadow_archive(db, data=data, result=result, settings=settings)
        db.commit()

        assert shadow.enabled is False
        assert db.query(ArchiveChunk).count() == 0
        assert not (tmp_path / "archive").exists()


def test_shadow_archive_writes_source_lines_and_manifest(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _shadow_settings(tmp_path, tenant_id="tenant-a", target_bytes=128)
    data = _session_ingest(
        source_lines=[
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=15, raw_json='{"type":"assistant"}'),
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=0, raw_json='{"type":"user"}'),
        ]
    )
    result = _ingest_result()

    with SessionLocal() as db:
        shadow = write_ingest_shadow_archive(db, data=data, result=result, settings=settings, archive_store=archive_store)
        db.commit()

        rows = db.query(ArchiveChunk).order_by(ArchiveChunk.id).all()

    assert shadow.enabled is True
    assert shadow.records_written == 2
    assert shadow.chunks_written >= 1
    assert len(rows) == shadow.chunks_written
    assert all(row.tenant_id == "tenant-a" for row in rows)
    assert all(str(row.session_id) == str(result.session_id) for row in rows)

    records = []
    for row in rows:
        records.extend(archive_store.read_chunk(row.relative_path))
    assert [record.raw_bytes for record in records] == [b'{"type":"user"}', b'{"type":"assistant"}']
    assert all(record.tenant_id == "tenant-a" for record in records)
    assert all(record.session_id == str(result.session_id) for record in records)


def test_shadow_archive_manifest_insert_is_idempotent(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _shadow_settings(tmp_path)
    data = _session_ingest()
    result = _ingest_result()

    with SessionLocal() as db:
        first = write_ingest_shadow_archive(db, data=data, result=result, settings=settings, archive_store=archive_store)
        second = write_ingest_shadow_archive(db, data=data, result=result, settings=settings, archive_store=archive_store)
        db.commit()

        assert first.error is None
        assert second.error is None
        assert db.query(ArchiveChunk).count() == first.chunks_written


def test_shadow_archive_falls_back_to_event_raw_json(tmp_path):
    data = _session_ingest(
        source_lines=[],
        events=[
            EventIngest(
                role="user",
                content_text="hello",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_path="/tmp/session.jsonl",
                source_offset=42,
                raw_json='{"type":"message","role":"user"}',
            )
        ],
    )

    lines = source_lines_from_ingest(data)

    assert len(lines) == 1
    assert lines[0].source_path == "/tmp/session.jsonl"
    assert lines[0].source_offset == 42
    assert lines[0].raw_json == '{"type":"message","role":"user"}'


def test_ingest_route_shadow_writes_archive_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_WRITE_ENABLED", "1")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_TENANT_ID", "tenant-route")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(tmp_path / "route-archive"))
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_SHADOW_CHUNK_TARGET_BYTES", "128")

    client, SessionLocal = _make_client(tmp_path)
    session_id = uuid4()
    try:
        response = client.post(
            "/agents/ingest",
            json={
                "id": str(session_id),
                "provider": "codex",
                "environment": "test",
                "project": "longhouse",
                "device_id": "route-device",
                "started_at": "2026-01-01T00:00:00Z",
                "source_lines": [
                    {
                        "source_path": "/tmp/route-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200
        with SessionLocal() as db:
            rows = db.query(ArchiveChunk).all()

        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-route"
        assert str(rows[0].session_id) == str(session_id)

        archive_store = FilesystemArchiveStore(tmp_path / "route-archive")
        records = archive_store.read_chunk(rows[0].relative_path)
        assert [record.raw_bytes for record in records] == [b'{"type":"message","role":"user"}']
    finally:
        api_app.dependency_overrides.clear()


def _session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-shadow.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-shadow-route.db'}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="route-device", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app), factory


def _shadow_settings(tmp_path, *, tenant_id: str = "tenant-test", target_bytes: int = 4096):
    return SimpleNamespace(
        archive_shadow_write_enabled=True,
        archive_shadow_tenant_id=tenant_id,
        archive_shadow_chunk_target_bytes=target_bytes,
        archive_root=str(tmp_path / "archive"),
    )


def _session_ingest(*, source_lines=None, events=None) -> SessionIngest:
    return SessionIngest(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/tmp",
        git_repo=None,
        git_branch=None,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_lines=source_lines
        if source_lines is not None
        else [
            SourceLineIngest(
                source_path="/tmp/session.jsonl",
                source_offset=0,
                raw_json='{"type":"message","role":"user"}',
            )
        ],
        events=events or [],
    )


def _ingest_result() -> IngestResult:
    return IngestResult(
        session_id=uuid4(),
        events_inserted=0,
        events_skipped=0,
        session_created=True,
        source_lines_inserted=1,
    )
