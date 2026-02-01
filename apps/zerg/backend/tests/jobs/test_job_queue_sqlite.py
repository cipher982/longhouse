from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from zerg.jobs.queue import claim_job_by_id
from zerg.jobs.queue import claim_next_job
from zerg.jobs.queue import complete_job
from zerg.jobs.queue import default_owner
from zerg.jobs.queue import enqueue_job
from zerg.jobs.queue import extend_lease
from zerg.jobs.queue import reschedule_job


def _set_queue_db(monkeypatch: pytest.MonkeyPatch, path) -> None:
    db_url = f"sqlite:///{path}"
    monkeypatch.setenv("JOB_QUEUE_DB_URL", db_url)


@pytest.mark.asyncio
async def test_enqueue_claim_complete(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_queue_db(monkeypatch, tmp_path / "queue-basic.db")

    scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=5)
    queue_id = await enqueue_job("job-basic", scheduled_for)
    assert queue_id is not None

    owner = default_owner()
    claimed = await claim_next_job(owner)
    assert claimed is not None
    assert claimed.id == queue_id
    assert claimed.attempts == 1

    ok = await complete_job(claimed.id, "success", owner=owner)
    assert ok is True


@pytest.mark.asyncio
async def test_dedupe_prevents_duplicate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_queue_db(monkeypatch, tmp_path / "queue-dedupe.db")

    scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=5)
    queue_id = await enqueue_job("job-dedupe", scheduled_for, dedupe_key="job-dedupe:1")
    assert queue_id is not None

    duplicate = await enqueue_job("job-dedupe", scheduled_for, dedupe_key="job-dedupe:1")
    assert duplicate is None


@pytest.mark.asyncio
async def test_reschedule_and_extend_lease(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_queue_db(monkeypatch, tmp_path / "queue-retry.db")

    scheduled_for = datetime.now(timezone.utc) - timedelta(seconds=5)
    queue_id = await enqueue_job("job-retry", scheduled_for)
    assert queue_id is not None

    owner = default_owner()
    claimed = await claim_job_by_id(queue_id, owner)
    assert claimed is not None
    assert claimed.attempts == 1

    ok = await extend_lease(queue_id, owner, 60)
    assert ok is True

    retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    ok = await reschedule_job(queue_id, retry_at, error="boom", owner=owner)
    assert ok is True

    claimed_again = await claim_job_by_id(queue_id, owner)
    assert claimed_again is not None
    assert claimed_again.attempts == 2
