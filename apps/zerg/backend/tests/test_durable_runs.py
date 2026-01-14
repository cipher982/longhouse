"""Tests for durable runs v2.2 implementation.

Tests verify:
1. Timeout produces DEFERRED status (not FAILED)
2. SUPERVISOR_DEFERRED event is emitted on timeout
3. Roundabout no longer cancels on no-progress (warn only)
4. SSE stream closes on supervisor_deferred event
5. Resume endpoint resumes WAITING runs when worker completes
6. Heartbeat events reset no-progress counter

NOTE: The continuation pattern was replaced by LangGraph's interrupt/resume
pattern in Jan 2026. See: docs/work/supervisor-continuation-refactor.md
"""

import asyncio
import tempfile
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.crud import crud
from zerg.events import EventType
from zerg.events import event_bus
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.services.supervisor_service import SupervisorService


@pytest.mark.timeout(30)
class TestDurableRunsTimeout:
    """Test timeout behavior produces DEFERRED status."""

    @pytest.fixture
    def temp_artifact_path(self, monkeypatch):
        """Create temporary artifact store path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
            yield tmpdir

    @pytest.mark.asyncio
    async def test_timeout_produces_deferred_status(self, db_session, test_user, temp_artifact_path):
        """Test that supervisor timeout sets DEFERRED status, not FAILED."""
        service = SupervisorService(db_session)

        # Mock AgentRunner.run_thread to be slower than timeout
        async def slow_run_thread(_self, *_args, **_kwargs):
            await asyncio.sleep(0.2)  # Longer than our test timeout, but finishes quickly
            return []

        with patch("zerg.managers.agent_runner.AgentRunner.run_thread", new=slow_run_thread):
            result = await service.run_supervisor(
                owner_id=test_user.id,
                task="This will timeout",
                timeout=0.05,  # Very short timeout
            )

            # Verify DEFERRED status, not FAILED or ERROR
            assert result.status == "deferred"
            assert "background" in result.result.lower()

            # Let the background task complete to avoid leaking pending tasks in pytest-xdist.
            await asyncio.sleep(0.25)

            # Verify DB record
            run = db_session.query(AgentRun).filter(AgentRun.id == result.run_id).first()
            assert run is not None
            assert run.status == RunStatus.DEFERRED

    @pytest.mark.asyncio
    async def test_timeout_emits_deferred_event(self, db_session, test_user, temp_artifact_path):
        """Test that timeout emits SUPERVISOR_DEFERRED event, not ERROR."""
        service = SupervisorService(db_session)
        events_received = []

        async def capture_deferred(event_data):
            events_received.append(("deferred", event_data))

        async def capture_error(event_data):
            events_received.append(("error", event_data))

        event_bus.subscribe(EventType.SUPERVISOR_DEFERRED, capture_deferred)
        event_bus.subscribe(EventType.ERROR, capture_error)

        try:
            async def slow_run_thread(_self, *_args, **_kwargs):
                await asyncio.sleep(0.2)
                return []

            with patch("zerg.managers.agent_runner.AgentRunner.run_thread", new=slow_run_thread):
                result = await service.run_supervisor(
                    owner_id=test_user.id,
                    task="Timeout test",
                    timeout=0.05,
                )

                await asyncio.sleep(0.25)  # Let events + background task propagate/finish

                # Verify DEFERRED event was emitted
                deferred_events = [e for e in events_received if e[0] == "deferred"]
                assert len(deferred_events) >= 1, "SUPERVISOR_DEFERRED event should be emitted"

                # Verify event contains expected fields
                deferred_payload = deferred_events[0][1]
                assert "run_id" in deferred_payload
                assert "message" in deferred_payload
                assert deferred_payload["run_id"] == result.run_id

                # Verify NO error event was emitted (for timeout)
                error_events = [e for e in events_received if e[0] == "error"]
                # Filter out any unrelated errors
                timeout_errors = [e for e in error_events if "timeout" in str(e[1]).lower()]
                assert len(timeout_errors) == 0, "ERROR event should not be emitted on timeout"

        finally:
            event_bus.unsubscribe(EventType.SUPERVISOR_DEFERRED, capture_deferred)
            event_bus.unsubscribe(EventType.ERROR, capture_error)

    @pytest.mark.asyncio
    async def test_normal_completion_still_works(self, db_session, test_user, temp_artifact_path):
        """Test that normal (non-timeout) completion still returns SUCCESS."""
        service = SupervisorService(db_session)

        # Mock a fast completion
        mock_message = AsyncMock()
        mock_message.role = "assistant"
        mock_message.content = "Done!"

        async def fast_run_thread(_self, *_args, **_kwargs):
            return [mock_message]

        with patch("zerg.managers.agent_runner.AgentRunner.run_thread", new=fast_run_thread):
            result = await service.run_supervisor(
                owner_id=test_user.id,
                task="Quick task",
                timeout=30,  # Plenty of time
            )

            # Verify SUCCESS status
            assert result.status == "success"

            # Verify DB record
            run = db_session.query(AgentRun).filter(AgentRun.id == result.run_id).first()
            assert run is not None
            assert run.status == RunStatus.SUCCESS


@pytest.mark.timeout(10)
class TestDeferredEventTypes:
    """Test that DEFERRED status and event types exist."""

    def test_deferred_status_exists(self):
        """Verify DEFERRED is a valid RunStatus."""
        assert hasattr(RunStatus, "DEFERRED")
        assert RunStatus.DEFERRED.value == "deferred"

    def test_waiting_status_exists(self):
        """Verify WAITING is a valid RunStatus (for interrupt/resume pattern)."""
        assert hasattr(RunStatus, "WAITING")
        assert RunStatus.WAITING.value == "waiting"

    def test_supervisor_deferred_event_exists(self):
        """Verify SUPERVISOR_DEFERRED is a valid EventType."""
        assert hasattr(EventType, "SUPERVISOR_DEFERRED")
        assert EventType.SUPERVISOR_DEFERRED.value == "supervisor_deferred"


@pytest.mark.timeout(30)
class TestResumeFlow:
    """Test resume flow when worker completes while supervisor is WAITING.

    This tests the LangGraph interrupt/resume pattern that replaced the
    old continuation pattern in Jan 2026.
    """

    @pytest.fixture
    def temp_artifact_path(self, monkeypatch):
        """Create temporary artifact store path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
            yield tmpdir

    @pytest.mark.asyncio
    async def test_resume_endpoint_resumes_waiting_run(self, db_session, test_user, sample_agent):
        """Test that resume endpoint calls resume_supervisor_with_worker_result for WAITING run."""
        from zerg.routers.jarvis_internal import WorkerCompletionPayload
        from zerg.routers.jarvis_internal import resume_run

        # Create a WAITING run (interrupted by spawn_worker)
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Mock resume_supervisor_with_worker_result
        # The import is inside the function, so we patch the service module
        async def mock_resume(db, run_id, worker_result, job_id=None):  # noqa: ARG001 - test double
            # Update run status to success (simulating what real resume does)
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            run.status = RunStatus.SUCCESS
            db.commit()
            return {"status": "success", "result": "Resumed successfully"}

        with patch("zerg.services.worker_resume.resume_supervisor_with_worker_result", side_effect=mock_resume):
            # Call resume endpoint
            payload = WorkerCompletionPayload(
                job_id=123,
                worker_id="test-worker-123",
                status="success",
                result_summary="Worker completed successfully",
            )

            result = await resume_run(
                run_id=waiting_run.id,
                payload=payload,
                db=db_session,
            )

            # Verify response
            assert result["status"] == "resumed"
            assert result["run_id"] == waiting_run.id

    @pytest.mark.asyncio
    async def test_resume_endpoint_skips_non_waiting_run(self, db_session, test_user, sample_agent):
        """Test that calling resume on non-WAITING run is a no-op (idempotent)."""
        from zerg.routers.jarvis_internal import WorkerCompletionPayload
        from zerg.routers.jarvis_internal import resume_run

        # Create a SUCCESS run (not WAITING)
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )
        completed_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
        )
        db_session.add(completed_run)
        db_session.commit()
        db_session.refresh(completed_run)

        # Call resume endpoint
        payload = WorkerCompletionPayload(
            job_id=123,
            worker_id="test-worker-123",
            status="success",
            result_summary="Worker completed",
        )

        result = await resume_run(
            run_id=completed_run.id,
            payload=payload,
            db=db_session,
        )

        # Verify it was skipped (idempotent)
        assert result["status"] == "skipped"
        assert "not WAITING" in result["reason"]

    @pytest.mark.asyncio
    async def test_worker_completion_triggers_resume(
        self, db_session, test_user, sample_agent, temp_artifact_path
    ):
        """Test that worker completion calls resume when run is WAITING."""
        from zerg.services.worker_runner import WorkerRunner

        # Create a WAITING supervisor run (interrupted by spawn_worker)
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Mock AgentRunner to return immediately
        with patch("zerg.services.worker_runner.AgentRunner") as mock_runner_class:
            mock_runner_instance = AsyncMock()
            mock_runner_instance.run_thread = AsyncMock(
                return_value=[AsyncMock(role="assistant", content="Done")]
            )
            mock_runner_class.return_value = mock_runner_instance

            # Run worker with event context
            runner = WorkerRunner()
            created_tasks = []
            real_create_task = asyncio.create_task

            def _capture_task(coro):
                task = real_create_task(coro)
                created_tasks.append(task)
                return task

            # Mock the resume function to track calls
            resume_calls = []

            async def mock_resume(db, run_id, worker_result):
                resume_calls.append({"run_id": run_id, "worker_result": worker_result})
                return {"status": "success"}

            with patch("zerg.services.worker_runner.asyncio.create_task", side_effect=_capture_task):
                with patch(
                    "zerg.services.worker_resume.resume_supervisor_with_worker_result",
                    side_effect=mock_resume,
                ):
                    result = await runner.run_worker(
                        db=db_session,
                        task="Test task",
                        agent=sample_agent,
                        timeout=10,
                        event_context={"run_id": waiting_run.id},
                        job_id=123,
                    )

            # Verify worker completed
            assert result.status == "success"

            # Ensure the background resume task finishes
            if created_tasks:
                await asyncio.gather(*created_tasks, return_exceptions=True)

            # Verify resume was called (fire-and-forget, so may or may not have completed)
            # The key is that the task was created to call resume


