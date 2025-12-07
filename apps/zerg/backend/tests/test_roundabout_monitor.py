import asyncio
from copy import deepcopy

import pytest

from zerg.events import EventType, event_bus
from zerg.models.models import WorkerJob
import zerg.services.roundabout_monitor as rm
from zerg.services.roundabout_monitor import RoundaboutMonitor
from zerg.services.worker_artifact_store import WorkerArtifactStore


@pytest.mark.asyncio
async def test_roundabout_records_tool_events(monkeypatch, db_session, tmp_path):
    """Monitor should subscribe, record tool events, and unsubscribe on completion."""
    # Speed up polling
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)

    # Isolate worker artifacts
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    # Reset event bus subscribers for isolation
    original_subs = deepcopy(event_bus._subscribers)
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model="gpt-5-mini",
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        monitor = RoundaboutMonitor(db_session, job.id, owner_id=1, timeout_seconds=2)
        monitor_task = asyncio.create_task(monitor.wait_for_completion())

        # Allow subscription to register
        await asyncio.sleep(0.01)

        # Emit tool events
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {"event_type": EventType.WORKER_TOOL_STARTED, "job_id": job.id, "tool_name": "http_request"},
        )
        await event_bus.publish(
            EventType.WORKER_TOOL_COMPLETED,
            {
                "event_type": EventType.WORKER_TOOL_COMPLETED,
                "job_id": job.id,
                "tool_name": "http_request",
                "duration_ms": 1200,
            },
        )

        # Complete the worker/job
        store.save_result(worker_id, "OK")
        store.complete_worker(worker_id, status="success")
        job.status = "success"
        db_session.commit()

        result = await monitor_task

        assert result.status == "complete"
        assert result.activity_summary["tool_calls_total"] == 1
        assert result.activity_summary["tool_calls_completed"] == 1
        # Ensure we cleaned up subscription
        assert monitor._event_subscription is None
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_monitor_timeout_sets_flag(monkeypatch, db_session, tmp_path):
    """Monitor timeout should report monitor_timeout and note worker still running."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = deepcopy(event_bus._subscribers)
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Long task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Long task",
            model="gpt-5-mini",
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        monitor = RoundaboutMonitor(
            db_session,
            job.id,
            owner_id=1,
            timeout_seconds=0.1,  # force quick timeout
        )

        result = await monitor.wait_for_completion()

        assert result.status == "monitor_timeout"
        assert result.worker_still_running is True
        assert result.activity_summary["monitoring_checks"] >= 1
        assert monitor._event_subscription is None
    finally:
        event_bus._subscribers = original_subs
