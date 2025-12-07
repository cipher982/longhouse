import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from zerg.events import EventType, event_bus
from zerg.models.models import WorkerJob
import zerg.services.roundabout_monitor as rm
from zerg.services.roundabout_monitor import (
    DecisionContext,
    RoundaboutDecision,
    RoundaboutMonitor,
    ToolActivity,
    make_heuristic_decision,
)
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


# ============================================================================
# Phase 4: Heuristic Decision Tests
# ============================================================================


class TestMakeHeuristicDecision:
    """Tests for the heuristic decision function."""

    def _make_context(self, **overrides) -> DecisionContext:
        """Helper to create a DecisionContext with defaults."""
        defaults = {
            "job_id": 1,
            "worker_id": "test-worker-123",
            "task": "Test task",
            "status": "running",
            "elapsed_seconds": 10.0,
            "tool_activities": [],
            "current_operation": None,
            "is_stuck": False,
            "stuck_seconds": 0.0,
            "polls_without_progress": 0,
            "last_tool_output": None,
        }
        defaults.update(overrides)
        return DecisionContext(**defaults)

    def test_wait_when_running_normally(self):
        """Should return WAIT when worker is running normally."""
        ctx = self._make_context(status="running")
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.WAIT
        assert "Continuing" in reason

    def test_exit_when_status_success(self):
        """Should return EXIT when worker status is success."""
        ctx = self._make_context(status="success")
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.EXIT
        assert "success" in reason

    def test_exit_when_status_failed(self):
        """Should return EXIT when worker status is failed."""
        ctx = self._make_context(status="failed")
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.EXIT
        assert "failed" in reason

    def test_exit_on_final_answer_pattern(self):
        """Should return EXIT when final answer pattern detected in output."""
        ctx = self._make_context(
            status="running", last_tool_output="Result: The disk is 78% full."
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.EXIT
        assert "pattern" in reason.lower()

    def test_cancel_when_stuck_too_long(self):
        """Should return CANCEL when stuck beyond threshold."""
        ctx = self._make_context(
            status="running",
            is_stuck=True,
            stuck_seconds=65.0,  # Beyond 60s threshold
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.CANCEL
        assert "stuck" in reason.lower()

    def test_cancel_when_no_progress(self):
        """Should return CANCEL after too many polls without progress."""
        ctx = self._make_context(
            status="running",
            polls_without_progress=7,  # Beyond 6 poll threshold
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.CANCEL
        assert "progress" in reason.lower()

    def test_wait_when_stuck_but_under_threshold(self):
        """Should still WAIT when stuck but under cancel threshold."""
        ctx = self._make_context(
            status="running",
            is_stuck=True,
            stuck_seconds=40.0,  # Under 60s threshold
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.WAIT


@pytest.mark.asyncio
async def test_roundabout_cancels_on_no_progress(monkeypatch, db_session, tmp_path):
    """Monitor should cancel when no progress for many polls."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setattr(rm, "ROUNDABOUT_NO_PROGRESS_POLLS", 3)  # Lower for fast test
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = deepcopy(event_bus._subscribers)
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Stuck task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Stuck task",
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
            timeout_seconds=2,  # Higher than we need
        )

        result = await monitor.wait_for_completion()

        # Should be cancelled due to no progress
        assert result.status == "cancelled"
        assert result.decision == RoundaboutDecision.CANCEL
        assert "progress" in result.activity_summary.get("cancel_reason", "").lower()
    finally:
        event_bus._subscribers = original_subs
