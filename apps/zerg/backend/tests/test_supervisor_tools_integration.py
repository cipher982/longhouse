"""Integration tests for supervisor tools with real agent execution."""

import tempfile

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.crud import crud
from zerg.managers.agent_runner import AgentRunner
from zerg.services.thread_service import ThreadService
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.tools.registry import ImmutableToolRegistry
from zerg.tools.builtin import BUILTIN_TOOLS
from tests.conftest import TEST_MODEL, TEST_WORKER_MODEL


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def supervisor_agent(db_session, test_user):
    """Create a supervisor agent with spawn_worker tool enabled."""
    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Supervisor Agent",
        model=TEST_WORKER_MODEL,
        system_instructions=(
            "You are a supervisor agent. You can delegate tasks to worker agents "
            "using the spawn_worker tool. When given a task, spawn a worker to handle it."
        ),
        task_instructions="",
    )
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_supervisor_spawns_worker_via_tool(
    supervisor_agent, db_session, test_user, temp_artifact_path
):
    """Test that a supervisor agent can use spawn_worker tool (queues job)."""
    from zerg.models.models import WorkerJob

    # Create a thread for the supervisor
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        supervisor_agent,
        title="Test Supervisor Thread",
        thread_type="manual",
        active=False,
    )

    # Add user message asking supervisor to spawn a worker
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="Spawn a worker to calculate 10 + 15",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(
        agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id
    )
    set_credential_resolver(resolver)

    try:
        # Run the supervisor agent
        runner = AgentRunner(supervisor_agent)
        messages = await runner.run_thread(db_session, thread)

        # Verify the supervisor called spawn_worker
        # Look for tool calls in the messages
        tool_calls_found = False
        spawn_worker_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                tool_calls_found = True
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "spawn_worker":
                        spawn_worker_called = True
                        break

        assert tool_calls_found, "Supervisor should have called tools"
        assert spawn_worker_called, "Supervisor should have called spawn_worker"

        # Verify a worker JOB was queued (not executed synchronously)
        jobs = db_session.query(WorkerJob).filter(
            WorkerJob.owner_id == test_user.id
        ).all()
        assert len(jobs) >= 1, "At least one worker job should have been queued"

        # Verify job is queued
        job = jobs[0]
        assert job.status == "queued", "Worker job should be queued"
        assert "calculate" in job.task.lower() or "10" in job.task

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_supervisor_can_list_workers(
    supervisor_agent, db_session, test_user, temp_artifact_path
):
    """Test that a supervisor can use list_workers tool."""
    from zerg.models.models import WorkerJob
    from datetime import datetime, timezone

    # Create a WorkerJob record (simulating a queued job)
    worker_job = WorkerJob(
        owner_id=test_user.id,
        task="Test task for listing",
        model=TEST_WORKER_MODEL,
        status="queued",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(worker_job)
    db_session.commit()

    # Create a thread for the supervisor
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        supervisor_agent,
        title="Test List Workers",
        thread_type="manual",
        active=False,
    )

    # Add user message asking supervisor to list workers
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="List all recent workers",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(
        agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id
    )
    set_credential_resolver(resolver)

    try:
        # Run the supervisor agent
        agent_runner = AgentRunner(supervisor_agent)
        messages = await agent_runner.run_thread(db_session, thread)

        # Verify the supervisor called list_workers
        list_workers_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "list_workers":
                        list_workers_called = True
                        break

        assert list_workers_called, "Supervisor should have called list_workers"

        # Check that the response mentions the worker
        final_message = messages[-1]
        assert final_message.role == "assistant"
        # The response should contain information about workers
        assert len(final_message.content) > 0

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_supervisor_reads_worker_result(
    supervisor_agent, db_session, test_user, temp_artifact_path
):
    """Test that a supervisor can read worker results."""
    from zerg.models.models import WorkerJob
    from zerg.services.worker_runner import WorkerRunner
    from datetime import datetime, timezone

    # First spawn a worker directly via WorkerRunner (creates artifacts)
    artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)
    runner = WorkerRunner(artifact_store=artifact_store)

    result = await runner.run_worker(
        db=db_session,
        task="Calculate 5 * 8",
        agent=None,
        agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
    )

    worker_id = result.worker_id

    # Create a WorkerJob record linking to this worker (so tools can find it)
    worker_job = WorkerJob(
        owner_id=test_user.id,
        task="Calculate 5 * 8",
        model=TEST_WORKER_MODEL,
        status="success",
        worker_id=worker_id,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(worker_job)
    db_session.commit()
    db_session.refresh(worker_job)

    job_id = worker_job.id

    # Create a thread for the supervisor
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        supervisor_agent,
        title="Test Read Worker Result",
        thread_type="manual",
        active=False,
    )

    # Add user message asking supervisor to read the worker result (using job_id)
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content=f"Read the result from worker job {job_id}",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(
        agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id
    )
    set_credential_resolver(resolver)

    try:
        # Run the supervisor agent
        agent_runner = AgentRunner(supervisor_agent)
        messages = await agent_runner.run_thread(db_session, thread)

        # Verify the supervisor called read_worker_result
        read_worker_result_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "read_worker_result":
                        read_worker_result_called = True
                        break

        assert (
            read_worker_result_called
        ), "Supervisor should have called read_worker_result"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_tools_registered_in_builtin(db_session):
    """Test that supervisor tools are registered in BUILTIN_TOOLS."""
    # Build registry
    registry = ImmutableToolRegistry.build([BUILTIN_TOOLS])

    # Verify all supervisor tools are registered
    assert registry.get("spawn_worker") is not None
    assert registry.get("list_workers") is not None
    assert registry.get("read_worker_result") is not None
    assert registry.get("read_worker_file") is not None
    assert registry.get("grep_workers") is not None
    assert registry.get("get_worker_metadata") is not None

    # Verify tool descriptions
    spawn_tool = registry.get("spawn_worker")
    assert "delegate" in spawn_tool.description.lower() or "spawn" in spawn_tool.description.lower()
