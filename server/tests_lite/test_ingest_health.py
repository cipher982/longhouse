"""Unit tests for ingest health checking."""
import asyncio
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from zerg.jobs.ingest_health import compute_ingest_health
from zerg.database import Base
from zerg.models.agents import AgentHeartbeat
from zerg.models.agents import AgentSession
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import Insight
from zerg.models.work import OperationalIncident


def _make_session_local(tmp_path):
    from zerg.database import make_engine
    engine = make_engine(f"sqlite:///{tmp_path}/test.db")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_db(tmp_path):
    return _make_session_local(tmp_path)()


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


def _add_heartbeat(db, minutes_ago=5, is_offline=False):
    """Add a heartbeat row received N minutes ago."""
    now = datetime.now(timezone.utc)
    hb = AgentHeartbeat(
        device_id="test-device",
        received_at=now - timedelta(minutes=minutes_ago),
        version="0.1.0",
        spool_pending=0,
        parse_errors_1h=0,
        consecutive_failures=0,
        disk_free_bytes=0,
        is_offline=1 if is_offline else 0,
    )
    db.add(hb)
    db.commit()
    return hb


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


def test_old_session_with_online_device_returns_stale(tmp_path):
    """Device is online (recent heartbeat) but no sessions in 2 days → stale."""
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=2, ended_days_ago=2)
    _add_heartbeat(db, minutes_ago=5)
    result = compute_ingest_health(db)
    assert result["status"] == "stale"
    assert result["gap_hours"] > 4
    assert result["device_online"] is True


def test_old_session_no_heartbeat_returns_device_offline(tmp_path):
    """No recent heartbeat → device is off/sleeping, not a real alert."""
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=2, ended_days_ago=2)
    result = compute_ingest_health(db)
    assert result["status"] == "device_offline"
    assert result["device_online"] is False


def test_old_session_stale_heartbeat_returns_device_offline(tmp_path):
    """Heartbeat is >15 min old → device considered offline."""
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=2, ended_days_ago=2)
    _add_heartbeat(db, minutes_ago=30)
    result = compute_ingest_health(db)
    assert result["status"] == "device_offline"


def test_old_session_offline_flag_returns_device_offline(tmp_path):
    """Heartbeat with is_offline=True doesn't count as online."""
    db = _make_db(tmp_path)
    _add_session(db, started_days_ago=2, ended_days_ago=2)
    _add_heartbeat(db, minutes_ago=5, is_offline=True)
    result = compute_ingest_health(db)
    assert result["status"] == "device_offline"


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


def test_run_stale_opens_incident(tmp_path, monkeypatch):
    import zerg.jobs.ingest_health as ih

    SessionLocal = _make_session_local(tmp_path)
    with SessionLocal() as db:
        _add_session(db, started_days_ago=2, ended_days_ago=2)
        _add_heartbeat(db, minutes_ago=5)

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 4.0)
    with patch.object(ih, "db_session", fake_db_session):
        result = asyncio.run(ih.run())

    assert result["action"] == "incident_opened"
    with SessionLocal() as db:
        incident = db.query(OperationalIncident).filter(OperationalIncident.dedupe_key == "ingest-health:stale").one()
        assert incident.status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert incident.source == "ingest_health"
        assert incident.incident_type == "stale_ingest"
        assert db.query(Insight).count() == 0


def test_run_device_offline_does_not_open_incident(tmp_path, monkeypatch):
    """Old sessions + no heartbeat → device_offline, no incident opened."""
    import zerg.jobs.ingest_health as ih

    SessionLocal = _make_session_local(tmp_path)
    with SessionLocal() as db:
        _add_session(db, started_days_ago=2, ended_days_ago=2)
        # No heartbeat — device is offline

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 4.0)
    with patch.object(ih, "db_session", fake_db_session):
        result = asyncio.run(ih.run())

    assert result["status"] == "device_offline"
    assert result["action"] == "none"
    with SessionLocal() as db:
        assert db.query(OperationalIncident).count() == 0


def test_run_device_offline_leaves_open_incident_open(tmp_path, monkeypatch):
    """If an open stale incident exists and device goes offline, leave it open.

    Resolving it would hide the real incident duration. When the device comes
    back online the incident stays open and the clock keeps running.
    """
    import zerg.jobs.ingest_health as ih

    SessionLocal = _make_session_local(tmp_path)
    with SessionLocal() as db:
        _add_session(db, started_days_ago=2, ended_days_ago=2)
        db.add(OperationalIncident(
            incident_type="stale_ingest",
            source="ingest_health",
            dedupe_key="ingest-health:stale",
            status=OPERATIONAL_INCIDENT_STATUS_OPEN,
            summary="No sessions ingested for 6.0 hours",
        ))
        db.commit()
        # No heartbeat added — device is offline

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 4.0)
    with patch.object(ih, "db_session", fake_db_session):
        result = asyncio.run(ih.run())

    assert result["status"] == "device_offline"
    assert result["action"] == "none"
    with SessionLocal() as db:
        incident = db.query(OperationalIncident).filter(OperationalIncident.dedupe_key == "ingest-health:stale").one()
        # Must still be open — device going offline is not recovery
        assert incident.status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert incident.resolved_at is None


def test_run_recovery_resolves_open_incident(tmp_path, monkeypatch):
    import zerg.jobs.ingest_health as ih

    SessionLocal = _make_session_local(tmp_path)
    with SessionLocal() as db:
        _add_session(db, started_days_ago=0.1, ended_days_ago=0.05)
        db.add(OperationalIncident(
            incident_type="stale_ingest",
            source="ingest_health",
            dedupe_key="ingest-health:stale",
            status=OPERATIONAL_INCIDENT_STATUS_OPEN,
            summary="No sessions ingested for 6.0 hours",
        ))
        db.commit()

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 4.0)
    with patch.object(ih, "db_session", fake_db_session):
        result = asyncio.run(ih.run())

    assert result["action"] == "incident_resolved"
    with SessionLocal() as db:
        incident = db.query(OperationalIncident).filter(OperationalIncident.dedupe_key == "ingest-health:stale").one()
        assert incident.status == OPERATIONAL_INCIDENT_STATUS_RESOLVED
        assert incident.resolved_at is not None
        assert db.query(Insight).count() == 0


def test_run_stale_updates_existing_open_incident(tmp_path, monkeypatch):
    import zerg.jobs.ingest_health as ih

    SessionLocal = _make_session_local(tmp_path)
    with SessionLocal() as db:
        _add_session(db, started_days_ago=2, ended_days_ago=2)
        _add_heartbeat(db, minutes_ago=5)
        db.add(OperationalIncident(
            incident_type="stale_ingest",
            source="ingest_health",
            dedupe_key="ingest-health:stale",
            status=OPERATIONAL_INCIDENT_STATUS_OPEN,
            summary="Old summary",
        ))
        db.commit()

    @contextmanager
    def fake_db_session():
        with SessionLocal() as db:
            yield db

    monkeypatch.setattr(ih, "_THRESHOLD_HOURS", 4.0)
    with patch.object(ih, "db_session", fake_db_session):
        result = asyncio.run(ih.run())

    assert result["action"] == "incident_updated"
    with SessionLocal() as db:
        incidents = db.query(OperationalIncident).filter(OperationalIncident.dedupe_key == "ingest-health:stale").all()
        assert len(incidents) == 1
        assert incidents[0].status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert "threshold: 4.0h" in incidents[0].summary
        assert db.query(Insight).count() == 0
