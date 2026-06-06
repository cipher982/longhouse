"""compaction_kind: classifier parity, write-path stamping, backfill, and read.

Gate for PR1 of the data-plane closeout: active-context boundary detection must
behave identically whether it reads the structured compaction_kind column or
falls back to parsing raw_json. We prove all three:
  1. the classifier matches the legacy raw parser exactly,
  2. live ingest stamps compaction_kind so the read needs no raw decode,
  3. backfill fills legacy NULL rows, and the read result is unchanged.
"""

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.services.agents.compaction import classify_compaction_kind
from zerg.services.agents.store import AgentsStore
from zerg.services.compaction_kind_backfill import backfill_compaction_kind


def _store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'compaction.db'}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    return engine, factory


def test_classifier_matches_legacy_parser(tmp_path):
    engine, factory = _store(tmp_path)
    try:
        with factory() as db:
            store = AgentsStore(db)
            cases = [
                ('{"type":"summary","summary":"x"}', "summary"),
                ('{"type":"system","subtype":"compact_boundary"}', "compact_boundary"),
                ('{"type":"system","subtype":"microcompact_boundary"}', "microcompact_boundary"),
                ('{"type":"system","subtype":"other"}', None),
                ('{"type":"assistant"}', None),
                ("not json", None),
                ("", None),
                (None, None),
            ]
            for raw, expected in cases:
                assert classify_compaction_kind(raw) == expected
                # Legacy boolean parser must agree: True iff classifier is non-None.
                assert store._is_compaction_boundary_raw_json(raw) is (expected is not None)
    finally:
        engine.dispose()


def _ingest(store: AgentsStore, session_id: uuid.UUID):
    from zerg.services.agents.models import EventIngest
    from zerg.services.agents.models import SessionIngest

    def _ev(role, offset, raw, ts):
        return EventIngest(
            role=role,
            content_text=role,
            timestamp=ts,
            source_path="/tmp/c.jsonl",
            source_offset=offset,
            raw_json=raw,
        )

    data = SessionIngest(
        id=session_id,
        provider="claude",
        environment="production",
        started_at="2026-01-01T00:00:00Z",
        events=[
            _ev("user", 10, '{"type":"user"}', "2026-01-01T00:00:01Z"),
            _ev("system", 20, '{"type":"system","subtype":"compact_boundary"}', "2026-01-01T00:00:02Z"),
            _ev("assistant", 30, '{"type":"assistant"}', "2026-01-01T00:00:03Z"),
        ],
    )
    store.ingest_session(data)


def test_write_path_stamps_compaction_kind_and_read_finds_boundary(tmp_path):
    engine, factory = _store(tmp_path)
    try:
        session_id = uuid.uuid4()
        with factory() as db:
            store = AgentsStore(db)
            _ingest(store, session_id)
            db.commit()

        with factory() as db:
            store = AgentsStore(db)
            boundary_rows = (
                db.query(AgentEvent)
                .filter(AgentEvent.session_id == session_id)
                .filter(AgentEvent.compaction_kind.isnot(None))
                .all()
            )
            assert [r.compaction_kind for r in boundary_rows] == ["compact_boundary"]

            boundary = store.get_active_context_boundary(session_id)
            assert boundary is not None
            assert boundary.source_offset == 20
    finally:
        engine.dispose()


def test_backfill_fills_legacy_null_rows_with_identical_read(tmp_path):
    engine, factory = _store(tmp_path)
    try:
        session_id = uuid.uuid4()
        with factory() as db:
            store = AgentsStore(db)
            _ingest(store, session_id)
            # Simulate legacy rows that predate the column.
            db.query(AgentEvent).filter(AgentEvent.session_id == session_id).update(
                {AgentEvent.compaction_kind: None}
            )
            db.commit()

        # Read still works via raw fallback before backfill.
        with factory() as db:
            store = AgentsStore(db)
            before = store.get_active_context_boundary(session_id)
            assert before is not None and before.source_offset == 20

        # Backfill, then read must return the identical boundary.
        with factory() as db:
            result = backfill_compaction_kind(db, after_id=0, batch_size=1000)
            db.commit()
            assert result.updated == 1

        with factory() as db:
            store = AgentsStore(db)
            row = (
                db.query(AgentEvent)
                .filter(AgentEvent.session_id == session_id)
                .filter(AgentEvent.source_offset == 20)
                .one()
            )
            assert row.compaction_kind == "compact_boundary"
            after = store.get_active_context_boundary(session_id)
            assert after is not None
            assert after.source_offset == before.source_offset
            assert after.event_id == before.event_id
    finally:
        engine.dispose()
