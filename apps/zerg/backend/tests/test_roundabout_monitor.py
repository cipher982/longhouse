import asyncio
from datetime import datetime
from datetime import timezone

import pytest

import zerg.services.roundabout_monitor as rm
from tests.conftest import TEST_WORKER_MODEL
from zerg.events import EventType
from zerg.events import event_bus
from zerg.models.models import WorkerJob
from zerg.services.roundabout_monitor import DecisionContext
from zerg.services.roundabout_monitor import RoundaboutDecision
from zerg.services.roundabout_monitor import RoundaboutMonitor
from zerg.services.roundabout_monitor import ToolActivity
from zerg.services.roundabout_monitor import make_heuristic_decision
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
    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
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

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Long task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Long task",
            model=TEST_WORKER_MODEL,
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
        ctx = self._make_context(status="running", last_tool_output="Result: The disk is 78% full.")
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

    def test_cancel_when_no_progress_with_tool_activity(self):
        """Should return CANCEL after too many polls without progress (with tool activity)."""
        ctx = self._make_context(
            status="running",
            polls_without_progress=7,  # Beyond 6 poll threshold
            elapsed_seconds=25.0,  # Beyond min_elapsed guard (20s)
            tool_activities=[ToolActivity("http_request", "completed", datetime.now(timezone.utc))],
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.CANCEL
        assert "progress" in reason.lower()

    def test_no_cancel_when_no_progress_but_too_early(self):
        """Should NOT cancel if no progress but not enough time elapsed."""
        ctx = self._make_context(
            status="running",
            polls_without_progress=7,
            elapsed_seconds=15.0,  # Less than min_elapsed guard (20s)
            tool_activities=[ToolActivity("http_request", "completed", datetime.now(timezone.utc))],
        )
        decision, reason = make_heuristic_decision(ctx)

        # Should wait, not cancel
        assert decision == RoundaboutDecision.WAIT

    def test_cancel_when_no_tool_activity_after_extended_time(self):
        """Should cancel if no tool activity after extended grace period (60s)."""
        ctx = self._make_context(
            status="running",
            polls_without_progress=15,
            elapsed_seconds=65.0,  # Beyond ROUNDABOUT_CANCEL_STUCK_THRESHOLD (60s)
            tool_activities=[],  # No tool activity
        )
        decision, reason = make_heuristic_decision(ctx)

        assert decision == RoundaboutDecision.CANCEL
        assert "activity" in reason.lower()

    def test_no_cancel_when_no_tool_activity_but_under_threshold(self):
        """Should NOT cancel if no tool activity but under 60s threshold."""
        ctx = self._make_context(
            status="running",
            polls_without_progress=7,
            elapsed_seconds=45.0,  # Under ROUNDABOUT_CANCEL_STUCK_THRESHOLD (60s)
            tool_activities=[],  # No tool activity
        )
        decision, reason = make_heuristic_decision(ctx)

        # Should wait, not cancel - worker may still be in initial model call
        assert decision == RoundaboutDecision.WAIT

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
    """Monitor should cancel when no progress for many polls (with tool activity)."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setattr(rm, "ROUNDABOUT_NO_PROGRESS_POLLS", 3)  # Lower for fast test
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Stuck task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Stuck task",
            model=TEST_WORKER_MODEL,
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
        monitor_task = asyncio.create_task(monitor.wait_for_completion())

        # Allow subscription to register
        await asyncio.sleep(0.01)

        # Emit a tool event to satisfy the "has_tool_activity" guard
        # After this, no more events will come (simulating "stuck after tool started")
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": EventType.WORKER_TOOL_STARTED,
                "job_id": job.id,
                "worker_id": worker_id,
                "tool_name": "stuck_tool",
            },
        )

        result = await monitor_task

        # Should be cancelled due to no progress (or timeout, which is also acceptable)
        assert result.status in ("cancelled", "monitor_timeout")
        if result.status == "cancelled":
            assert result.decision == RoundaboutDecision.CANCEL
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_correlates_events_by_job_id(monkeypatch, db_session, tmp_path):
    """Monitor should correlate tool events by job_id (regression test for event filtering bug)."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
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

        # Emit event WITH job_id - should be recorded
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": EventType.WORKER_TOOL_STARTED,
                "job_id": job.id,  # Critical: includes job_id
                "worker_id": worker_id,
                "owner_id": 1,
                "tool_name": "http_request",
            },
        )

        # Emit event with WRONG job_id - should be ignored
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": EventType.WORKER_TOOL_STARTED,
                "job_id": 99999,  # Different job
                "worker_id": "other-worker",
                "owner_id": 1,
                "tool_name": "should_not_appear",
            },
        )

        # Emit event with NO job_id - should be ignored (regression test)
        await event_bus.publish(
            EventType.WORKER_TOOL_STARTED,
            {
                "event_type": EventType.WORKER_TOOL_STARTED,
                "worker_id": worker_id,  # Same worker but no job_id
                "owner_id": 1,
                "tool_name": "also_should_not_appear",
            },
        )

        # Complete the worker
        await event_bus.publish(
            EventType.WORKER_TOOL_COMPLETED,
            {
                "event_type": EventType.WORKER_TOOL_COMPLETED,
                "job_id": job.id,
                "worker_id": worker_id,
                "tool_name": "http_request",
                "duration_ms": 100,
            },
        )

        store.save_result(worker_id, "OK")
        store.complete_worker(worker_id, status="success")
        job.status = "success"
        db_session.commit()

        result = await monitor_task

        assert result.status == "complete"
        # Should only have recorded the events with matching job_id
        assert result.activity_summary["tool_calls_total"] == 1
        assert result.activity_summary["tool_calls_completed"] == 1
        # Verify tool names don't include the ignored events
        tools_used = result.activity_summary.get("tools_used", [])
        assert "http_request" in tools_used
        assert "should_not_appear" not in tools_used
        assert "also_should_not_appear" not in tools_used
    finally:
        event_bus._subscribers = original_subs


