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
from zerg.models.user import User


def _make_db(tmp_path, name: str = "operator_sweep.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return engine, make_sessionmaker(engine)


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    job_registry.unregister(JOB_ID)


@pytest.mark.asyncio
async def test_operator_sweep_skips_when_operator_mode_disabled(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_disabled.db")
    calls = []

    @contextmanager
    def override_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.delenv("OIKOS_OPERATOR_MODE_ENABLED", raising=False)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", override_db_session)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "operator mode disabled"}
    assert calls == []


@pytest.mark.asyncio
async def test_operator_sweep_skips_without_owner(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_no_owner.db")
    calls = []

    @contextmanager
    def override_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", override_db_session)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "no owner"}
    assert calls == []


@pytest.mark.asyncio
async def test_operator_sweep_invokes_oikos_when_enabled(monkeypatch, tmp_path):
    engine, SessionLocal = _make_db(tmp_path, "operator_sweep_enabled.db")
    calls = []

    with SessionLocal() as db:
        db.add(User(id=7, email="owner@test.local", role="ADMIN"))
        db.commit()

    @contextmanager
    def override_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

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
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", override_db_session)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

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

    @contextmanager
    def override_db_session():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    async def fake_invoke_oikos(*args, **kwargs):
        calls.append((args, kwargs))
        return 123

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.db_session", override_db_session)
    monkeypatch.setattr("zerg.jobs.oikos_operator_sweep.invoke_oikos", fake_invoke_oikos)

    result = await run()

    engine.dispose()

    assert result == {"status": "skipped", "reason": "operator mode disabled"}
    assert calls == []
