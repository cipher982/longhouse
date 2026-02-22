"""Unit tests for ingest health checking."""
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import sessionmaker

from zerg.models.agents import AgentsBase, AgentSession
from zerg.jobs.ingest_health import compute_ingest_health


def _make_db(tmp_path):
    from zerg.database import make_engine
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _add_session(db, started_days_ago, ended_days_ago=None):
    now = datetime.now(timezone.utc)
    s = AgentSession(
        provider="claude",
        environment="production",
        started_at=now - timedelta(days=started_days_ago),
        ended_at=(now - timedelta(days=ended_days_ago)) if ended_days_ago is not None else None,
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        needs_embedding=0,
        user_state=None,
    )
    db.add(s)
    db.commit()
    return s


def test_no_sessions_returns_unknown(tmp_path):
    db = _make_db(tmp_path)
    result = compute_ingest_health(db)
    assert result["status"] == "unknown"
    assert result["session_count"] == 0


def test_recent_session_returns_ok(tmp_path):
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=0.1, ended_days_ago=0.05)
    result = compute_ingest_health(db)
    assert result["status"] == "ok"
    assert result["gap_hours"] < 4


def test_old_session_returns_stale(tmp_path):
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=2, ended_days_ago=2)
    # threshold is 4h by default, session is 2 days old → stale
    result = compute_ingest_health(db)
    assert result["status"] == "stale"
    assert result["gap_hours"] > 4


def test_in_progress_session_uses_started_at(tmp_path):
    """ongoing session (ended_at=NULL) with recent started_at → ok."""
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=0.1, ended_days_ago=None)
    result = compute_ingest_health(db)
    assert result["status"] == "ok"


def test_threshold_zero_always_ok(tmp_path, monkeypatch):
    import zerg.jobs.ingest_health as ih
    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 0.0)
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=10, ended_days_ago=10)
    result = compute_ingest_health(db)
    assert result["status"] == "ok"
