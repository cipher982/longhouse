"""Tests for the periodic Oikos operator sweep job."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.jobs.oikos_operator_sweep import JOB_ID
from zerg.jobs.oikos_operator_sweep import run
from zerg.jobs.registry import job_registry
from zerg.models.agents import AgentsBase
from zerg.models.user import User
from zerg.models.work import OikosWakeup


def _make_db(tmp_path, name: str = "operator_sweep.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


def _make_override_db_session(SessionLocal):
    @contextmanager
    def override_db_session():
        db = SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return override_db_session


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    job_registry.unregister(JOB_ID)


@pytest.mark.asyncio
async def test_operator_sweep_skips_when_operator_mode_disabled(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_disabled.db")
    calls = []

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.delenv("OIKOS_OPERATOR_MODE_ENABLED", raising=False)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", _make_override_db_session(SessionLocal))
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    with SessionLocal() as db:
        wakeups = db.query(OikosWakeup).all()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "operator mode disabled"}
    assert calls == []
    assert wakeups == []


@pytest.mark.asyncio
async def test_operator_sweep_skips_without_owner(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_no_owner.db")
    calls = []

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", _make_override_db_session(SessionLocal))
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    with SessionLocal() as db:
        wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id).all()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "no owner"}
    assert calls == []
    assert len(wakeups) == 1
    assert wakeups[0].status == "suppressed"
    assert wakeups[0].reason == "no_owner"


@pytest.mark.asyncio
async def test_operator_sweep_invokes_oikos_when_enabled(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_enabled.db")
    calls = []

    with SessionLocal() as db:
        db.add(User(id=7, email="owner@test.local", role="ADMIN"))
        db.commit()

    async def fake_invoke_oikos(owner_id, message, message_id, **kwargs):
        calls.append(
            {
                "owner_id": owner_id,
                "message": message,
                "message_id": message_id,
                **kwargs,
            }
        )
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", _make_override_db_session(SessionLocal))
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    with SessionLocal() as db:
        wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id).all()

    engine.dispose()

    assert result == {
        "status": "enqueued",
        "owner_id": 7,
        "trigger_type": "periodic_sweep",
    }
    assert len(calls) == 1
    assert calls[0]["owner_id"] == 7
    assert calls[0]["source"] == "operator"
    assert calls[0]["surface_adapter"].surface_id == "operator"
    assert calls[0]["surface_payload"]["trigger_type"] == "periodic_sweep"
    assert calls[0]["surface_payload"]["conversation_id"] == "operator:sweep"
    assert "Trigger: periodic_sweep" in calls[0]["message"]
    assert len(wakeups) == 1
    assert wakeups[0].status == "enqueued"
    assert wakeups[0].run_id == 123
    assert wakeups[0].trigger_type == "periodic_sweep"


@pytest.mark.asyncio
async def test_operator_sweep_skips_when_user_policy_disables_it(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_policy_disabled.db")
    calls = []

    with SessionLocal() as db:
        db.add(
            User(
                id=7,
                email="owner@test.local",
                role="ADMIN",
                context={"preferences": {"operator_mode": {"enabled": False}}},
            )
        )
        db.commit()

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", _make_override_db_session(SessionLocal))
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    with SessionLocal() as db:
        wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id).all()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "operator mode disabled"}
    assert calls == []
    assert len(wakeups) == 1
    assert wakeups[0].status == "suppressed"
    assert wakeups[0].reason == "user_policy_disabled"
