"""Archive-primary ingest and chunk coverage tests."""

from __future__ import annotations

import json
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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import SessionObservation
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.archive_primary import PreparedArchivePrimary
from zerg.services.archive_primary import build_source_line_archive_records
from zerg.services.archive_primary import source_lines_from_ingest
from zerg.services.archive_primary import write_ingest_archive
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.session_observations import OBS_KIND_PROVIDER_EVENT
from zerg.services.session_observations import OBS_KIND_PROVIDER_SOURCE_LINE
from zerg.services.session_observations import decode_observation_payload_json


def test_archive_writes_source_lines_and_manifest(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, tenant_id="tenant-a", target_bytes=128)
    data = _session_ingest(
        source_lines=[
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=15, raw_json='{"type":"assistant"}'),
            SourceLineIngest(source_path="/tmp/session.jsonl", source_offset=0, raw_json='{"type":"user"}'),
        ]
    )
    result = _ingest_result()

    with SessionLocal() as db:
        archive_result = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).order_by(ArchiveChunk.id).all()

    assert archive_result.records_written == 2
    assert archive_result.chunks_written >= 1
    assert len(rows) == archive_result.chunks_written
    assert all(row.tenant_id == "tenant-a" for row in rows)
    assert all(str(row.session_id) == str(result.session_id) for row in rows)

    records = []
    for row in rows:
        records.extend(archive_store.read_chunk(row.relative_path))
    assert [record.raw_bytes for record in records] == [b'{"type":"user"}', b'{"type":"assistant"}']
    assert all(record.tenant_id == "tenant-a" for record in records)
    assert all(record.session_id == str(result.session_id) for record in records)


def test_archive_manifest_insert_is_idempotent(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path)
    data = _session_ingest()
    result = _ingest_result()

    with SessionLocal() as db:
        first = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        second = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        assert first.error is None
        assert second.error is None
        assert second.records_written == 0
        assert second.chunks_written == 0
        assert db.query(ArchiveChunk).count() == first.chunks_written


def test_archive_skips_source_lines_already_present_in_sealed_chunks(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, target_bytes=4096)
    result = _ingest_result()

    with SessionLocal() as db:
        first = write_ingest_archive(
            db,
            data=_session_ingest(
                source_lines=[
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"type":"message","role":"user"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=15,
                        raw_json='{"type":"message","role":"assistant"}',
                    ),
                ]
            ),
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        second = write_ingest_archive(
            db,
            data=_session_ingest(
                source_lines=[
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                        raw_json='{"type":"message","role":"user"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=15,
                        raw_json='{"type":"message","role":"assistant"}',
                    ),
                    SourceLineIngest(
                        source_path="/tmp/session.jsonl",
                        source_offset=39,
                        raw_json='{"type":"message","role":"tool"}',
                    ),
                ]
            ),
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).order_by(ArchiveChunk.first_source_seq).all()

    assert first.error is None
    assert first.records_written == 2
    assert second.error is None
    assert second.records_written == 1
    assert len(rows) == first.chunks_written + second.chunks_written

    records = []
    for row in rows:
        records.extend(archive_store.read_chunk(row.relative_path))

    assert [(record.source_offset, record.raw_bytes) for record in records] == [
        (0, b'{"type":"message","role":"user"}'),
        (15, b'{"type":"message","role":"assistant"}'),
        (39, b'{"type":"message","role":"tool"}'),
    ]


def test_archive_source_sequences_do_not_collide_for_many_same_offset_records(tmp_path):
    result = _ingest_result()
    source_lines = [
        SourceLineIngest(
            source_path=f"/tmp/session-{index}.jsonl",
            source_offset=0,
            raw_json=f'{{"type":"message","index":{index}}}',
        )
        for index in range(2048)
    ]

    records = build_source_line_archive_records(
        data=_session_ingest(source_lines=source_lines),
        result=result,
        source_lines=source_lines,
        tenant_id="tenant-test",
    )
    source_seqs = [record.source_seq for record in records]

    assert len(source_seqs) == len(set(source_seqs))
    assert all(0 <= source_seq < (1 << 63) for source_seq in source_seqs)

    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    chunk = archive_store.write_chunk(records)

    assert chunk.record_count == len(source_lines)


