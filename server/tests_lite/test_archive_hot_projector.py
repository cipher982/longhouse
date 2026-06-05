from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import uuid4

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import ArchiveChunk
from zerg.models.agents import ProjectorCheckpoint
from zerg.models.agents import TimelineCard
from zerg.services.archive_hot_projector import HOT_CARD_PARSER_REVISION
from zerg.services.archive_hot_projector import HOT_CARD_PROJECTOR_NAME
from zerg.services.archive_hot_projector import project_archive_chunks_to_hot_cards
from zerg.services.archive_hot_projector import select_pending_archive_chunks
from zerg.services.archive_store import ArchiveRecord
from zerg.services.archive_store import FilesystemArchiveStore


def test_archive_hot_projector_builds_card_and_checkpoints(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()
    ts = _ts()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Hello Longhouse"}}',
                ),
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=100,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_use","name":"Read","id":"tool-1","input":{}}]}}',
                ),
                _record(
                    session_id,
                    source_seq=3,
                    source_offset=200,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:05Z","message":{"content":[{"type":"text","text":"Done."}]}}',
                ),
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        checkpoint = db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()

        assert result.selected_chunks == 1
        assert result.sessions_projected == 1
        assert result.events_projected == 3
        assert session.user_messages == 1
        assert session.assistant_messages == 1
        assert session.tool_calls == 1
        assert session.first_user_message_preview == "Hello Longhouse"
        assert session.last_visible_text_preview == "Done."
        assert session.last_activity_at == ts.replace(second=5, tzinfo=None)
        assert card.user_messages == 1
        assert card.assistant_messages == 1
        assert card.tool_calls == 1
        assert card.first_user_message_preview == "Hello Longhouse"
        assert card.last_visible_text_preview == "Done."
        assert card.archive_state == "current"
        assert card.parser_revision == HOT_CARD_PARSER_REVISION
        assert checkpoint.projector_name == HOT_CARD_PROJECTOR_NAME
        assert checkpoint.parser_revision == HOT_CARD_PARSER_REVISION
        assert checkpoint.status == "current"

        rerun = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)

        assert rerun.selected_chunks == 0


def test_archive_hot_projector_supports_generic_normalized_event_json(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="codex")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    provider="codex",
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"message","role":"user","content":"ship it","timestamp":"2026-01-01T00:00:01Z"}',
                ),
                _record(
                    session_id,
                    provider="codex",
                    source_seq=2,
                    source_offset=50,
                    raw='{"role":"assistant","content_text":"shipped","timestamp":"2026-01-01T00:00:03Z"}',
                ),
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert result.sessions_projected == 1
        assert card.user_messages == 1
        assert card.assistant_messages == 1
        assert card.first_user_message_preview == "ship it"
        assert card.last_visible_text_preview == "shipped"


def test_archive_hot_projector_does_not_move_last_activity_backward(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()
    newer_activity = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)

    with SessionLocal() as db:
        session = _add_session(db, session_id=session_id, provider="claude")
        session.last_activity_at = newer_activity.replace(tzinfo=None)
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"older archive"}}',
                )
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert result.sessions_projected == 1
        assert session.last_activity_at == newer_activity.replace(tzinfo=None)
        assert card.last_activity_at == newer_activity.replace(tzinfo=None)


def test_archive_hot_projector_skips_sidechain_records_for_parent_counts(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"root request"}}',
                ),
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=100,
                    raw=(
                        '{"type":"user","isSidechain":true,"timestamp":"2026-01-01T00:00:01Z",'
                        '"message":{"content":"child task"}}'
                    ),
                ),
                _record(
                    session_id,
                    source_seq=3,
                    source_offset=200,
                    raw=(
                        '{"type":"assistant","timestamp":"2026-01-01T00:00:02Z",'
                        '"message":{"content":[{"type":"text","text":"parent response"}]}}'
                    ),
                ),
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert result.sessions_projected == 1
        assert result.events_projected == 2
        assert card.user_messages == 1
        assert card.assistant_messages == 1
        assert card.first_user_message_preview == "root request"
        assert card.last_visible_text_preview == "parent response"


def test_archive_hot_projector_does_not_overwrite_from_partial_archive(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        session = _add_session(db, session_id=session_id, provider="claude")
        session.user_messages = 7
        session.first_user_message_preview = "existing full card"
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=500,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"partial append"}}',
                )
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        checkpoint = db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()
        assert result.sessions_projected == 0
        assert result.sessions_partial == 1
        assert session.user_messages == 7
        assert session.first_user_message_preview == "existing full card"
        assert db.query(TimelineCard).filter(TimelineCard.session_id == session_id).first() is None
        assert checkpoint.status == "current"


def test_archive_hot_projector_rebuilds_from_out_of_order_chunk_arrival(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=100,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:04Z","message":{"content":[{"type":"text","text":"second"}]}}',
                )
            ],
        )
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:01Z","message":{"content":"first"}}',
                )
            ],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store, limit=2)
        db.commit()

        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert result.selected_chunks == 2
        assert result.sessions_projected == 1
        assert card.first_user_message_preview == "first"
        assert card.last_visible_text_preview == "second"
        assert db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.status == "current").count() == 2


