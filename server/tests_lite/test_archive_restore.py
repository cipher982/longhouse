from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from zerg.data_plane import initialize_derived_database
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import TimelineCard
from zerg.services.archive_derived_projector import project_archive_chunks_to_derived_events
from zerg.services.archive_hot_projector import project_archive_chunks_to_hot_cards
from zerg.services.archive_restore import replay_event_stream_records
from zerg.services.archive_restore import restore_archive_manifests_and_sessions
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore
from zerg.services.agents.kernel_capabilities import project_session_capabilities


def test_restore_archive_to_clean_stores_and_replay_event_stream(tmp_path):
    manifest, derived = _stores(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()
    _write_archive_fixture(archive_store, session_id=session_id)

    with manifest() as manifest_db, derived() as derived_db:
        restore = restore_archive_manifests_and_sessions(manifest_db, archive_store=archive_store)
        manifest_db.commit()

        assert restore.chunks_seen == 2
        assert restore.chunks_inserted == 2
        assert restore.sessions_created == 1
        assert restore.records_read == 4
        assert manifest_db.query(ArchiveChunk).count() == 2
        restored_session = manifest_db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert restored_session.provider == "codex"
        assert restored_session.environment == "restored"
        capabilities = project_session_capabilities(manifest_db, session_id=session_id)
        assert capabilities.control_label == "imported"
        assert capabilities.search_only is True
        assert capabilities.live_control_available is False

        hot = project_archive_chunks_to_hot_cards(manifest_db, archive_store=archive_store)
        derived_result = project_archive_chunks_to_derived_events(manifest_db, derived_db, archive_store=archive_store)
        replay = replay_event_stream_records(manifest_db, archive_store=archive_store, session_id=session_id)
        manifest_db.commit()
        derived_db.commit()

        card = manifest_db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert hot.sessions_projected == 1
        assert card.first_user_message_preview == "restore me"
        assert card.last_visible_text_preview == "restored"
        assert derived_result.events_projected == 2
        detail_rows = derived_db.execute(text("SELECT role, content_text FROM derived_events ORDER BY id")).fetchall()
        assert detail_rows == [("user", "restore me"), ("assistant", "restored")]
        search_rows = derived_db.execute(
            text("SELECT content_text FROM derived_events_fts WHERE derived_events_fts MATCH 'restore'")
        ).fetchall()
        assert [row[0] for row in search_rows] == ["restore me"]
        assert replay.chunks_seen == 1
        assert replay.records_read == 2
        assert replay.live_archive_primary_records == 1
        assert replay.legacy_export_records == 1
        assert replay.unknown_ref_records == 0
        assert sorted(replay.raw_bytes) == sorted(
            [
                b'{"type":"server","role":"system"}',
                b'{"type":"message","role":"assistant","content":"legacy raw event"}',
            ]
        )


def _stores(tmp_path):
    manifest_engine = make_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    Base.metadata.create_all(bind=manifest_engine)
    derived_engine = make_engine(f"sqlite:///{tmp_path / 'derived.db'}")
    initialize_derived_database(derived_engine)
    return make_sessionmaker(manifest_engine), make_sessionmaker(derived_engine)


def _write_archive_fixture(archive_store: FilesystemArchiveStore, *, session_id) -> None:
    archive_store.write_chunk(
        [
            _record(
                session_id,
                stream="source_lines",
                source_seq=1,
                source_offset=0,
                raw='{"type":"message","role":"user","content":"restore me","timestamp":"2026-01-01T00:00:01Z"}',
            ),
            _record(
                session_id,
                stream="source_lines",
                source_seq=2,
                source_offset=100,
                raw='{"role":"assistant","content_text":"restored","timestamp":"2026-01-01T00:00:03Z"}',
            ),
        ]
    )
    archive_store.write_chunk(
        [
            _record(
                session_id,
                stream="events",
                source_seq=10,
                source_offset=None,
                raw='{"type":"server","role":"system"}',
                legacy_ref={
                    "source": "agents_ingest",
                    "event_key": "live-event-key",
                    "role": "system",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "tool_call_id": None,
                    "source_path": None,
                    "source_offset": None,
                },
            ),
            _record(
                session_id,
                stream="events",
                source_seq=11,
                source_offset=200,
                raw='{"type":"message","role":"assistant","content":"legacy raw event"}',
                legacy_ref={
                    "table": "events",
                    "rowid": 42,
                    "event_hash": "legacy-event-hash",
                    "event_uuid": "legacy-event-uuid",
                },
            ),
        ]
    )


def _record(
    session_id,
    *,
    stream: str,
    source_seq: int,
    source_offset: int | None,
    raw: str,
    legacy_ref=None,
) -> ArchiveRecord:
    return ArchiveRecord(
        tenant_id="tenant-test",
        session_id=str(session_id),
        stream=stream,
        source_seq=source_seq,
        raw_bytes=raw.encode("utf-8"),
        legacy_ref=legacy_ref or {"source": "test"},
        provider="codex",
        source_path="/tmp/session.jsonl" if source_offset is not None else None,
        source_offset=source_offset,
    )
