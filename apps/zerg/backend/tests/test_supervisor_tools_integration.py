"""Integration tests for supervisor tools with real agent execution."""

import tempfile

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_WORKER_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.crud import crud
from zerg.managers.agent_runner import AgentRunner
from zerg.services.thread_service import ThreadService
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.registry import ImmutableToolRegistry


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def supervisor_agent(db_session, test_user):
    """Create a supervisor agent with supervisor tools enabled."""
    agent = crud.create_agent(
        db=db_session,
        owner_id=test_user.id,
        name="Supervisor Agent",
        model=TEST_MODEL,  # Use smarter model - gpt-5-mini is unreliable for tool calling
        system_instructions=(
            "You are a supervisor agent that MUST delegate ALL tasks to workers. "
            "You have access to the spawn_worker tool. "
            "IMPORTANT: When asked to do anything, you MUST call spawn_worker immediately. "
            "Never respond with text - always use the spawn_worker tool."
        ),
        task_instructions="",
    )
    # Set allowed_tools - required for tools to be passed to the LLM
    agent.allowed_tools = [
        "spawn_worker",
        "list_workers",
        "read_worker_result",
        "read_worker_file",
        "grep_workers",
        "get_worker_metadata",
    ]
    db_session.commit()
    db_session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_supervisor_spawns_worker_via_tool(supervisor_agent, db_session, test_user, temp_artifact_path):
    """Test that a supervisor agent can use spawn_worker tool (queues job).

    spawn_worker uses LangGraph's interrupt/resume pattern, so calling it triggers
    AgentInterrupted. We verify the interrupt payload and that the job was queued.
    """
    from zerg.managers.agent_runner import AgentInterrupted
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
    resolver = CredentialResolver(agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the supervisor agent - spawn_worker triggers interrupt
        runner = AgentRunner(supervisor_agent)
        try:
            await runner.run_thread(db_session, thread)
            pytest.fail("Expected AgentInterrupted to be raised")
        except AgentInterrupted as e:
            # Verify interrupt payload indicates worker_pending
            assert e.interrupt_value.get("type") == "worker_pending", "Interrupt should be worker_pending"
            assert "job_id" in e.interrupt_value, "Interrupt should include job_id"

        # Verify a worker JOB was queued
        jobs = db_session.query(WorkerJob).filter(WorkerJob.owner_id == test_user.id).all()
        assert len(jobs) >= 1, "At least one worker job should have been queued"

        # Verify job is queued
        job = jobs[0]
        assert job.status == "queued", "Worker job should be queued"
        assert len(job.task) > 0, "Worker job should have a task"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_supervisor_can_list_workers(supervisor_agent, db_session, test_user, temp_artifact_path):
    """Test that a supervisor can use list_workers tool."""
    from datetime import datetime
    from datetime import timezone

    from zerg.models.models import WorkerJob

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
        content="Use the list_workers tool to show me all recent workers",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id)
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
async def test_supervisor_reads_worker_result(supervisor_agent, db_session, test_user, temp_artifact_path):
    """Test that a supervisor can read worker results."""
    from datetime import datetime
    from datetime import timezone

    from zerg.models.models import WorkerJob
    from zerg.services.worker_runner import WorkerRunner

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
    resolver = CredentialResolver(agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id)
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

        assert read_worker_result_called, "Supervisor should have called read_worker_result"

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


@pytest.mark.asyncio
async def test_read_worker_result_includes_duration(supervisor_agent, db_session, test_user, temp_artifact_path):
    """Test that read_worker_result returns duration_ms from completed workers (Tier 1 visibility)."""
    from datetime import datetime
    from datetime import timezone

    from zerg.connectors.context import set_credential_resolver
    from zerg.connectors.resolver import CredentialResolver
    from zerg.models.models import WorkerJob
    from zerg.services.worker_runner import WorkerRunner

    # Create and run a worker directly via WorkerRunner
    # Set up credential resolver context FIRST (needed for worker execution)
    resolver = CredentialResolver(agent_id=supervisor_agent.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        artifact_store = WorkerArtifactStore(base_path=temp_artifact_path)
        runner = WorkerRunner(artifact_store=artifact_store)

        result = await runner.run_worker(
            db=db_session,
            task="Calculate 7 * 6",
            agent=None,
            agent_config={"model": TEST_WORKER_MODEL, "owner_id": test_user.id},
        )

        worker_id = result.worker_id

        # Create a WorkerJob record linking to this worker
        worker_job = WorkerJob(
            owner_id=test_user.id,
            task="Calculate 7 * 6",
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

        # Call read_worker_result_async directly (to preserve context in same async loop)
        from zerg.tools.builtin.supervisor_tools import read_worker_result_async

        result_text = await read_worker_result_async(str(job_id))

        # Verify the result includes duration_ms
        assert "Execution time:" in result_text, f"Result should include execution time. Got: {result_text}"
        assert "ms" in result_text, "Result should include milliseconds unit"

        # Verify the duration_ms is actually a number
        # Extract the duration using a simple pattern
        import re

        duration_match = re.search(r"Execution time: (\d+)ms", result_text)
        assert duration_match is not None, "Should find duration in result"
        duration_value = int(duration_match.group(1))
        assert duration_value > 0, "Duration should be greater than 0ms"

        # Verify we still get the actual result text
        assert "42" in result_text or "result" in result_text.lower(), "Result should include actual worker output"

    finally:
        set_credential_resolver(None)