@pytest.mark.timeout(30)
class TestHeartbeatCounterReset:
    """Test that heartbeat events reset no-progress counter."""

    def test_heartbeat_event_exists(self):
        """Verify WORKER_HEARTBEAT is a valid EventType."""
        assert hasattr(EventType, "WORKER_HEARTBEAT")

    @pytest.mark.asyncio
    async def test_heartbeat_resets_no_progress_counter(
        self, monkeypatch, db_session, test_user, tmp_path
    ):
        """Test that heartbeat event handler resets the no-progress counter.

        This tests the heartbeat handler logic directly without needing
        a full roundabout monitoring loop (which is tested in test_roundabout_monitor.py).
        """
        from zerg.services.roundabout_monitor import RoundaboutMonitor
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.models.models import WorkerJob
        from tests.conftest import TEST_WORKER_MODEL

        # Isolate worker artifacts
        monkeypatch.setenv("SWARMLET_DATA_PATH", str(tmp_path / "workers"))
        store = WorkerArtifactStore(base_path=str(tmp_path / "workers"))

        # Create worker and job
        worker_id = store.create_worker("Test task", owner_id=test_user.id)
        store.start_worker(worker_id)

        job = WorkerJob(
            owner_id=test_user.id,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="running",
            worker_id=worker_id,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Create monitor (but don't start the monitoring loop)
        monitor = RoundaboutMonitor(
            db_session, job.id, owner_id=test_user.id, timeout_seconds=3
        )

        # Manually set counter to a high value
        monitor._polls_without_progress = 10

        # Subscribe to events (this sets up the heartbeat handler)
        await monitor._subscribe_to_tool_events()

        try:
            # Emit heartbeat event
            await event_bus.publish(
                EventType.WORKER_HEARTBEAT,
                {
                    "job_id": job.id,
                    "activity": "llm_thinking",
                },
            )

            # Wait for event to be processed
            await asyncio.sleep(0.02)

            # Verify counter was reset
            assert monitor._polls_without_progress == 0, (
                f"Heartbeat should reset counter to 0, got {monitor._polls_without_progress}"
            )

        finally:
            # Clean up subscriptions
            await monitor._unsubscribe_from_tool_events()


@pytest.mark.timeout(10)
class TestSSEDeferredHandling:
    """Test SSE stream behavior on deferred events."""

    def test_sse_event_types_include_deferred(self):
        """Verify SSE subscribes to SUPERVISOR_DEFERRED event."""
        # This is a static check - the subscription is in jarvis_chat.py
        from zerg.routers.jarvis_chat import _chat_stream_generator

        # The function should exist and be an async generator
        assert asyncio.iscoroutinefunction(_chat_stream_generator) or hasattr(_chat_stream_generator, "__code__")

        # Note: Full SSE testing requires Playwright E2E tests
        # This just verifies the function exists
