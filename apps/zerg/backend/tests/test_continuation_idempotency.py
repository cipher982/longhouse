"""Tests for continuation idempotency and race conditions.

NOTE: The original tests for run_continuation() were removed during the
LangGraph interrupt/resume refactor (Jan 2026). The continuation pattern
now uses interrupt() + Command(resume=...) instead of creating separate
continuation runs.

See: docs/work/supervisor-continuation-refactor.md
"""

import asyncio
import uuid
from unittest.mock import patch

import pytest

from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.services.supervisor_service import SupervisorService


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


@pytest.mark.asyncio
async def test_concurrent_resume_only_runs_once(db_session, test_user):
    """Concurrent resume attempts should only resume once (idempotent)."""
    from langchain_core.messages import AIMessage
    from langchain_core.messages import HumanMessage
    from langchain_core.messages import SystemMessage

    from zerg.database import get_session_factory
    from zerg.services.worker_resume import resume_supervisor_with_worker_result

    # Seed supervisor agent/thread once
    bootstrap = SupervisorService(db_session)
    agent = bootstrap.get_or_create_supervisor_agent(test_user.id)
    thread = bootstrap.get_or_create_supervisor_thread(test_user.id, agent)

    # Add one user message so DB conversation length is non-zero
    from zerg.crud import crud

    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="check disk space",
        processed=True,
    )

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

    class FakeRunnable:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, _input, _config):  # noqa: ANN001 - test double
            self.calls += 1
            # Mimic LangGraph's message history output: system + internal context + conversation + final ai
            return [
                SystemMessage(content="sys"),
                SystemMessage(content="[INTERNAL CONTEXT]"),
                HumanMessage(content="check disk space"),
                AIMessage(content="ok"),
            ]

    fake_runnable = FakeRunnable()

    # Use two independent DB sessions to simulate a real race
    session_factory = get_session_factory()
    db1 = session_factory()
    db2 = session_factory()
    try:
        with (
            patch("zerg.services.worker_resume.USE_LANGGRAPH_SUPERVISOR", True),
            patch("zerg.agents_def.zerg_react_agent.get_runnable", return_value=fake_runnable),
        ):
            r1, r2 = await asyncio.gather(
                resume_supervisor_with_worker_result(db=db1, run_id=run.id, worker_result="a"),
                resume_supervisor_with_worker_result(db=db2, run_id=run.id, worker_result="b"),
            )
    finally:
        db1.close()
        db2.close()

    statuses = sorted([r.get("status") for r in (r1, r2) if r is not None])
    assert statuses.count("success") == 1
    assert statuses.count("skipped") == 1
    assert fake_runnable.calls == 1

    # Verify no duplicate tool messages were created (regression test for Jan 2026 bug)
    # The bug was in worker_resume.py where ToolMessages could be saved twice
    from collections import Counter

    from zerg.models.models import ThreadMessage

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
