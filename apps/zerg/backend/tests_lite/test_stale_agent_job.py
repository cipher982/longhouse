"""Tests for the check_stale_agents builtin job.

Covers:
- Stale device (no heartbeat >30min) emits an Insight
- Fresh device (heartbeat <30min) does not emit
- Second run with same device deduplicates (appends to observations)

Uses in-memory SQLite, ORM-only (no HTTP). No shared conftest.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentsBase
from zerg.models.work import Insight


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_stale_agents.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal, engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stale_agent_job_emits_insight(tmp_path):
    """A device with no heartbeat in >30min triggers an insight."""
    SessionLocal, engine = _make_db(tmp_path)

    # Insert a stale heartbeat (40 minutes ago)
    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=40)
    with SessionLocal() as db:
        db.add(AgentHeartbeat(
            device_id="stale-device-001",
            received_at=stale_ts,
            version="0.5.0",
            spool_pending=0,
            parse_errors_1h=0,
            consecutive_failures=0,
            disk_free_bytes=0,
            is_offline=0,
        ))
        db.commit()

    # Patch db_session to use our test engine
    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    import zerg.jobs.check_stale_agents as job_module

    with patch.object(job_module, "db_session", fake_db_session):
        import asyncio
        result = asyncio.run(job_module.run())

    assert result["success"] is True
    assert result["stale_devices"] == 1

    with SessionLocal() as db:
        insights = db.query(Insight).filter(
            Insight.title.contains("stale-device-001")
        ).all()
        assert len(insights) == 1, f"Expected 1 insight, got {len(insights)}"
        assert insights[0].insight_type == "failure"
        assert insights[0].severity == "warning"
        assert "stale-agent" in (insights[0].tags or [])


def test_fresh_agent_no_insight(tmp_path):
    """A device with a recent heartbeat (<30min) does not trigger an insight."""
    SessionLocal, engine = _make_db(tmp_path)

    fresh_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    with SessionLocal() as db:
        db.add(AgentHeartbeat(
            device_id="fresh-device-001",
            received_at=fresh_ts,
            version="0.5.0",
            spool_pending=0,
            parse_errors_1h=0,
            consecutive_failures=0,
            disk_free_bytes=0,
            is_offline=0,
        ))
        db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    import zerg.jobs.check_stale_agents as job_module

    with patch.object(job_module, "db_session", fake_db_session):
        import asyncio
        result = asyncio.run(job_module.run())

    assert result["success"] is True
    assert result["stale_devices"] == 0

    with SessionLocal() as db:
        count = db.query(Insight).count()
        assert count == 0, "No insights should be emitted for a fresh device"


def test_stale_agent_deduplicates_on_second_run(tmp_path):
    """Second run for same stale device appends to observations, not creates a new insight."""
    SessionLocal, engine = _make_db(tmp_path)

    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=40)
    with SessionLocal() as db:
        db.add(AgentHeartbeat(
            device_id="dedup-device-001",
            received_at=stale_ts,
            version="0.5.0",
            spool_pending=0,
            parse_errors_1h=0,
            consecutive_failures=0,
            disk_free_bytes=0,
            is_offline=0,
        ))
        db.commit()

    from unittest.mock import patch
    from contextlib import contextmanager

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    import zerg.jobs.check_stale_agents as job_module
    import asyncio

    with patch.object(job_module, "db_session", fake_db_session):
        asyncio.run(job_module.run())
        asyncio.run(job_module.run())

    with SessionLocal() as db:
        insights = db.query(Insight).filter(
            Insight.title.contains("dedup-device-001")
        ).all()
        assert len(insights) == 1, "Should have exactly 1 insight (deduped)"
        assert len(insights[0].observations or []) >= 1, "Second run should append to observations"
