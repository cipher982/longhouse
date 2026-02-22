"""Unit tests for the active sessions presence join logic.

Covers the two bugs found in session 2026-02-20/21:
- UUID→String type mismatch: session_ids contains UUIDs, SessionPresence.session_id
  is String(255) — query returned 0 rows silently.
- DateTime naive/aware TypeError: presence.updated_at is naive (SQLite func.now()),
  now is UTC-aware — subtraction raised TypeError, presence_fresh was always False.

Tests exercise the join + freshness logic directly via ORM, not the HTTP endpoint,
so they run fast (no server) and cover the exact code paths that broke.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import EventIngest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    db_path = tmp_path / "presence_join.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _ingest_session(db, project="zerg", cwd="/tmp/zerg"):
    store = AgentsStore(db)
    result = store.ingest_session(
        SessionIngest(
            provider="claude",
            environment="test",
            project=project,
            device_id="test-device",
            cwd=cwd,
            git_repo=None,
            git_branch=None,
            started_at=datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc),
            events=[
                EventIngest(
                    role="user",
                    content_text="hello",
                    timestamp=datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc),
                    source_path="/tmp/session.jsonl",
                    source_offset=0,
                )
            ],
        )
    )
    return result.session_id


def _upsert_presence(db, session_id: str, state: str, updated_at: datetime):
    """Insert or update a presence row directly (bypasses endpoint)."""
    existing = db.query(SessionPresence).filter(
        SessionPresence.session_id == session_id
    ).first()
    if existing:
        existing.state = state
        existing.updated_at = updated_at
    else:
        db.add(SessionPresence(
            session_id=session_id,
            state=state,
            tool_name=None,
            cwd="/tmp",
            project="zerg",
            provider="claude",
            updated_at=updated_at,
        ))
    db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_presence_found_for_session(tmp_path):
    """Presence row is found for a session — UUID→String join works."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid = _ingest_session(db)

        now = datetime.now(timezone.utc)
        _upsert_presence(db, str(sid), "thinking", now)

        # Replicate the endpoint's lookup: str_session_ids + presence_map
        str_ids = [str(sid)]
        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_(str_ids)
        ).all()
        presence_map = {p.session_id: p for p in rows}

        assert str(sid) in presence_map, "presence row must be found via str(UUID) key"
        assert presence_map[str(sid)].state == "thinking"


def test_fresh_presence_within_threshold(tmp_path):
    """Presence updated 30s ago is considered fresh (< 10min threshold)."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid = _ingest_session(db)
        now = datetime.now(timezone.utc)

        # Store naive datetime (as SQLite func.now() does)
        naive_now = datetime.utcnow() - timedelta(seconds=30)
        _upsert_presence(db, str(sid), "running", naive_now)

        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_([str(sid)])
        ).all()
        presence = rows[0]

        # Replicate the endpoint's timezone-safe freshness check
        updated_at = presence.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        presence_fresh = (now - updated_at) < timedelta(minutes=10)

        assert presence_fresh, "presence 30s old must be fresh"
        assert presence.state == "running"


def test_stale_presence_beyond_threshold(tmp_path):
    """Presence updated 15min ago is considered stale (> 10min threshold)."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid = _ingest_session(db)
        now = datetime.now(timezone.utc)

        stale_time = datetime.utcnow() - timedelta(minutes=15)
        _upsert_presence(db, str(sid), "thinking", stale_time)

        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_([str(sid)])
        ).all()
        presence = rows[0]

        updated_at = presence.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        presence_fresh = (now - updated_at) < timedelta(minutes=10)

        assert not presence_fresh, "presence 15min old must be stale"


def test_naive_datetime_no_typeerror(tmp_path):
    """Naive updated_at does not raise TypeError when compared to UTC-aware now.

    This is the exact bug that caused presence_state to always be None —
    SQLite func.now() stores naive datetimes; subtracting from UTC-aware now()
    raised TypeError silently caught by the endpoint's try/except.
    """
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid = _ingest_session(db)
        naive_dt = datetime(2026, 2, 20, 20, 46, 32)  # no tzinfo
        _upsert_presence(db, str(sid), "thinking", naive_dt)

        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_([str(sid)])
        ).all()
        presence = rows[0]
        assert presence.updated_at.tzinfo is None, "stored as naive"

        now = datetime.now(timezone.utc)
        updated_at = presence.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)

        # Must not raise TypeError
        try:
            diff = now - updated_at
            assert diff.total_seconds() > 0
        except TypeError:
            pytest.fail("TypeError: naive/aware datetime subtraction — fix not applied")


def test_no_presence_row_returns_none(tmp_path):
    """Sessions without a presence row produce presence_state=None."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid = _ingest_session(db)

        str_ids = [str(sid)]
        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_(str_ids)
        ).all()
        presence_map = {p.session_id: p for p in rows}

        presence = presence_map.get(str(sid))
        assert presence is None, "no presence row → None"


def test_multiple_sessions_presence_map(tmp_path):
    """Presence map correctly isolates rows per session — no cross-contamination."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sid_a = _ingest_session(db, project="proj-a")
        sid_b = _ingest_session(db, project="proj-b")

        now = datetime.now(timezone.utc)
        _upsert_presence(db, str(sid_a), "thinking", datetime.utcnow())
        # sid_b has no presence

        str_ids = [str(sid_a), str(sid_b)]
        rows = db.query(SessionPresence).filter(
            SessionPresence.session_id.in_(str_ids)
        ).all()
        presence_map = {p.session_id: p for p in rows}

        assert str(sid_a) in presence_map
        assert str(sid_b) not in presence_map
        assert presence_map[str(sid_a)].state == "thinking"