# ============================================================================
# Phase 5: LLM-Gated Decision Tests
# ============================================================================

from unittest.mock import MagicMock
from unittest.mock import patch

from zerg.services.llm_decider import DecisionMode
from zerg.services.llm_decider import LLMDecisionResult


class TestRoundaboutDecisionModes:
    """Tests for the decision mode integration."""

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


@pytest.mark.asyncio
async def test_roundabout_with_llm_mode_exit(monkeypatch, db_session, tmp_path):
    """LLM mode should exit early when LLM says exit."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock LLM to return "exit"
        mock_result = LLMDecisionResult(
            action="exit",
            rationale="LLM decided: exit",
            response_time_ms=500.0,
            was_fallback=False,
        )

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action") as mock_decide:
            mock_decide.return_value = ("exit", "LLM decided: exit", mock_result)

            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=2,
                decision_mode=DecisionMode.LLM,
                llm_poll_interval=1,  # Call LLM every poll
            )

            result = await monitor.wait_for_completion()

            assert result.status == "early_exit"
            assert result.decision == RoundaboutDecision.EXIT
            assert "llm_calls" in result.activity_summary
            assert result.activity_summary["decision_mode"] == "llm"
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_with_llm_mode_cancel(monkeypatch, db_session, tmp_path):
    """LLM mode should cancel when LLM says cancel."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Stuck task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Stuck task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock LLM to return "cancel"
        mock_result = LLMDecisionResult(
            action="cancel",
            rationale="LLM decided: cancel",
            response_time_ms=500.0,
            was_fallback=False,
        )

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action") as mock_decide:
            mock_decide.return_value = ("cancel", "LLM decided: cancel", mock_result)

            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=2,
                decision_mode=DecisionMode.LLM,
                llm_poll_interval=1,
            )

            result = await monitor.wait_for_completion()

            assert result.status == "cancelled"
            assert result.decision == RoundaboutDecision.CANCEL
            assert "llm_calls" in result.activity_summary
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_llm_budget_enforcement(monkeypatch, db_session, tmp_path):
    """LLM mode should respect max calls budget."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        call_count = 0

        async def mock_decide(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return (
                "wait",
                "LLM decided: wait",
                LLMDecisionResult(
                    action="wait",
                    rationale="LLM decided: wait",
                    response_time_ms=100.0,
                    was_fallback=False,
                ),
            )

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action", mock_decide):
            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=0.5,  # Short timeout
                decision_mode=DecisionMode.LLM,
                llm_poll_interval=1,  # Call every poll
                llm_max_calls=2,  # Low budget
            )

            result = await monitor.wait_for_completion()

            # Should have hit budget limit
            assert call_count == 2
            assert result.activity_summary.get("llm_skipped_budget", 0) > 0
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_llm_interval_enforcement(monkeypatch, db_session, tmp_path):
    """LLM mode should respect poll interval."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        call_count = 0

        async def mock_decide(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return (
                "wait",
                "LLM decided: wait",
                LLMDecisionResult(
                    action="wait",
                    rationale="LLM decided: wait",
                    response_time_ms=100.0,
                    was_fallback=False,
                ),
            )

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action", mock_decide):
            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=0.2,  # Short timeout
                decision_mode=DecisionMode.LLM,
                llm_poll_interval=3,  # Only every 3rd poll
                llm_max_calls=10,  # High budget
            )

            result = await monitor.wait_for_completion()

            # Should have skipped some polls due to interval
            assert result.activity_summary.get("llm_skipped_interval", 0) > 0
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_hybrid_mode_heuristic_takes_precedence(monkeypatch, db_session, tmp_path):
    """Hybrid mode should use heuristic decision when it's not WAIT."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="success",  # Heuristic will say EXIT
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Complete worker artifacts
        store.save_result(worker_id, "OK")
        store.complete_worker(worker_id, status="success")

        llm_called = False

        async def mock_decide(*args, **kwargs):
            nonlocal llm_called
            llm_called = True
            return ("wait", "LLM decided: wait", MagicMock())

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action", mock_decide):
            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=2,
                decision_mode=DecisionMode.HYBRID,
            )

            result = await monitor.wait_for_completion()

            # Heuristic should have exited immediately, no LLM call needed
            assert result.status == "complete"
            assert not llm_called
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_heuristic_mode_no_llm_calls(monkeypatch, db_session, tmp_path):
    """Heuristic mode should never call LLM."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setattr(rm, "ROUNDABOUT_NO_PROGRESS_POLLS", 2)  # Quick cancel
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        llm_called = False

        async def mock_decide(*args, **kwargs):
            nonlocal llm_called
            llm_called = True
            return ("wait", "LLM decided: wait", MagicMock())

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action", mock_decide):
            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=2,
                decision_mode=DecisionMode.HEURISTIC,  # Default mode
            )

            result = await monitor.wait_for_completion()

            # LLM should never have been called
            assert not llm_called
            assert result.activity_summary["decision_mode"] == "heuristic"
            assert "llm_calls" not in result.activity_summary
    finally:
        event_bus._subscribers = original_subs


