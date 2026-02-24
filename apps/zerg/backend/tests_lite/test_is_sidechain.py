"""Regression tests for is_sidechain ground-truth autonomous session detection.

Covers:
- Ingest with is_sidechain=True stores is_sidechain=1 in DB
- list_sessions(hide_autonomous=True) excludes sidechain sessions
- list_sessions(hide_autonomous=False) includes sidechain sessions
- hide_autonomous still excludes user_messages=0 sessions regardless of is_sidechain
- Normal sessions (is_sidechain=False, user_messages>0) always pass both filters
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database, make_engine
from zerg.services.agents_store import AgentsStore, EventIngest, SessionIngest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path, name="test_sidechain.db"):
    """Create a fresh SQLite DB with agents tables, return (store, db)."""
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    db = sessionmaker(bind=engine)()
    return AgentsStore(db), db


def _ts(hour=10):
    return datetime(2026, 1, 1, hour, 0, 0, tzinfo=timezone.utc)


def _ingest(store, *, is_sidechain=False, user_messages=1, session_id=None):
    """Helper to ingest a minimal session."""
    sid = session_id or uuid4()
    events = []
    for i in range(user_messages):
        events.append(EventIngest(
            role="user",
            content_text=f"Hello {i}",
            timestamp=_ts(i + 1),
            source_path="/tmp/test.jsonl",
            source_offset=i * 100,
        ))
    data = SessionIngest(
        id=sid,
        provider="claude",
        environment="production",
        started_at=_ts(0),
        is_sidechain=is_sidechain,
        events=events,
    )
    return store.ingest_session(data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sidechain_flag_stored(tmp_path):
    """Ingest with is_sidechain=True stores 1 in DB."""
    store, _ = _make_store(tmp_path)
    result = _ingest(store, is_sidechain=True, user_messages=1)
    session = store.get_session(result.session_id)
    assert session is not None
    assert session.is_sidechain == 1


def test_non_sidechain_flag_stored(tmp_path):
    """Ingest with is_sidechain=False stores 0 in DB."""
    store, _ = _make_store(tmp_path)
    result = _ingest(store, is_sidechain=False, user_messages=1)
    session = store.get_session(result.session_id)
    assert session is not None
    assert session.is_sidechain == 0


def test_reingest_updates_sidechain_flag(tmp_path):
    """Re-ingesting a session updates the is_sidechain flag."""
    store, _ = _make_store(tmp_path)
    # First ingest: not sidechain
    result = _ingest(store, is_sidechain=False, user_messages=1)
    sid = result.session_id

    # Second ingest with same session ID but now sidechain
    data = SessionIngest(
        id=sid,
        provider="claude",
        environment="production",
        started_at=_ts(0),
        is_sidechain=True,
        events=[],
    )
    store.ingest_session(data)

    session = store.get_session(sid)
    assert session.is_sidechain == 1


def test_hide_autonomous_excludes_sidechain(tmp_path):
    """hide_autonomous=True excludes sessions with is_sidechain=1."""
    store, _ = _make_store(tmp_path)
    _ingest(store, is_sidechain=True, user_messages=2)   # sidechain — should be hidden
    _ingest(store, is_sidechain=False, user_messages=1)  # normal — should be visible

    sessions, total = store.list_sessions(hide_autonomous=True)
    assert total == 1
    assert sessions[0].is_sidechain == 0


def test_hide_autonomous_false_includes_sidechain(tmp_path):
    """hide_autonomous=False includes sidechain sessions."""
    store, _ = _make_store(tmp_path)
    _ingest(store, is_sidechain=True, user_messages=2)
    _ingest(store, is_sidechain=False, user_messages=1)

    sessions, total = store.list_sessions(hide_autonomous=False)
    assert total == 2


def test_hide_autonomous_still_excludes_zero_user_messages(tmp_path):
    """hide_autonomous=True still excludes sessions with user_messages=0 even if is_sidechain=0."""
    store, _ = _make_store(tmp_path)
    _ingest(store, is_sidechain=False, user_messages=0)  # no user msgs — should be hidden
    _ingest(store, is_sidechain=False, user_messages=1)  # normal — visible

    sessions, total = store.list_sessions(hide_autonomous=True)
    assert total == 1
    assert sessions[0].user_messages == 1


def test_normal_session_always_visible(tmp_path):
    """Normal session (is_sidechain=False, user_messages>0) passes all filters."""
    store, _ = _make_store(tmp_path)
    _ingest(store, is_sidechain=False, user_messages=3)

    sessions_hidden, _ = store.list_sessions(hide_autonomous=True)
    sessions_all, _ = store.list_sessions(hide_autonomous=False)
    assert len(sessions_hidden) == 1
    assert len(sessions_all) == 1
