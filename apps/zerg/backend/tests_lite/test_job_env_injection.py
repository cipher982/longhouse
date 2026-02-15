"""Tests for legacy job env injection from JobSecret."""

import os

import pytest

from zerg.database import Base, make_engine, make_sessionmaker
from zerg.jobs.registry import JobConfig, _invoke_job_func
from zerg.models.models import JobSecret, User
from zerg.utils.crypto import encrypt


def _make_db(tmp_path):
    db_path = tmp_path / "test_job_env.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


@pytest.mark.asyncio
async def test_legacy_env_injection_restores(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        db.add(User(id=1, email="dev@local", role="ADMIN"))
        db.add(JobSecret(owner_id=1, key="SECRET_A", encrypted_value=encrypt("value_a")))
        db.commit()

    monkeypatch.setattr("zerg.database.get_session_factory", lambda: SessionLocal)

    os.environ["SECRET_A"] = "old"

    def run():
        return {"seen": os.environ.get("SECRET_A")}

    config = JobConfig(
        id="legacy-env-job",
        cron="0 0 * * *",
        func=run,
        secrets=["SECRET_A"],
        description="Legacy env job",
    )

    result = await _invoke_job_func(config)
    assert result["seen"] == "value_a"
    assert os.environ["SECRET_A"] == "old"