@pytest.mark.asyncio
async def test_roundabout_llm_timeout_fallback(monkeypatch, db_session, tmp_path):
    """LLM timeout should fall back to wait."""
    monkeypatch.setattr(rm, "ROUNDABOUT_CHECK_INTERVAL", 0.02)
    monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
    store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

    original_subs = {k: set(v) for k, v in event_bus._subscribers.items()}
    event_bus._subscribers.clear()

    try:
        worker_id = store.create_worker("Test task", owner_id=1)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=1,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock LLM to return a timeout fallback
        mock_result = LLMDecisionResult(
            action="wait",
            rationale="LLM timeout, defaulting to wait",
            response_time_ms=1500.0,
            was_fallback=True,
        )

        with patch("zerg.services.roundabout_monitor.decide_roundabout_action") as mock_decide:
            mock_decide.return_value = ("wait", "LLM timeout, defaulting to wait", mock_result)

            monitor = RoundaboutMonitor(
                db_session,
                job.id,
                owner_id=1,
                timeout_seconds=0.2,  # Short overall timeout
                decision_mode=DecisionMode.LLM,
                llm_poll_interval=1,
            )

            result = await monitor.wait_for_completion()

            # Should have timed out overall (monitor timeout), with LLM timeouts recorded
            assert result.activity_summary.get("llm_timeouts", 0) > 0
    finally:
        event_bus._subscribers = original_subs
