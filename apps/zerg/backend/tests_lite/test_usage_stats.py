"""Unit tests for token daily stats rollup."""
import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentsBase, AgentSession


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _add_session(db, provider, started_days_ago=0):
    """Add a session. AgentSession has no model/approx_token_count columns yet."""
    now = datetime.now(timezone.utc)
    s = AgentSession(
        provider=provider,
        environment="production",
        started_at=now - timedelta(days=started_days_ago, hours=1),
        ended_at=now - timedelta(days=started_days_ago),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        needs_embedding=0,
        user_state=None,
    )
    db.add(s)
    db.commit()
    return s


def _run_rollup(db):
    """Run the token rollup synchronously for testing."""
    from zerg.jobs import token_rollup
    import unittest.mock as mock
    from contextlib import contextmanager

    @contextmanager
    def mock_db_session():
        yield db

    with mock.patch("zerg.jobs.token_rollup.db_session", mock_db_session):
        return asyncio.run(token_rollup.run())


def test_rollup_aggregates_sessions(tmp_path):
    db = _make_db(tmp_path)
    _add_session(db, "claude")
    _add_session(db, "claude")
    _add_session(db, "gemini")

    result = _run_rollup(db)
    assert result["rows_written"] >= 2

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = db.execute(
        text("SELECT * FROM token_daily_stats WHERE date = :d"),
        {"d": today}
    ).fetchall()

    claude_row = next((r for r in rows if r.provider == "claude"), None)
    assert claude_row is not None
    assert claude_row.session_count == 2

    gemini_row = next((r for r in rows if r.provider == "gemini"), None)
    assert gemini_row is not None
    assert gemini_row.session_count == 1


def test_rollup_idempotent(tmp_path):
    db = _make_db(tmp_path)
    _add_session(db, "claude")

    _run_rollup(db)
    _run_rollup(db)  # Run twice

    rows = db.execute(text("SELECT * FROM token_daily_stats WHERE provider = 'claude'")).fetchall()
    assert len(rows) == 1  # No duplicates
    assert rows[0].session_count == 1


def test_rollup_total_tokens_always_zero(tmp_path):
    """Until approx_token_count column exists, total_tokens is 0."""
    db = _make_db(tmp_path)
    _add_session(db, "claude")

    _run_rollup(db)

    rows = db.execute(text("SELECT * FROM token_daily_stats WHERE provider = 'claude'")).fetchall()
    assert rows[0].total_tokens == 0


def test_rollup_multiple_days(tmp_path):
    """Rollup covers last 7 days."""
    db = _make_db(tmp_path)
    _add_session(db, "claude", started_days_ago=0)
    _add_session(db, "claude", started_days_ago=3)

    result = _run_rollup(db)
    assert result["days_recomputed"] == 7

    rows = db.execute(text("SELECT * FROM token_daily_stats WHERE provider = 'claude'")).fetchall()
    # Two different dates → two rows
    assert len(rows) == 2
