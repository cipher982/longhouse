from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.models import Runner
from zerg.models.models import RunnerHealthIncident
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.runner_health_reconciler import OPEN_INCIDENT_STATUS
from zerg.services.runner_health_reconciler import RESOLVED_INCIDENT_STATUS
from zerg.services.runner_health_reconciler import reconcile_runner_health
from zerg.utils.time import utc_now_naive

pytestmark = pytest.mark.asyncio


def _make_db(tmp_path: Path):
    db_path = tmp_path / "runner-health-reconcile.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal()


async def test_reconcile_opens_offline_incident_and_marks_cached_status(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = utc_now_naive()
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="cube",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="online",
            last_seen_at=now - timedelta(minutes=10),
            runner_metadata={"install_mode": "server", "capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        with patch(
            "zerg.services.runner_health_reconciler.get_runner_connection_manager",
            return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: False),
        ):
            result = await reconcile_runner_health(db, now=now)

        db.refresh(runner)
        incident = db.query(RunnerHealthIncident).filter(RunnerHealthIncident.runner_id == runner.id).one()

        assert result["checked"] == 1
        assert result["cached_status_updates"] == 1
        assert result["incidents_opened"] == 1
        assert runner.status == "offline"
        assert incident.status == OPEN_INCIDENT_STATUS
        assert incident.reason_code == "stale_heartbeat"
    finally:
        db.close()


async def test_reconcile_sends_one_telegram_alert_per_incident(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = utc_now_naive()
        user = User(email="owner@test.local", role="ADMIN", context={"telegram_chat_id": "1234"})
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="zerg",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="offline",
            last_seen_at=now - timedelta(minutes=12),
            runner_metadata={"install_mode": "server", "capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        incident = RunnerHealthIncident(
            owner_id=user.id,
            runner_id=runner.id,
            incident_type="offline",
            status=OPEN_INCIDENT_STATUS,
            reason_code="stale_heartbeat",
            summary="Offline. Last heartbeat 720s ago.",
            opened_at=now - timedelta(minutes=6),
            last_observed_at=now - timedelta(minutes=1),
            context={},
        )
        db.add(incident)
        db.commit()

        send_telegram = AsyncMock(return_value=True)
        with (
            patch(
                "zerg.services.runner_health_reconciler.get_runner_connection_manager",
                return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: False),
            ),
            patch("zerg.services.runner_health_reconciler._send_telegram_alert", send_telegram),
            patch("zerg.services.runner_health_reconciler._send_email_alert", return_value=False),
        ):
            result_first = await reconcile_runner_health(db, now=now)
            result_second = await reconcile_runner_health(db, now=now + timedelta(minutes=1))

        db.refresh(incident)

        assert result_first["alerts_sent"] == 1
        assert result_second["alerts_sent"] == 0
        assert incident.alert_channel == "telegram"
        assert incident.alert_sent_at is not None
        send_telegram.assert_awaited_once()
    finally:
        db.close()


async def test_reconcile_enqueues_one_oikos_wakeup_for_prolonged_offline_runner(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = utc_now_naive()
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="clifford",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="offline",
            last_seen_at=now - timedelta(hours=1),
            runner_metadata={"install_mode": "server", "capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        incident = RunnerHealthIncident(
            owner_id=user.id,
            runner_id=runner.id,
            incident_type="offline",
            status=OPEN_INCIDENT_STATUS,
            reason_code="stale_heartbeat",
            summary="Offline. Last heartbeat 3600s ago.",
            opened_at=now - timedelta(minutes=31),
            last_observed_at=now - timedelta(minutes=1),
            context={},
        )
        db.add(incident)
        db.commit()

        invoke_oikos = AsyncMock(return_value=123)
        with (
            patch(
                "zerg.services.runner_health_reconciler.get_runner_connection_manager",
                return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: False),
            ),
            patch("zerg.services.runner_health_reconciler._send_telegram_alert", AsyncMock(return_value=False)),
            patch("zerg.services.runner_health_reconciler._send_email_alert", return_value=False),
            patch("zerg.services.runner_health_reconciler.get_operator_policy", return_value=SimpleNamespace(enabled=True)),
            patch("zerg.services.oikos_service.invoke_oikos", invoke_oikos),
        ):
            result_first = await reconcile_runner_health(db, now=now)
            result_second = await reconcile_runner_health(db, now=now + timedelta(minutes=1))

        db.refresh(incident)
        wakeups = db.query(OikosWakeup).all()

        assert result_first["wakeups_sent"] == 1
        assert result_second["wakeups_sent"] == 0
        assert incident.wakeup_sent_at is not None
        assert incident.wakeup_count == 1
        assert len(wakeups) == 1
        assert wakeups[0].status == "enqueued"
        invoke_oikos.assert_awaited_once()
    finally:
        db.close()


async def test_reconcile_resolves_open_incident_when_runner_returns_online(tmp_path: Path):
    db = _make_db(tmp_path)
    try:
        now = utc_now_naive()
        user = User(email="owner@test.local", role="ADMIN")
        db.add(user)
        db.commit()
        db.refresh(user)

        runner = Runner(
            owner_id=user.id,
            name="cinder",
            auth_secret_hash="hash",
            capabilities=["exec.full"],
            status="offline",
            last_seen_at=now,
            runner_metadata={"install_mode": "server", "capabilities": ["exec.full"], "heartbeat_interval_ms": 30000},
        )
        db.add(runner)
        db.commit()
        db.refresh(runner)

        incident = RunnerHealthIncident(
            owner_id=user.id,
            runner_id=runner.id,
            incident_type="offline",
            status=OPEN_INCIDENT_STATUS,
            reason_code="stale_heartbeat",
            summary="Offline. Last heartbeat 720s ago.",
            opened_at=now - timedelta(minutes=20),
            last_observed_at=now - timedelta(minutes=2),
            context={},
        )
        db.add(incident)
        db.commit()

        with patch(
            "zerg.services.runner_health_reconciler.get_runner_connection_manager",
            return_value=SimpleNamespace(is_online=lambda owner_id, runner_id: True),
        ):
            result = await reconcile_runner_health(db, now=now)

        db.refresh(runner)
        db.refresh(incident)

        assert result["incidents_resolved"] == 1
        assert runner.status == "online"
        assert incident.status == RESOLVED_INCIDENT_STATUS
        assert incident.resolved_at is not None
    finally:
        db.close()
