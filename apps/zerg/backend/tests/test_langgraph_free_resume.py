"""Tests for LangGraph-free supervisor resume path.

These tests verify the new resume implementation that uses AgentRunner.run_continuation()
instead of LangGraph's Command(resume=...) pattern. The default behavior now uses
the LangGraph-free path.

Key behaviors tested:
- Happy path: WAITING run resumes successfully
- Concurrency: Only one resume succeeds when called concurrently
- Error handling: Missing tool_call_id fails with proper events
- Fallback: tool_call_id lookup via supervisor_run_id when job_id not passed
- Idempotency: Duplicate ToolMessages prevented on retry
"""

import asyncio
import uuid
from collections import Counter
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

from zerg.crud import crud
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
from zerg.models.models import WorkerJob
from zerg.services.supervisor_service import SupervisorService


@pytest.mark.timeout(30)
class TestLangGraphFreeResumeHappyPath:
    """Test basic happy path for LangGraph-free resume."""

    @pytest.mark.asyncio
    async def test_resume_success_with_worker_job(self, db_session, test_user, sample_agent):
        """Test that resume succeeds when WorkerJob has tool_call_id."""
        from zerg.services.worker_resume import _continue_supervisor_langgraph_free

        # Create thread with AIMessage containing tool_calls
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Add user message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="user",
            content="Run a background task",
            processed=True,
        )

        # Add AIMessage with tool_calls
        tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
        ai_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",
            processed=True,
        )
        # Set tool_calls in message_metadata
        ai_msg.message_metadata = {
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "spawn_commis",
                    "args": {"task": "test task"},
                }
            ]
        }
        db_session.commit()

        # Create WAITING run
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Create WorkerJob with tool_call_id
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=waiting_run.id,
            tool_call_id=tool_call_id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock AgentRunner.run_continuation to return success
        mock_created_rows = [
            MagicMock(role="assistant", content="Task completed successfully"),
        ]

        async def mock_run_continuation(self, db, thread, tool_call_id, tool_result, run_id, trace_id=None):
            return mock_created_rows

        with patch(
            "zerg.managers.agent_runner.AgentRunner.run_continuation",
            new=mock_run_continuation,
        ):
            result = await _continue_supervisor_langgraph_free(
                db=db_session,
                run_id=waiting_run.id,
                worker_result="Worker completed: test result",
                job_id=job.id,
            )

        # Verify result
        assert result is not None
        assert result["status"] == "success"
        assert "Task completed" in result.get("result", "")

        # Verify run status
        db_session.refresh(waiting_run)
        assert waiting_run.status == RunStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_resume_skipped_when_not_waiting(self, db_session, test_user, sample_agent):
        """Test that resume is skipped when run is not WAITING."""
        from zerg.services.worker_resume import _continue_supervisor_langgraph_free

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create SUCCESS run (not WAITING)
        success_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.SUCCESS,
            trigger=RunTrigger.API,
        )
        db_session.add(success_run)
        db_session.commit()
        db_session.refresh(success_run)

        result = await _continue_supervisor_langgraph_free(
            db=db_session,
            run_id=success_run.id,
            worker_result="Worker result",
        )

        # Verify skipped
        assert result is not None
        assert result["status"] == "skipped"
        assert "not waiting" in result.get("reason", "").lower()


@pytest.mark.timeout(30)
class TestLangGraphFreeResumeConcurrency:
    """Test concurrent resume only runs once (idempotency)."""

    @pytest.mark.asyncio
    async def test_concurrent_resume_only_runs_once(self, db_session, test_user):
        """Concurrent resume attempts should only resume once (idempotent)."""
        from zerg.database import get_session_factory
        from zerg.services.worker_resume import resume_supervisor_with_worker_result

        # Seed supervisor agent/thread once
        bootstrap = SupervisorService(db_session)
        agent = bootstrap.get_or_create_supervisor_agent(test_user.id)
        thread = bootstrap.get_or_create_supervisor_thread(test_user.id, agent)

        # Add one user message so DB conversation length is non-zero
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="user",
            content="run background task",
            processed=True,
        )

        # Add AIMessage with tool_calls
        tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
        ai_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",
            processed=True,
        )
        ai_msg.message_metadata = {
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "spawn_commis",
                    "args": {"task": "test task"},
                }
            ]
        }
        db_session.commit()

        # Create WAITING run
        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Create WorkerJob with tool_call_id
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=run.id,
            tool_call_id=tool_call_id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Track how many times run_continuation is called
        call_count = 0

        async def mock_run_continuation(self, db, thread, tool_call_id, tool_result, run_id, trace_id=None):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # Simulate some work
            return [MagicMock(role="assistant", content="Done")]

        # Use two independent DB sessions to simulate a real race
        session_factory = get_session_factory()
        db1 = session_factory()
        db2 = session_factory()

        try:
            with patch(
                "zerg.managers.agent_runner.AgentRunner.run_continuation",
                new=mock_run_continuation,
            ):
                r1, r2 = await asyncio.gather(
                    resume_supervisor_with_worker_result(db=db1, run_id=run.id, worker_result="a", job_id=job.id),
                    resume_supervisor_with_worker_result(db=db2, run_id=run.id, worker_result="b", job_id=job.id),
                )
        finally:
            db1.close()
            db2.close()

        statuses = sorted([r.get("status") for r in (r1, r2) if r is not None])

        # One should succeed, one should be skipped
        assert statuses.count("success") == 1, f"Expected exactly one success, got: {statuses}"
        assert statuses.count("skipped") == 1, f"Expected exactly one skipped, got: {statuses}"

        # run_continuation should only be called once
        assert call_count == 1, f"Expected run_continuation called once, got {call_count}"

        # Verify no duplicate tool messages were created
        tool_msgs = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "tool",
            )
            .all()
        )
        if tool_msgs:
            contents = [m.content for m in tool_msgs]
            content_counts = Counter(contents)
            duplicates = {k[:40]: v for k, v in content_counts.items() if v > 1}
            assert not duplicates, f"DUPLICATE TOOL MESSAGES: Found {duplicates}"