def test_archive_falls_back_to_event_raw_json(tmp_path):
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


def test_archive_writes_event_stream_for_raw_events_without_source_path(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    settings = _archive_settings(tmp_path, target_bytes=4096)
    data = _session_ingest(
        source_lines=[],
        events=[
            EventIngest(
                role="system",
                content_text="server synthetic",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                source_path=None,
                source_offset=None,
                raw_json='{"type":"server","role":"system"}',
            )
        ],
    )
    result = _ingest_result()

    with SessionLocal() as db:
        archive_result = write_ingest_archive(
            db,
            data=data,
            result=result,
            settings=settings,
            archive_store=archive_store,
        )
        db.commit()

        rows = db.query(ArchiveChunk).all()

    assert archive_result.error is None
    assert archive_result.records_written == 1
    assert len(rows) == 1
    assert rows[0].stream == "events"

    records = archive_store.read_chunk(rows[0].relative_path)
    assert [record.raw_bytes for record in records] == [b'{"type":"server","role":"system"}']
    assert records[0].source_path is None
    assert records[0].source_offset is None


def test_ingest_route_writes_archive_without_legacy_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_ID", "tenant-primary")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(tmp_path / "primary-archive"))

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
                        "source_path": "/tmp/primary-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    }
                ],
                "events": [
                    {
                        "role": "user",
                        "content_text": "hello from archive primary",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "source_path": "/tmp/primary-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    },
                    {
                        "role": "system",
                        "content_text": "server synthetic event",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "raw_json": '{"type":"server","role":"system"}',
                    },
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 200, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "written"
        with SessionLocal() as db:
            chunks = db.query(ArchiveChunk).all()
            events = db.query(AgentEvent).order_by(AgentEvent.timestamp).all()
            source_lines = db.query(AgentSourceLine).all()
            source_observation = (
                db.query(SessionObservation).filter(SessionObservation.kind == OBS_KIND_PROVIDER_SOURCE_LINE).one()
            )
            event_observations = (
                db.query(SessionObservation)
                .filter(SessionObservation.kind == OBS_KIND_PROVIDER_EVENT)
                .order_by(SessionObservation.observed_at)
                .all()
            )

        assert {chunk.stream for chunk in chunks} == {"events", "source_lines"}
        assert all(chunk.tenant_id == "tenant-primary" for chunk in chunks)
        # The slim source_lines index row is always written (it drives export,
        # resume, and rewind), but carries NO raw payload when legacy raw writes
        # are disabled — raw bytes live only in the archive, fetched by line_hash.
        assert len(source_lines) == 1
        assert source_lines[0].line_hash
        assert source_lines[0].raw_json_z is None
        assert (source_lines[0].raw_json or "") == ""
        assert len(events) == 2
        assert events[0].content_text == "hello from archive primary"
        assert events[1].content_text == "server synthetic event"
        assert all(event.raw_json is None for event in events)
        assert all(event.raw_json_z is None for event in events)
        assert source_observation.payload_json == ""
        assert source_observation.payload_json_z is not None
        assert "raw_json" not in json.loads(decode_observation_payload_json(source_observation) or "{}")
        assert len(event_observations) == 2
        assert all(observation.payload_json == "" for observation in event_observations)
        assert all(observation.payload_json_z is not None for observation in event_observations)
        assert all(
            "raw_json" not in json.loads(decode_observation_payload_json(observation) or "{}")
            for observation in event_observations
        )

        archive_store = FilesystemArchiveStore(tmp_path / "primary-archive")
        records_by_stream: dict[str, list[bytes]] = {}
        for chunk in chunks:
            records_by_stream.setdefault(chunk.stream, [])
            records_by_stream[chunk.stream].extend(
                record.raw_bytes for record in archive_store.read_chunk(chunk.relative_path)
            )
        assert records_by_stream["source_lines"] == [b'{"type":"message","role":"user"}']
        assert sorted(records_by_stream["events"]) == sorted(
            [
                b'{"type":"message","role":"user"}',
                b'{"type":"server","role":"system"}',
            ]
        )
    finally:
        api_app.dependency_overrides.clear()


def test_ingest_route_fails_when_archive_write_fails(tmp_path, monkeypatch):
    bad_archive_root = tmp_path / "not-a-directory"
    bad_archive_root.write_text("not a directory")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(bad_archive_root))

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
                        "source_path": "/tmp/fallback-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    }
                ],
                "events": [
                    {
                        "role": "user",
                        "content_text": "fallback raw event",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "source_path": "/tmp/fallback-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    }
                ],
            },
            headers={"X-Agents-Token": "dev"},
        )

        assert response.status_code == 503, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "failed"
        with SessionLocal() as db:
            assert db.query(ArchiveChunk).count() == 0
            assert db.query(AgentSourceLine).count() == 0
            assert db.query(AgentEvent).count() == 0
    finally:
        api_app.dependency_overrides.clear()


