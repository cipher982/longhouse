"""Tests for the check_stale_agents builtin job.

Covers:
- Stale device (no heartbeat >30min) opens an incident
- Fresh device (heartbeat <30min) does not emit
- Second run with same device deduplicates against the same open incident
- Recovered device resolves the open incident

Uses in-memory SQLite, ORM-only (no HTTP). No shared conftest.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from sqlalchemy.orm import sessionmaker

from zerg.database import make_engine
from zerg.models.agents import AgentHeartbeat
from zerg.database import Base
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import Insight
from zerg.models.work import OperationalIncident

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    db_path = tmp_path / "test_stale_agents.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal, engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stale_agent_job_opens_incident(tmp_path):
    """A device with no heartbeat in >30min opens an incident."""
    SessionLocal, engine = _make_db(tmp_path)

    # Insert 3 stale heartbeats (device must have >=3 to be monitored)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for minutes_ago in (40, 100, 160):
            db.add(AgentHeartbeat(
                device_id="stale-device-001",
                received_at=now - timedelta(minutes=minutes_ago),
                version="0.5.0",
                spool_pending=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=0,
                is_offline=0,
            ))
        db.commit()

    # Patch db_session to use our test engine
    from contextlib import contextmanager
    from unittest.mock import patch

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
        incidents = db.query(OperationalIncident).filter(
            OperationalIncident.dedupe_key == "stale-agent:stale-device-001"
        ).all()
        assert len(incidents) == 1, f"Expected 1 incident, got {len(incidents)}"
        assert incidents[0].incident_type == "stale_agent"
        assert incidents[0].source == "check_stale_agents"
        assert incidents[0].status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert incidents[0].context["device_id"] == "stale-device-001"
        assert db.query(Insight).count() == 0


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

    from contextlib import contextmanager
    from unittest.mock import patch

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
        assert db.query(OperationalIncident).count() == 0
        assert db.query(Insight).count() == 0


def test_stale_agent_deduplicates_on_second_run(tmp_path):
    """Second run for same stale device updates the same open incident."""
    SessionLocal, engine = _make_db(tmp_path)

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for minutes_ago in (40, 100, 160):
            db.add(AgentHeartbeat(
                device_id="dedup-device-001",
                received_at=now - timedelta(minutes=minutes_ago),
                version="0.5.0",
                spool_pending=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=0,
                is_offline=0,
            ))
        db.commit()

    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    import asyncio

    import zerg.jobs.check_stale_agents as job_module

    with patch.object(job_module, "db_session", fake_db_session):
        first = asyncio.run(job_module.run())
        second = asyncio.run(job_module.run())

    with SessionLocal() as db:
        incidents = db.query(OperationalIncident).filter(
            OperationalIncident.dedupe_key == "stale-agent:dedup-device-001"
        ).all()
        assert len(incidents) == 1, "Should have exactly 1 incident (deduped)"
        assert incidents[0].status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert first["incidents_opened"] == 1
        assert second["incidents_updated"] == 1
        assert db.query(Insight).count() == 0


def test_stale_agent_incident_resolves_when_heartbeat_recovers(tmp_path):
    """A previously stale device resolves its incident after a fresh heartbeat."""
    SessionLocal, engine = _make_db(tmp_path)

    now_ts = datetime.now(timezone.utc)
    fresh_ts = now_ts
    with SessionLocal() as db:
        for minutes_ago in (40, 100, 160):
            db.add(AgentHeartbeat(
                device_id="recovered-device-001",
                received_at=now_ts - timedelta(minutes=minutes_ago),
                version="0.5.0",
                spool_pending=0,
                parse_errors_1h=0,
                consecutive_failures=0,
                disk_free_bytes=0,
                is_offline=0,
            ))
        db.commit()

    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    import asyncio

    import zerg.jobs.check_stale_agents as job_module

    with patch.object(job_module, "db_session", fake_db_session):
        asyncio.run(job_module.run())

    with SessionLocal() as db:
        db.add(AgentHeartbeat(
            device_id="recovered-device-001",
            received_at=fresh_ts,
            version="0.5.0",
            spool_pending=0,
            parse_errors_1h=0,
            consecutive_failures=0,
            disk_free_bytes=0,
            is_offline=0,
        ))
        db.commit()

    with patch.object(job_module, "db_session", fake_db_session):
        result = asyncio.run(job_module.run())

    assert result["incidents_resolved"] == 1
    with SessionLocal() as db:
        incident = db.query(OperationalIncident).filter(
            OperationalIncident.dedupe_key == "stale-agent:recovered-device-001"
        ).one()
        assert incident.status == OPERATIONAL_INCIDENT_STATUS_RESOLVED
        assert incident.resolved_at is not None
        assert db.query(Insight).count() == 0
