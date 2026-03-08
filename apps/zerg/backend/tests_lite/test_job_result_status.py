"""Tests for scheduler status derivation from returned job payloads."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.jobs.commis import _run_job
from zerg.jobs.queue import QueueJob
from zerg.jobs.queue import QueueOwner
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import interpret_job_result
from zerg.jobs.registry import job_registry


def _register_test_job(job_id: str, *, max_attempts: int = 1) -> None:
    async def _noop():
        return {"ok": True}

    config = JobConfig(
        id=job_id,
        cron="0 * * * *",
        func=_noop,
        enabled=True,
        max_attempts=max_attempts,
        timeout_seconds=30,
        description="job status test",
    )
    job_registry.unregister(job_id)
    job_registry.register(config)


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    job_registry.unregister("job-status-degraded")
    job_registry.unregister("job-status-failure")


def test_interpret_job_result_promotes_pipeline_summary_errors():
    status, error, error_type = interpret_job_result(
        {
            "status": "success",
            "pipeline_summaries": [
                {
                    "status": "error",
                    "error_type": "PartialFailure",
                    "error_note": "1 editorial reviews failed",
                }
            ],
        }
    )

    assert status == "degraded"
    assert error == "1 editorial reviews failed"
    assert error_type == "PartialFailure"


@pytest.mark.asyncio
async def test_run_job_returns_degraded_for_non_success_pipeline_summary(monkeypatch):
    job_id = "job-status-degraded"
    _register_test_job(job_id)

    async def fake_invoke(_config):
        return {
            "status": "success",
            "pipeline_summaries": [
                {
                    "status": "error",
                    "error_type": "PartialFailure",
                    "error_note": "1 editorial reviews failed",
                }
            ],
        }

    monkeypatch.setattr("zerg.jobs.registry._invoke_job_func", fake_invoke)

    result = await job_registry.run_job(job_id)

    assert result.status == "degraded"
    assert result.error == "1 editorial reviews failed"
    assert result.error_type == "PartialFailure"


@pytest.mark.asyncio
async def test_run_job_treats_reported_error_status_as_failure(monkeypatch):
    job_id = "job-status-failure"
    _register_test_job(job_id)

    async def fake_invoke(_config):
        return {"status": "error", "error": "reported failure", "error_type": "ReportedFailure"}

    monkeypatch.setattr("zerg.jobs.registry._invoke_job_func", fake_invoke)
    monkeypatch.setattr("zerg.jobs.registry.asyncio.sleep", AsyncMock())

    result = await job_registry.run_job(job_id)

    assert result.status == "failure"
    assert result.error_type == "ReportedFailure"
    assert "reported failure" in (result.error or "")


@pytest.mark.asyncio
async def test_commis_run_job_persists_degraded_but_completes_queue_success(monkeypatch):
    queue_job = QueueJob(
        id="q1",
        job_id="editorial-job",
        scheduled_for=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        attempts=1,
        max_attempts=3,
        status="running",
    )
    owner = QueueOwner(name="test-owner", pid=123)
    job_def = SimpleNamespace(
        id="editorial-job",
        enabled=True,
        timeout_seconds=30,
        tags=["ai-tools"],
        project="ai-tools",
        func=lambda: None,
    )
    emitted = {}

    async def fake_emit_job_run(**kwargs):
        emitted.update(kwargs)

    monkeypatch.setattr("zerg.jobs.commis.extend_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.complete_job", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.reschedule_job", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.emit_job_run", fake_emit_job_run)
    monkeypatch.setattr("zerg.jobs.commis.get_scheduler_name", lambda: "sched-test")
    monkeypatch.setattr("zerg.jobs.commis.job_registry.get", lambda _job_id: job_def)
    monkeypatch.setattr("zerg.jobs.loader.get_manifest_metadata", lambda _job_id: None)

    async def fake_invoke(_job_def):
        return {
            "status": "success",
            "pipeline_summaries": [
                {
                    "status": "error",
                    "error_type": "PartialFailure",
                    "error_note": "1 editorial reviews failed",
                }
            ],
        }

    monkeypatch.setattr("zerg.jobs.commis._invoke_job", fake_invoke)

    await _run_job(queue_job, owner)

    assert emitted["status"] == "degraded"
    assert emitted["error_message"] == "1 editorial reviews failed"
    zerg_complete = __import__("zerg.jobs.commis", fromlist=["complete_job"]).complete_job
    zerg_complete.assert_awaited_once_with("q1", "success", None, owner=owner)


@pytest.mark.asyncio
async def test_commis_run_job_reschedules_reported_failure(monkeypatch):
    queue_job = QueueJob(
        id="q2",
        job_id="editorial-job",
        scheduled_for=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        attempts=1,
        max_attempts=3,
        status="running",
    )
    owner = QueueOwner(name="test-owner", pid=123)
    job_def = SimpleNamespace(
        id="editorial-job",
        enabled=True,
        timeout_seconds=30,
        tags=["ai-tools"],
        project="ai-tools",
        func=lambda: None,
    )
    emitted = {}

    async def fake_emit_job_run(**kwargs):
        emitted.update(kwargs)

    monkeypatch.setattr("zerg.jobs.commis.extend_lease", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.complete_job", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.reschedule_job", AsyncMock(return_value=True))
    monkeypatch.setattr("zerg.jobs.commis.emit_job_run", fake_emit_job_run)
    monkeypatch.setattr("zerg.jobs.commis.get_scheduler_name", lambda: "sched-test")
    monkeypatch.setattr("zerg.jobs.commis.job_registry.get", lambda _job_id: job_def)
    monkeypatch.setattr("zerg.jobs.loader.get_manifest_metadata", lambda _job_id: None)

    async def fake_invoke(_job_def):
        return {"status": "error", "error": "reported failure", "error_type": "ReportedFailure"}

    monkeypatch.setattr("zerg.jobs.commis._invoke_job", fake_invoke)

    await _run_job(queue_job, owner)

    assert emitted["status"] == "failure"
    zerg_reschedule = __import__("zerg.jobs.commis", fromlist=["reschedule_job"]).reschedule_job
    zerg_reschedule.assert_awaited_once()
