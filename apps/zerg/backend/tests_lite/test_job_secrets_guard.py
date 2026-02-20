"""Tests for _has_missing_required_secrets guard in commis.py.

Covers:
- enqueue_scheduled_run skips when required secrets missing
- enqueue_scheduled_run proceeds when secrets present (via env var)
- enqueue_missed_runs (backfill) also skips when required secrets missing
- enqueue_missed_runs proceeds when secrets present (via env var)
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.jobs.registry import JobConfig, job_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_job(job_id, secrets, *, enabled=True):
    async def _noop():
        return {"ok": True}

    config = JobConfig(
        id=job_id,
        cron="0 * * * *",
        func=_noop,
        enabled=enabled,
        secrets=secrets,
        description="Test",
    )
    job_registry.unregister(job_id)
    job_registry.register(config)
    return config


def _cleanup(*job_ids):
    for jid in job_ids:
        job_registry.unregister(jid)


def _make_factory(tmp_path, name):
    from zerg.database import make_engine, make_sessionmaker
    from zerg.models.agents import AgentsBase
    from zerg.models.models import Base

    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    AgentsBase.metadata.create_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _mock_trigger(fire_time):
    """Return a mock CronTrigger that fires once at fire_time then stops."""
    t = MagicMock()
    t.get_next_fire_time.side_effect = [fire_time, None]
    return t


# ---------------------------------------------------------------------------
# Tests: enqueue_scheduled_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_scheduled_run_skips_when_secrets_missing(tmp_path):
    """enqueue_scheduled_run does not call enqueue_job when required secrets absent."""
    job_id = "guard-test-sched-skip"
    _register_job(job_id, secrets=["REQUIRED_KEY_SKIP"])
    factory = _make_factory(tmp_path, "sched_skip.db")

    try:
        with patch("zerg.database.get_session_factory", return_value=factory):
            with patch("zerg.jobs.commis.enqueue_job", new_callable=AsyncMock) as mock_enqueue:
                from zerg.jobs.commis import enqueue_scheduled_run

                # Pass scheduled_at to bypass CronTrigger
                await enqueue_scheduled_run(job_id, scheduled_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                mock_enqueue.assert_not_called()
    finally:
        _cleanup(job_id)


@pytest.mark.asyncio
async def test_enqueue_scheduled_run_proceeds_when_secrets_present(tmp_path):
    """enqueue_scheduled_run calls enqueue_job when required secrets are in env."""
    job_id = "guard-test-sched-ok"
    _register_job(job_id, secrets=["PRESENT_KEY_OK"])
    factory = _make_factory(tmp_path, "sched_ok.db")

    try:
        with patch.dict(os.environ, {"PRESENT_KEY_OK": "somevalue"}):
            with patch("zerg.database.get_session_factory", return_value=factory):
                with patch("zerg.jobs.commis.enqueue_job", new_callable=AsyncMock, return_value="q1") as mock_enqueue:
                    from zerg.jobs.commis import enqueue_scheduled_run

                    await enqueue_scheduled_run(job_id, scheduled_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                    mock_enqueue.assert_called_once()
    finally:
        _cleanup(job_id)


# ---------------------------------------------------------------------------
# Tests: enqueue_missed_runs (backfill)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_missed_runs_skips_when_secrets_missing(tmp_path):
    """Backfill does not enqueue jobs with missing required secrets."""
    job_id = "guard-test-backfill-skip"
    _register_job(job_id, secrets=["BACKFILL_SECRET_SKIP"], enabled=True)
    factory = _make_factory(tmp_path, "backfill_skip.db")

    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fire_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    try:
        with patch("zerg.jobs.commis.is_job_queue_db_enabled", return_value=True):
            with patch("zerg.jobs.commis.cleanup_zombies", new_callable=AsyncMock, return_value=0):
                with patch("zerg.jobs.commis.get_last_scheduled_for", new_callable=AsyncMock, return_value=past):
                    with patch("apscheduler.triggers.cron.CronTrigger") as mock_cron_cls:
                        mock_cron_cls.from_crontab.return_value = _mock_trigger(fire_time)
                        with patch("zerg.database.get_session_factory", return_value=factory):
                            with patch("zerg.jobs.commis.enqueue_job", new_callable=AsyncMock) as mock_enqueue:
                                from zerg.jobs.commis import enqueue_missed_runs

                                await enqueue_missed_runs(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
                                mock_enqueue.assert_not_called()
    finally:
        _cleanup(job_id)


@pytest.mark.asyncio
async def test_enqueue_missed_runs_proceeds_when_secrets_present(tmp_path):
    """Backfill enqueues when all required secrets are available via env."""
    job_id = "guard-test-backfill-ok"
    _register_job(job_id, secrets=["BACKFILL_OK_SECRET_PRESENT"], enabled=True)
    factory = _make_factory(tmp_path, "backfill_ok.db")

    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fire_time = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    try:
        with patch.dict(os.environ, {"BACKFILL_OK_SECRET_PRESENT": "val"}):
            with patch("zerg.jobs.commis.is_job_queue_db_enabled", return_value=True):
                with patch("zerg.jobs.commis.cleanup_zombies", new_callable=AsyncMock, return_value=0):
                    with patch("zerg.jobs.commis.get_last_scheduled_for", new_callable=AsyncMock, return_value=past):
                        with patch("apscheduler.triggers.cron.CronTrigger") as mock_cron_cls:
                            mock_cron_cls.from_crontab.return_value = _mock_trigger(fire_time)
                            with patch("zerg.database.get_session_factory", return_value=factory):
                                with patch(
                                    "zerg.jobs.commis.enqueue_job", new_callable=AsyncMock, return_value="q1"
                                ) as mock_enqueue:
                                    from zerg.jobs.commis import enqueue_missed_runs

                                    await enqueue_missed_runs(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
                                    mock_enqueue.assert_called_once()
    finally:
        _cleanup(job_id)
