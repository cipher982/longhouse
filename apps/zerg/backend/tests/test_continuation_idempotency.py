"""Tests for continuation idempotency and race conditions."""

import asyncio
from unittest.mock import patch

import pytest

from zerg.database import get_session_factory
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import ThreadMessage
from zerg.services.supervisor_service import SupervisorRunResult
from zerg.services.supervisor_service import SupervisorService


@pytest.mark.asyncio
async def test_concurrent_continuation_creates_only_one(db_session, test_user):
    """Test that concurrent continuation attempts create only one continuation run.

    This test simulates a race condition where two workers complete simultaneously
    and both try to create a continuation run. The DB unique constraint should
    prevent duplicates, and both callers should get a valid response.
    """
    # Seed supervisor agent/thread once
    bootstrap = SupervisorService(db_session)
    agent = bootstrap.get_or_create_supervisor_agent(test_user.id)
    thread = bootstrap.get_or_create_supervisor_thread(test_user.id, agent)

    # Create a DEFERRED run to continue
    original_run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.DEFERRED,
        trigger=RunTrigger.API,
    )
    db_session.add(original_run)
    db_session.commit()
    db_session.refresh(original_run)

    async def _fake_run_supervisor(self, owner_id, task, run_id=None, timeout=0, **_kwargs):
        # Yield once to increase the chance of interleaving between callers.
        await asyncio.sleep(0)
        return SupervisorRunResult(
            run_id=run_id or -1,
            thread_id=thread.id,
            status="success",
            result="ok",
            duration_ms=0,
        )

    session_factory = get_session_factory()
    db1 = session_factory()
    db2 = session_factory()
    try:
        supervisor1 = SupervisorService(db1)
        supervisor2 = SupervisorService(db2)

        with patch.object(SupervisorService, "run_supervisor", new=_fake_run_supervisor):
            # Call run_continuation twice concurrently (simulating race condition)
            results = await asyncio.gather(
                supervisor1.run_continuation(
                    original_run_id=original_run.id,
                    job_id=1,
                    worker_id="worker-1",
                    result_summary="Test result 1",
                ),
                supervisor2.run_continuation(
                    original_run_id=original_run.id,
                    job_id=2,
                    worker_id="worker-2",
                    result_summary="Test result 2",
                ),
                return_exceptions=True,
            )
    finally:
        db1.close()
        db2.close()

    # Both calls should succeed (no exceptions)
    assert len(results) == 2
    assert not isinstance(results[0], Exception)
    assert not isinstance(results[1], Exception)

    # Both should return the same continuation run ID
    result1, result2 = results
    assert result1.run_id == result2.run_id

    # Verify only ONE continuation run was created in the database
    continuations = (
        db_session.query(AgentRun)
        .filter(
            AgentRun.continuation_of_run_id == original_run.id,
            AgentRun.trigger == RunTrigger.CONTINUATION,
        )
        .all()
    )

    assert len(continuations) == 1
    assert continuations[0].id == result1.run_id

    # Tool message should only be injected once (same transaction as the run insert).
    tool_msgs = (
        db_session.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role == "tool",
            ThreadMessage.content.contains("[Worker job"),
        )
        .all()
    )
    assert len(tool_msgs) == 1


@pytest.mark.asyncio
async def test_continuation_idempotency_sequential(db_session, test_user):
    """Test that calling run_continuation twice sequentially returns the same run.

    This verifies that the idempotency check works even without race conditions.
    """
    # Create supervisor service and components
    supervisor = SupervisorService(db_session)
    agent = supervisor.get_or_create_supervisor_agent(test_user.id)
    thread = supervisor.get_or_create_supervisor_thread(test_user.id, agent)

    # Create a DEFERRED run to continue
    original_run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.DEFERRED,
        trigger=RunTrigger.API,
    )
    db_session.add(original_run)
    db_session.commit()
    db_session.refresh(original_run)

    async def _fake_run_supervisor(self, owner_id, task, run_id=None, timeout=0, **_kwargs):
        return SupervisorRunResult(
            run_id=run_id or -1,
            thread_id=thread.id,
            status="success",
            result="ok",
            duration_ms=0,
        )

    with patch.object(SupervisorService, "run_supervisor", new=_fake_run_supervisor):
        # Call run_continuation first time
        result1 = await supervisor.run_continuation(
            original_run_id=original_run.id,
            job_id=1,
            worker_id="worker-1",
            result_summary="Test result 1",
        )

        # Call run_continuation second time (should return existing)
        result2 = await supervisor.run_continuation(
            original_run_id=original_run.id,
            job_id=2,
            worker_id="worker-2",
            result_summary="Test result 2",
        )

    # Both should return the same continuation run ID
    assert result1.run_id == result2.run_id

    # Verify only ONE continuation run exists
    continuations = (
        db_session.query(AgentRun)
        .filter(
            AgentRun.continuation_of_run_id == original_run.id,
            AgentRun.trigger == RunTrigger.CONTINUATION,
        )
        .all()
    )

    assert len(continuations) == 1

    # Tool message should only be injected once.
    tool_msgs = (
        db_session.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role == "tool",
            ThreadMessage.content.contains("[Worker job"),
        )
        .all()
    )
    assert len(tool_msgs) == 1


@pytest.mark.asyncio
async def test_continuation_allows_null_continuation_of_run_id(db_session, test_user):
    """Test that the unique constraint allows multiple NULL continuation_of_run_id values.

    This verifies that non-continuation runs (NULL continuation_of_run_id) are not
    affected by the unique constraint.
    """
    # Create supervisor service and components
    supervisor = SupervisorService(db_session)
    agent = supervisor.get_or_create_supervisor_agent(test_user.id)
    thread = supervisor.get_or_create_supervisor_thread(test_user.id, agent)

    # Create multiple regular (non-continuation) runs
    run1 = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.SUCCESS,
        trigger=RunTrigger.API,
        continuation_of_run_id=None,  # NULL
    )
    run2 = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.SUCCESS,
        trigger=RunTrigger.API,
        continuation_of_run_id=None,  # NULL
    )

    db_session.add(run1)
    db_session.add(run2)
    db_session.commit()  # Should succeed - multiple NULLs are allowed

    # Verify both runs were created
    runs = (
        db_session.query(AgentRun)
        .filter(
            AgentRun.agent_id == agent.id,
            AgentRun.continuation_of_run_id.is_(None),
        )
        .all()
    )

    assert len(runs) >= 2  # At least our 2 new runs