@pytest.mark.timeout(30)
class TestLangGraphFreeResumeErrorHandling:
    """Test error handling in LangGraph-free resume path."""

    @pytest.mark.asyncio
    async def test_resume_fails_when_no_tool_call_id(self, db_session, test_user, sample_agent):
        """Test that resume fails with proper events when tool_call_id is missing."""
        from zerg.services.worker_resume import _continue_supervisor_langgraph_free

        # Create thread
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Create WAITING run WITHOUT a WorkerJob
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Mock emit_run_event to track calls (patch where it's used)
        events_emitted = []

        async def mock_emit_run_event(db, run_id, event_type, payload):
            events_emitted.append({"type": event_type, "payload": payload})

        with patch("zerg.services.event_store.emit_run_event", side_effect=mock_emit_run_event):
            result = await _continue_supervisor_langgraph_free(
                db=db_session,
                run_id=waiting_run.id,
                worker_result="Worker result",
                job_id=None,  # No job_id, no WorkerJob
            )

        # Verify error result
        assert result is not None
        assert result["status"] == "error"
        assert "tool_call_id" in result.get("error", "").lower()

        # Verify run status
        db_session.refresh(waiting_run)
        assert waiting_run.status == RunStatus.FAILED

        # Verify events emitted
        event_types = [e["type"] for e in events_emitted]
        assert "error" in event_types, "Error event should be emitted"
        assert "run_updated" in event_types, "run_updated event should be emitted"

    @pytest.mark.asyncio
    async def test_tool_call_id_fallback_lookup(self, db_session, test_user, sample_agent):
        """Test that tool_call_id is found via supervisor_run_id when job_id not passed."""
        from zerg.services.worker_resume import _continue_supervisor_langgraph_free

        # Create thread with AIMessage containing tool_calls
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Add user message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="user",
            content="Run a background task",
            processed=True,
        )

        # Add AIMessage with tool_calls
        tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
        ai_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",
            processed=True,
        )
        ai_msg.message_metadata = {
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "spawn_commis",
                    "args": {"task": "test task"},
                }
            ]
        }
        db_session.commit()

        # Create WAITING run
        waiting_run = AgentRun(
            agent_id=sample_agent.id,
            thread_id=thread.id,
            status=RunStatus.WAITING,
            trigger=RunTrigger.API,
            assistant_message_id=str(uuid.uuid4()),
        )
        db_session.add(waiting_run)
        db_session.commit()
        db_session.refresh(waiting_run)

        # Create WorkerJob with supervisor_run_id (but don't pass job_id to resume)
        job = WorkerJob(
            owner_id=test_user.id,
            supervisor_run_id=waiting_run.id,
            tool_call_id=tool_call_id,
            task="Test task",
            model="gpt-mock",
            status="success",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # Mock AgentRunner.run_continuation
        mock_created_rows = [MagicMock(role="assistant", content="Done")]

        async def mock_run_continuation(self, db, thread, tool_call_id, tool_result, run_id, trace_id=None):
            return mock_created_rows

        with patch(
            "zerg.managers.agent_runner.AgentRunner.run_continuation",
            new=mock_run_continuation,
        ):
            # Call WITHOUT job_id - should use fallback lookup
            result = await _continue_supervisor_langgraph_free(
                db=db_session,
                run_id=waiting_run.id,
                worker_result="Worker completed",
                job_id=None,  # Not passed - fallback to supervisor_run_id lookup
            )

        # Verify success (fallback worked)
        assert result is not None
        assert result["status"] == "success"


@pytest.mark.timeout(30)
class TestRunContinuationIdempotency:
    """Test AgentRunner.run_continuation() idempotency."""

    @pytest.mark.asyncio
    async def test_run_continuation_creates_tool_message(self, db_session, test_user, sample_agent):
        """Test that run_continuation creates ToolMessage for worker result."""
        from langchain_core.messages import SystemMessage as LcSystemMessage

        from zerg.managers.agent_runner import AgentRunner
        from zerg.services.supervisor_react_engine import SupervisorResult

        # Create thread with AIMessage containing pending tool_call
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Add user message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="user",
            content="Run a task",
            processed=True,
        )

        # Add AIMessage with tool_calls (simulating spawn_commis call)
        tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
        ai_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",
            processed=True,
        )
        ai_msg.message_metadata = {
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "spawn_commis",
                    "args": {"task": "test task"},
                }
            ]
        }
        db_session.commit()

        # Mock run_supervisor_loop to return messages including input + new response
        # The result messages should include all input messages + new AIMessage
        mock_result = SupervisorResult(
            messages=[
                LcSystemMessage(content="system"),
                LcSystemMessage(content="context"),
                HumanMessage(content="Run a task"),
                AIMessage(content="", tool_calls=[{"id": tool_call_id, "name": "spawn_commis", "args": {"task": "test"}}]),
                ToolMessage(content="Worker completed:\n\ntest result", tool_call_id=tool_call_id, name="spawn_commis"),
                AIMessage(content="Task completed successfully."),
            ],
            usage={"total_tokens": 100},
            interrupted=False,
        )

        with patch(
            "zerg.services.supervisor_react_engine.run_supervisor_loop",
            new=AsyncMock(return_value=mock_result),
        ):
            runner = AgentRunner(sample_agent)
            created_rows = await runner.run_continuation(
                db=db_session,
                thread=thread,
                tool_call_id=tool_call_id,
                tool_result="Worker completed: test result",
                run_id=123,
            )

        # Verify ToolMessage was created in the database
        tool_msgs = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "tool",
            )
            .all()
        )
        assert len(tool_msgs) >= 1, "ToolMessage should be created"

        # Verify at least one tool message contains our worker result
        found_worker_result = any(
            "Worker completed" in (msg.content or "")
            for msg in tool_msgs
        )
        assert found_worker_result, "Should find ToolMessage with worker result content"

    @pytest.mark.asyncio
    async def test_run_continuation_idempotent_tool_message(self, db_session, test_user, sample_agent):
        """Test that run_continuation doesn't create duplicate ToolMessage."""
        from zerg.managers.agent_runner import AgentRunner
        from zerg.services.supervisor_react_engine import SupervisorResult

        # Create thread with AIMessage + existing ToolMessage
        thread = crud.create_thread(
            db=db_session,
            agent_id=sample_agent.id,
            title="Test thread",
            active=True,
        )

        # Add user message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="user",
            content="Run a task",
            processed=True,
        )

        # Add AIMessage with tool_calls
        tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
        ai_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="assistant",
            content="",
            processed=True,
        )
        ai_msg.message_metadata = {
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "name": "spawn_commis",
                    "args": {"task": "test task"},
                }
            ]
        }

        # Pre-create ToolMessage (simulating first resume attempt)
        existing_tool_msg = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="tool",
            content="Worker completed:\n\nFirst result",
            processed=True,
        )
        existing_tool_msg.message_metadata = {
            "tool_call_id": tool_call_id,
            "name": "spawn_commis",
        }
        db_session.commit()

        # Mock run_supervisor_loop to return a simple result
        # Patch where it's defined in supervisor_react_engine module
        mock_result = SupervisorResult(
            messages=[
                AIMessage(content="Task completed successfully."),
            ],
            usage={"total_tokens": 100},
            interrupted=False,
        )

        with patch(
            "zerg.services.supervisor_react_engine.run_supervisor_loop",
            new=AsyncMock(return_value=mock_result),
        ):
            runner = AgentRunner(sample_agent)
            created_rows = await runner.run_continuation(
                db=db_session,
                thread=thread,
                tool_call_id=tool_call_id,
                tool_result="Worker completed: second result (should be ignored)",
                run_id=123,
            )

        # Verify no duplicate ToolMessage
        tool_msgs = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.role == "tool",
            )
            .all()
        )

        # Count ToolMessages with our tool_call_id
        matching_count = 0
        for msg in tool_msgs:
            meta = msg.message_metadata or {}
            if meta.get("tool_call_id") == tool_call_id:
                matching_count += 1

        assert matching_count == 1, f"Should have exactly 1 ToolMessage with tool_call_id, got {matching_count}"