def test_archive_hot_projector_full_rebuild_checkpoints_whole_session_once(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=10,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"first"}}',
                )
            ],
        )
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=100,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"middle"}]}}',
                )
            ],
        )
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=3,
                    source_offset=200,
                    raw='{"type":"assistant","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"text","text":"final"}]}}',
                )
            ],
        )
        db.commit()

        first = project_archive_chunks_to_hot_cards(db, archive_store=archive_store, limit=1)
        db.commit()

        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        pending_after_first = select_pending_archive_chunks(db, limit=10)
        assert first.selected_chunks == 1
        assert first.events_projected == 3
        assert first.checkpoints_written == 3
        assert card.assistant_messages == 2
        assert card.last_visible_text_preview == "final"
        assert pending_after_first == []

        second = project_archive_chunks_to_hot_cards(db, archive_store=archive_store, limit=10)
        db.commit()

        assert second.selected_chunks == 0
        assert db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.status == "current").count() == 3
        assert select_pending_archive_chunks(db, limit=10) == []


def test_archive_hot_projector_incrementally_updates_after_checkpointed_append(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"first"}}',
                )
            ],
        )
        db.commit()

        first = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=100,
                    raw=(
                        '{"type":"assistant","timestamp":"2026-01-01T00:00:03Z",'
                        '"message":{"content":[{"type":"text","text":"second"}]}}'
                    ),
                ),
                _record(
                    session_id,
                    source_seq=2,
                    source_offset=200,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:04Z","message":{"content":"third"}}',
                ),
            ],
        )
        db.commit()

        second = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        card = db.query(TimelineCard).filter(TimelineCard.session_id == session_id).one()
        assert first.sessions_projected == 1
        assert second.selected_chunks == 1
        assert second.sessions_projected == 1
        assert second.events_projected == 2
        assert session.user_messages == 2
        assert session.assistant_messages == 1
        assert card.user_messages == 2
        assert card.assistant_messages == 1
        assert card.first_user_message_preview == "first"
        assert card.last_visible_text_preview == "third"
        assert card.archive_last_source_offset == 200
        assert db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.status == "current").count() == 2


def test_archive_hot_projector_tracks_parser_revision_replay(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}',
                )
            ],
        )
        db.commit()

        first = project_archive_chunks_to_hot_cards(db, archive_store=archive_store, parser_revision="parser-a")
        second = project_archive_chunks_to_hot_cards(db, archive_store=archive_store, parser_revision="parser-b")
        db.commit()

        assert first.selected_chunks == 1
        assert second.selected_chunks == 1
        assert db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).count() == 2
        assert select_pending_archive_chunks(db, parser_revision="parser-b") == []


def test_archive_hot_projector_marks_unsupported_chunks_terminal(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="codex")
        _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[_record(session_id, provider="codex", source_seq=1, source_offset=0, raw='{"kind":"unknown"}')],
        )
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        checkpoint = db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()
        assert result.unsupported_chunks == 1
        assert result.sessions_partial == 1
        assert checkpoint.status == "unsupported"
        assert select_pending_archive_chunks(db) == []


def test_archive_hot_projector_records_corruption_error_for_retry(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    archive_store = FilesystemArchiveStore(tmp_path / "archive")
    session_id = uuid4()

    with SessionLocal() as db:
        _add_session(db, session_id=session_id, provider="claude")
        chunk = _add_archive_chunk(
            db,
            archive_store,
            session_id=session_id,
            records=[
                _record(
                    session_id,
                    source_seq=1,
                    source_offset=0,
                    raw='{"type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}',
                )
            ],
        )
        archive_store.root.joinpath(chunk.relative_path).write_bytes(b"not zstd")
        db.commit()

        result = project_archive_chunks_to_hot_cards(db, archive_store=archive_store)
        db.commit()

        checkpoint = db.query(ProjectorCheckpoint).filter(ProjectorCheckpoint.session_id == session_id).one()
        assert result.chunks_failed == 1
        assert checkpoint.status == "error"
        assert checkpoint.error == "ArchiveCorruptionError"
        assert select_pending_archive_chunks(db) == [chunk]


def _session_factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'archive-hot-projector.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _add_session(db, *, session_id, provider: str) -> AgentSession:
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="test",
        project="longhouse",
        device_id="device-1",
        cwd="/tmp/longhouse",
        started_at=_ts(),
        last_activity_at=_ts(),
    )
    db.add(session)
    db.flush()
    return session


def _record(
    session_id,
    *,
    source_seq: int,
    source_offset: int,
    raw: str,
    provider: str = "claude",
) -> ArchiveRecord:
    return ArchiveRecord(
        tenant_id="tenant-test",
        session_id=str(session_id),
        stream="source_lines",
        source_seq=source_seq,
        raw_bytes=raw.encode("utf-8"),
        legacy_ref={"source": "test"},
        provider=provider,
        source_path="/tmp/session.jsonl",
        source_offset=source_offset,
    )


def _add_archive_chunk(
    db, archive_store: FilesystemArchiveStore, *, session_id, records: list[ArchiveRecord]
) -> ArchiveChunk:
    ref = archive_store.write_chunk(records)
    chunk = ArchiveChunk(
        tenant_id=ref.tenant_id,
        session_id=session_id,
        stream=ref.stream,
        relative_path=ref.relative_path,
        first_source_seq=ref.first_source_seq,
        last_source_seq=ref.last_source_seq,
        record_count=ref.record_count,
        uncompressed_bytes=ref.uncompressed_bytes,
        compressed_bytes=ref.compressed_bytes,
        payload_sha256=ref.payload_sha256,
        file_sha256=ref.file_sha256,
        state="sealed",
    )
    db.add(chunk)
    db.flush()
    return chunk
