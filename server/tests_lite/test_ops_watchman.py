from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSessionBranch
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_OPEN
from zerg.models.work import OPERATIONAL_INCIDENT_STATUS_RESOLVED
from zerg.models.work import OperationalIncident
from zerg.models.work import OpsWatchObservation
from zerg.models.work import OpsWatchRun
from zerg.services import ops_watchman


def _make_db(tmp_path):
    db_path = tmp_path / "ops_watchman.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine), db_path


def _seed_session(db, *, now: datetime) -> AgentSession:
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="ops-watchman",
        started_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=20),
        user_messages=1,
        assistant_messages=1,
        tool_calls=1,
        provider_session_id="provider-session",
    )
    db.add(session)
    db.flush()

    branch = AgentSessionBranch(
        session_id=session.id,
        branch_reason="root",
        is_head=1,
    )
    db.add(branch)
    db.flush()

    db.add(
        AgentEvent(
            session_id=session.id,
            branch_id=branch.id,
            role="user",
            content_text="Something changed",
            timestamp=now - timedelta(minutes=5),
            source_path="/tmp/session.jsonl",
            source_offset=1,
            event_hash="hash-user",
        )
    )
    db.add(
        AgentEvent(
            session_id=session.id,
            branch_id=branch.id,
            role="assistant",
            tool_name="Bash",
            tool_input_json={"cmd": "echo hi"},
            timestamp=now - timedelta(minutes=4),
            source_path="/tmp/session.jsonl",
            source_offset=2,
            event_hash="hash-tool",
        )
    )
    db.commit()
    db.refresh(session)
    return session


def test_collect_observations_includes_recent_session_activity(monkeypatch, tmp_path):
    SessionLocal, db_path = _make_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        session = _seed_session(db, now=now)

        observations = ops_watchman.collect_observations(db, now=now)

    sources = {row.source for row in observations}
    assert {
        "db_file_stats",
        "write_serializer",
        "ingest_health",
        "open_incidents",
        "recent_session_activity",
    } <= sources

    session_rows = [
        row for row in observations if row.source == "recent_session_activity" and row.entity_id == str(session.id)
    ]
    assert len(session_rows) == 1
    payload = session_rows[0].payload_json
    assert payload["new_events"] == 2
    assert payload["new_user_messages"] == 1
    assert payload["new_tool_calls"] == 1
    assert payload["branch_count"] == 1
    assert payload["distinct_source_paths"] == 1


def test_watchman_model_config_defaults_to_openrouter(monkeypatch):
    monkeypatch.delenv("OPS_WATCHMAN_MODEL", raising=False)
    monkeypatch.delenv("OPS_WATCHMAN_BASE_URL", raising=False)
    monkeypatch.delenv("OPS_WATCHMAN_API_KEY_ENV", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPS_WATCHMAN_REASONING_EFFORT", raising=False)

    model_id, base_url, api_key_env, api_key, reasoning = ops_watchman._watchman_model_config()

    assert model_id == "deepseek/deepseek-v4-pro"
    assert base_url == "https://openrouter.ai/api/v1"
    assert api_key_env == "OPENROUTER_API_KEY"
    assert api_key is None
    assert reasoning == "low"


@pytest.mark.asyncio
async def test_run_watchman_cycle_persists_run_observations_and_incident(monkeypatch, tmp_path):
    SessionLocal, db_path = _make_db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        _seed_session(db, now=now)

    analysis = {
        "status": "critical",
        "title": "Session Explosion",
        "summary": "One ended session is still growing fast.",
        "evidence": ["ended session gained 2 recent events in test window"],
        "should_email": True,
        "recommended_action": "Inspect the session lineage and shipper source.",
        "incident_type": "session_growth",
        "dedupe_key": "session-growth:test",
    }
    usage = {
        "input_tokens": 641,
        "output_tokens": 170,
        "total_tokens": 1462,
        "reasoning_tokens": 651,
        "provider_cost_in_usd_ticks": 5163500,
        "estimated_cost_usd": 0.0002132,
        "elapsed_ms": 5000,
    }

    async def _fake_analyze(_context):
        return analysis, usage, "deepseek/deepseek-v4-pro", None

    monkeypatch.setattr(ops_watchman, "analyze_context", _fake_analyze)
    monkeypatch.setattr(ops_watchman, "send_alert_email", lambda *args, **kwargs: "msg-1")

    result = await ops_watchman.run_watchman_cycle(db_session_factory=SessionLocal)

    assert result["status"] == "success"
    assert result["analysis_status"] == "critical"
    assert result["email_sent"] is True
    assert result["input_tokens"] == 641

    with SessionLocal() as db:
        runs = db.query(OpsWatchRun).all()
        observations = db.query(OpsWatchObservation).all()
        incidents = (
            db.query(OperationalIncident).filter(OperationalIncident.source == ops_watchman.WATCHMAN_SOURCE).all()
        )

        assert len(runs) == 1
        assert runs[0].status == "success"
        assert runs[0].analysis_status == "critical"
        assert runs[0].input_tokens == 641
        assert runs[0].estimated_cost_usd == 0.0002132
        assert len(observations) >= 4
        assert len(incidents) == 1
        assert incidents[0].status == OPERATIONAL_INCIDENT_STATUS_OPEN
        assert incidents[0].dedupe_key == "session-growth:test"


def test_reconcile_incident_normal_resolves_open_watchman_incidents(tmp_path):
    SessionLocal, _db_path = _make_db(tmp_path)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        incident = OperationalIncident(
            incident_type="session_growth",
            source=ops_watchman.WATCHMAN_SOURCE,
            dedupe_key="session-growth:test",
            status=OPERATIONAL_INCIDENT_STATUS_OPEN,
            summary="Still growing",
            context={"analysis_status": "critical"},
            opened_at=now - timedelta(minutes=5),
            last_observed_at=now - timedelta(minutes=1),
        )
        db.add(incident)
        db.commit()

        action, incident_id, _resolved_summaries = ops_watchman.reconcile_incident(
            db,
            analysis={
                "status": "normal",
                "title": "Recovered",
                "summary": "Looks normal again.",
                "evidence": [],
                "should_email": False,
                "recommended_action": "None",
                "incident_type": "session_growth",
                "dedupe_key": "",
            },
            usage={"input_tokens": 10, "estimated_cost_usd": 0.0},
            now=now,
        )
        db.commit()

        assert action == "resolved"
        assert incident_id is None

        refreshed = db.query(OperationalIncident).filter(OperationalIncident.id == incident.id).first()
        assert refreshed is not None
        assert refreshed.status == OPERATIONAL_INCIDENT_STATUS_RESOLVED
        assert refreshed.resolved_at is not None