def test_live_ingest_archive_failure_is_retryable(tmp_path, monkeypatch):
    bad_archive_root = tmp_path / "not-a-directory"
    bad_archive_root.write_text("not a directory")
    monkeypatch.setenv("LONGHOUSE_ARCHIVE_ROOT", str(bad_archive_root))

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
                        "source_path": "/tmp/fail-closed-session.jsonl",
                        "source_offset": 0,
                        "raw_json": '{"type":"message","role":"user"}',
                    }
                ],
            },
            headers={
                "X-Agents-Token": "dev",
                "X-Longhouse-Ship-Trace": json.dumps(
                    {
                        "schema": "ship_trace.v1",
                        "trace_id": f"{session_id}:0:8192:1778220000000",
                        "provider": "codex",
                        "session_id": str(session_id),
                        "work_context": "live_transcript",
                    },
                    separators=(",", ":"),
                ),
            },
        )

        assert response.status_code == 503, response.text
        assert response.headers["X-Ingest-Archive-Primary"] == "failed"
        with SessionLocal() as db:
            assert db.query(ArchiveChunk).count() == 0
            assert db.query(AgentSourceLine).count() == 0
            assert db.query(AgentEvent).count() == 0
    finally:
        api_app.dependency_overrides.clear()


def test_ingest_route_prepares_archive_before_main_writer(tmp_path, monkeypatch):
    inside_writer = False
    observations: dict[str, bool] = {}

    class OrderingSerializer:
        is_configured = True
        writer_active = False
        active_label = None
        active_age_ms = 0.0
        queue_depth = 0

        async def execute_after_closing_request_session(self, fn, fallback_db, **_kwargs):
            nonlocal inside_writer
            inside_writer = True
            try:
                result = fn(fallback_db)
                fallback_db.commit()
                return result
            finally:
                inside_writer = False

        async def execute_or_direct(self, *_args, **_kwargs):  # pragma: no cover - regression guard
            raise AssertionError("empty archive prepare should not enqueue manifest writes")

    def fake_prepare_ingest_archive(**_kwargs):
        observations["prepare_inside_writer"] = inside_writer
        return PreparedArchivePrimary()

    client, _ = _make_client(tmp_path)
    monkeypatch.setattr(
        "zerg.services.write_serializer.get_write_serializer",
        lambda: OrderingSerializer(),
    )
    monkeypatch.setattr(
        "zerg.services.archive_primary.prepare_ingest_archive",
        fake_prepare_ingest_archive,
    )
    try:
        response = client.post(
            "/agents/ingest",
            json={
                "id": "41111111-2222-3333-4444-555555555555",
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
        assert observations == {"prepare_inside_writer": False}
    finally:
        api_app.dependency_overrides.clear()


def _session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-primary.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-primary-route.db'}")
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


def _archive_settings(tmp_path, *, tenant_id: str = "tenant-test", target_bytes: int = 4096):
    return SimpleNamespace(
        archive_primary_tenant_id=tenant_id,
        archive_primary_chunk_target_bytes=target_bytes,
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
