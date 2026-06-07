"""Phase D prerequisite: projection paths must work with legacy raw OFF.

When write_legacy_raw=False, raw bytes live only in the archive. These tests
prove the non-happy projection paths still produce correct structured state:
  1. the slim source_lines index row is still written (no raw payload), so
     export/resume and the rebuild path keep working;
  2. compaction boundaries survive in BULK ingests (>=100 events, which use the
     observation->reducer path, not direct projection);
  3. observation rebuild does not erase the slim index for archive-only sessions.
"""

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from sqlalchemy import text

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.models import EventIngest
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.models import SourceLineIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.session_observation_rebuild import rebuild_session_observation_projections


def _factory(tmp_path):
    # initialize_database (not bare create_all) so events_fts exists — that is
    # what makes large ingests take the observation->reducer (bulk) path with
    # FTS triggers disabled. A bare create_all would silently route through the
    # direct-projection path and hide bulk-path bugs.
    engine = make_engine(f"sqlite:///{tmp_path / 'archive_only.db'}")
    initialize_database(engine)
    return make_sessionmaker(engine)


def _boundary_session(session_id, n_events):
    """A session with n_events, one of which is a compact_boundary, plus source lines."""
    events = []
    source_lines = []
    for i in range(n_events):
        off = i * 100
        if i == n_events // 2:
            raw = '{"type":"system","subtype":"compact_boundary"}'
            role = "system"
        else:
            raw = f'{{"type":"assistant","i":{i}}}'
            role = "assistant"
        ts = f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z"
        events.append(EventIngest(role=role, content_text=f"e{i}", timestamp=ts, source_path="/tmp/s.jsonl", source_offset=off, raw_json=raw))
        source_lines.append(SourceLineIngest(source_path="/tmp/s.jsonl", source_offset=off, raw_json=raw))
    return SessionIngest(
        id=session_id, provider="claude", environment="production",
        started_at="2026-01-01T00:00:00Z", events=events, source_lines=source_lines,
    )


def test_slim_source_line_written_with_raw_off_via_observation(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid.uuid4()
    with factory() as db:
        AgentsStore(db).ingest_session(_boundary_session(session_id, 3), write_legacy_raw=False)
        db.commit()
    with factory() as db:
        rows = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).all()
        assert len(rows) == 3
        for r in rows:
            assert r.line_hash
            assert r.raw_json_z is None
            assert (r.raw_json or "") == ""


def test_bulk_ingest_preserves_compaction_kind_with_raw_off(tmp_path):
    # >=100 events forces the observation->reducer (bulk) path, not direct projection.
    factory = _factory(tmp_path)
    session_id = uuid.uuid4()
    with factory() as db:
        # Guard the guard: this test is only meaningful if events_fts exists, so
        # the >=100-event ingest actually takes the bulk reducer path.
        fts = db.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts'")
        ).fetchone()
        assert fts is not None, "events_fts must exist for the bulk path to engage"
        store = AgentsStore(db)
        store.ingest_session(_boundary_session(session_id, 120), write_legacy_raw=False)
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
        # And no raw was retained on the monolith.
        assert all(r.raw_json_z is None for r in db.query(AgentEvent).filter(AgentEvent.session_id == session_id))
        # Active-context boundary detection works without any raw read.
        boundary = store.get_active_context_boundary(session_id)
        assert boundary is not None


def test_rebuild_does_not_erase_slim_index_when_raw_off(tmp_path):
    factory = _factory(tmp_path)
    session_id = uuid.uuid4()
    with factory() as db:
        AgentsStore(db).ingest_session(_boundary_session(session_id, 5), write_legacy_raw=False)
        db.commit()
    with factory() as db:
        before = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).count()
        assert before == 5
    # Rebuild replays observations; for archive-only sessions it must NOT wipe
    # the slim index (it clears then re-derives from observation metadata).
    with factory() as db:
        rebuild_session_observation_projections(db, session_id=session_id)
        db.commit()
    with factory() as db:
        after = db.query(AgentSourceLine).filter(AgentSourceLine.session_id == session_id).all()
        assert len(after) == 5
        for r in after:
            assert r.line_hash
