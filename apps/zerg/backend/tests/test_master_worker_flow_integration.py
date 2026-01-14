"""Integration test: supervisor → spawn_worker → interrupt → worker_complete → resume → final response.

This covers the master/worker flow used by Jarvis chat using LangGraph's interrupt/resume pattern:
- Supervisor calls spawn_worker which triggers interrupt()
- Run is marked WAITING (interrupted waiting for worker completion)
- Worker completes, triggers resume via Command(resume=result)
- Supervisor resumes from interrupt() and generates final response

NOTE: This was rewritten during the LangGraph interrupt/resume refactor (Jan 2026).
The old continuation pattern (DEFERRED + run_continuation) was replaced with
interrupt()/Command(resume=...) pattern.

See: docs/work/supervisor-continuation-refactor.md
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from tests.conftest import TEST_WORKER_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.managers.agent_runner import AgentInterrupted
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import AgentRun
from zerg.models.models import WorkerJob
from zerg.routers.jarvis_sse import stream_run_events
from zerg.services.event_store import emit_run_event
from zerg.services.supervisor_context import set_supervisor_context
from zerg.services.supervisor_service import SupervisorService


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(agent_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_supervisor_worker_interrupt_resume_flow(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    """Test the interrupt/resume flow for supervisor → worker → final response.

    This test verifies:
    1. Supervisor run becomes WAITING when spawn_worker calls interrupt()
    2. Worker job is created and correlated to the supervisor run
    3. Resume completes the supervisor run with final response
    """
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    # Create a run record the same way /api/jarvis/chat does (run_id known before streaming).
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Start consuming SSE stream BEFORE running the supervisor to avoid missing early events.
    events: list[dict] = []

    async def consume_stream() -> None:
        async for evt in stream_run_events(run.id, test_user.id):
            events.append(evt)
            if evt.get("event") == "supervisor_complete":
                break

    consumer_task = asyncio.create_task(consume_stream())

    # Create a worker job first (simulating what spawn_worker does before interrupt)
    worker_job = WorkerJob(
        owner_id=test_user.id,
        supervisor_run_id=run.id,
        task="Check disk space on cube",
        model=TEST_WORKER_MODEL,
        status="queued",
    )
    db_session.add(worker_job)
    db_session.commit()
    db_session.refresh(worker_job)

    async def fake_run_thread_with_interrupt(_self, _db, _thread):
        """Simulate supervisor calling spawn_worker which triggers interrupt."""
        # Raise AgentInterrupted to simulate the interrupt() call inside spawn_worker
        # Note: No "message" field - frontend shows typing indicator, worker card shows task
        raise AgentInterrupted({
            "type": "worker_pending",
            "job_id": worker_job.id,
            "task": "Check disk space on cube",
        })

    # Test Phase 1: Supervisor run should become WAITING when interrupted
    with patch("zerg.managers.agent_runner.AgentRunner.run_thread", new=fake_run_thread_with_interrupt):
        result = await service.run_supervisor(
            owner_id=test_user.id,
            task="can you check disk space on cube",
            run_id=run.id,
            timeout=30,
        )
        # With interrupt pattern, status should be "waiting" not "deferred"
        assert result.status == "waiting"

    # Verify run is WAITING (this is the key assertion for interrupt pattern)
    db_session.refresh(run)
    assert run.status == RunStatus.WAITING

    # Test Phase 2: Simulate worker completion events
    await emit_run_event(
        db=db_session,
        run_id=run.id,
        event_type="worker_complete",
        payload={
            "job_id": worker_job.id,
            "worker_id": "test-worker-1",
            "status": "success",
            "duration_ms": 1234,
            "owner_id": test_user.id,
        },
    )

    await emit_run_event(
        db=db_session,
        run_id=run.id,
        event_type="worker_summary_ready",
        payload={
            "job_id": worker_job.id,
            "worker_id": "test-worker-1",
            "summary": "Cube at 45% disk; Docker is largest.",
            "owner_id": test_user.id,
        },
    )

    # Test Phase 3: Simulate resume with worker result
    # Mock the resume function to update run status and emit completion event
    async def mock_resume(db, run_id, worker_result):
        run_to_update = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        run_to_update.status = RunStatus.SUCCESS
        db.commit()

        # Emit supervisor_complete event (same as real resume does)
        await emit_run_event(
            db=db,
            run_id=run_id,
            event_type="supervisor_complete",
            payload={
                "thread_id": thread.id,
                "result": f"Based on the worker's findings: {worker_result}",
                "status": "success",
                "owner_id": test_user.id,
            },
        )
        return {"status": "success", "result": worker_result}

    with patch(
        "zerg.services.worker_resume.resume_supervisor_with_worker_result",
        side_effect=mock_resume,
    ):
        from zerg.services.worker_resume import resume_supervisor_with_worker_result

        # Call resume (normally triggered by worker_runner when worker completes)
        await resume_supervisor_with_worker_result(
            db=db_session,
            run_id=run.id,
            worker_result="Cube at 45% disk; Docker is largest.",
        )

    # Wait for the stream to receive supervisor_complete
    try:
        await asyncio.wait_for(consumer_task, timeout=5)
    except asyncio.TimeoutError:
        pass  # Stream may have already completed

    # Parse events and verify key events occurred
    parsed = []
    for evt in events:
        try:
            data = json.loads(evt.get("data") or "{}")
        except json.JSONDecodeError:
            continue
        parsed.append((evt.get("event"), data.get("payload") or {}, data))

    # Verify supervisor_waiting event was emitted (new pattern)
    waiting_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "supervisor_waiting":
            waiting_payload = payload
            break
    assert waiting_payload is not None
    assert waiting_payload.get("job_id") == worker_job.id

    # Verify supervisor_complete event was emitted
    complete_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "supervisor_complete":
            complete_payload = payload
            break
    assert complete_payload is not None
    assert "45% disk" in (complete_payload.get("result") or "")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_spawn_worker_fallback_when_outside_runnable_context(
    db_session,
    test_user,
    credential_context,
    temp_artifact_path,
):
    """Test that spawn_worker queues a job when called outside LangGraph context.

    This tests the graceful degradation when spawn_worker is called directly
    (e.g., from tests or CLI) rather than from within a LangGraph graph execution.
    """
    from zerg.tools.builtin.supervisor_tools import spawn_worker_async

    # Set up supervisor context for the tool
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Set supervisor context (normally done by supervisor_service)
    token = set_supervisor_context(run_id=run.id, owner_id=test_user.id, message_id="test-message-id")

    try:
        # Call spawn_worker directly (outside LangGraph context)
        # This should trigger the fallback path since interrupt() will fail
        result = await spawn_worker_async(task="Test fallback task", model=TEST_WORKER_MODEL)

        # Should return "queued successfully" (fallback pattern)
        assert "queued successfully" in result

        # Worker job should have been created
        job = (
            db_session.query(WorkerJob)
            .filter(WorkerJob.task == "Test fallback task")
            .first()
        )
        assert job is not None
        assert job.supervisor_run_id == run.id
        assert job.owner_id == test_user.id

    finally:
        from zerg.services.supervisor_context import reset_supervisor_context
        reset_supervisor_context(token)

    # NOTE: When called outside the graph, we only enqueue the worker job.
    # Worker execution is handled by WorkerJobProcessor in a running backend.


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_interrupt_triggers_waiting_status(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    """Test that spawn_worker properly triggers interrupt and WAITING status.

    This catches regressions where:
    - GraphInterrupt is accidentally caught (would result in SUCCESS instead of WAITING)
    - Tools run in wrong context (thread instead of async, breaking interrupt)

    The test uses ScriptedLLM to deterministically call spawn_worker,
    then verifies the run transitions to WAITING (not SUCCESS or FAILED).
    """
    from unittest.mock import patch

    from zerg.testing.scripted_llm import ScriptedChatLLM

    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    # Create a run record
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Use ScriptedChatLLM - it will call spawn_worker for "disk space on cube" prompts
    scripted_llm = ScriptedChatLLM()

    # Patch the LLM creation to use our scripted version
    # Patch both LangGraph path and new engine path
    with (
        patch("zerg.agents_def.zerg_react_agent._make_llm", return_value=scripted_llm),
        patch("zerg.services.supervisor_react_engine._make_llm", return_value=scripted_llm),
    ):
        # Run supervisor - should interrupt on spawn_worker
        result = await service.run_supervisor(
            owner_id=test_user.id,
            task="check disk space on cube",
            run_id=run.id,
            timeout=30,
        )

        # KEY ASSERTION: Run should be WAITING, not SUCCESS or FAILED
        # If GraphInterrupt was caught, status would be SUCCESS (old bug)
        # If tools ran in wrong context, status would be SUCCESS (recent bug)
        assert result.status == "waiting", (
            f"Expected 'waiting' but got '{result.status}'. "
            f"If 'success', GraphInterrupt is being caught somewhere. "
            f"If 'failed', spawn_worker errored instead of interrupting."
        )

    # Verify database state matches
    db_session.refresh(run)
    assert run.status == RunStatus.WAITING, f"Run should be WAITING but is {run.status}"

    # Verify worker job was created (spawn_worker ran before interrupt)
    worker_job = (
        db_session.query(WorkerJob)
        .filter(WorkerJob.supervisor_run_id == run.id)
        .first()
    )
    assert worker_job is not None, "Worker job should have been created before interrupt"
    assert worker_job.status == "queued", "Worker job should be queued"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_resume_completes_interrupted_run(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    """Test that resume_supervisor_with_worker_result completes an interrupted run.

    This test:
    1. Sets up a run in WAITING state (simulating post-interrupt)
    2. Calls the REAL resume function with a mock runnable
    3. Verifies the run completes with SUCCESS status and final response
    """
    from unittest.mock import patch

    from langchain_core.messages import AIMessage
    from langchain_core.messages import HumanMessage
    from langchain_core.messages import SystemMessage

    from zerg.crud import crud
    from zerg.services.worker_resume import resume_supervisor_with_worker_result

    # Set up supervisor agent/thread
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    # Add a user message so conversation has content
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="check disk space on cube",
        processed=True,
    )

    # Create a run in WAITING state (simulating interrupt happened)
    import uuid

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

    # Create corresponding worker job
    worker_job = WorkerJob(
        owner_id=test_user.id,
        supervisor_run_id=run.id,
        task="Check disk space on cube",
        model=TEST_WORKER_MODEL,
        status="success",
    )
    db_session.add(worker_job)
    db_session.commit()

    # Mock runnable that returns a final response (simulating resumed graph)
    class MockRunnable:
        async def ainvoke(self, _input, _config):
            # Return message history with final assistant response
            return [
                SystemMessage(content="You are Jarvis."),
                HumanMessage(content="check disk space on cube"),
                AIMessage(content="Cube is at 45% disk usage. Docker images are the largest consumer."),
            ]

    mock_runnable = MockRunnable()

    # Call REAL resume function with mock runnable
    with (
        patch("zerg.services.worker_resume.USE_LANGGRAPH_SUPERVISOR", True),
        patch("zerg.agents_def.zerg_react_agent.get_runnable", return_value=mock_runnable),
    ):
        result = await resume_supervisor_with_worker_result(
            db=db_session,
            run_id=run.id,
            worker_result="Cube disk usage: 45% used. Docker images are largest.",
        )

    # Verify resume succeeded
    assert result is not None, "Resume should return a result"
    assert result.get("status") == "success", f"Resume should succeed but got {result}"

    # Verify run is now SUCCESS
    db_session.refresh(run)
    assert run.status == RunStatus.SUCCESS, f"Run should be SUCCESS but is {run.status}"

    # Verify final response was captured
    final_result = result.get("result", "")
    assert "45%" in final_result, f"Final response should contain '45%', got: {final_result}"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_double_worker_spawn_on_resume_bug(
    db_session,
    test_user,
    credential_context,
    temp_artifact_path,
):
    """REGRESSION TEST: Reproduce the bug where LLM spawns a SECOND worker on resume.

    From Docker logs (2026-01-13 01:58):
    1. User: "check disk space on cube real quick"
    2. LLM call 1: spawn_worker("Check disk space on cube")
    3. Graph interrupts, worker executes, completes
    4. Resume supervisor
    5. LLM call 2 (on resume): spawn_worker("Check disk space on cube real quick")
       ^^^ THIS IS THE BUG - should synthesize result, not spawn again
    6. TWO workers created for ONE user request

    This test directly calls spawn_worker_async twice with the task strings
    from the real bug scenario to verify idempotency works.
    """
    import json
    import os

    from zerg.services.supervisor_context import reset_supervisor_context
    from zerg.services.worker_artifact_store import WorkerArtifactStore
    from zerg.tools.builtin.supervisor_tools import spawn_worker_async

    # Set up supervisor agent/thread
    service = SupervisorService(db_session)
    agent = service.get_or_create_supervisor_agent(test_user.id)
    thread = service.get_or_create_supervisor_thread(test_user.id, agent)

    # Create run
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Set supervisor context for spawn_worker tool
    sup_token = set_supervisor_context(
        run_id=run.id,
        owner_id=test_user.id,
        message_id="test-msg-1",
    )

    try:
        # Phase 1: First spawn_worker call - creates the worker
        # Task: "Check disk space on cube" (what the LLM said first)
        result1 = await spawn_worker_async("Check disk space on cube", model=TEST_WORKER_MODEL)
        assert "queued successfully" in result1, f"First spawn should create job, got: {result1}"

        # Verify first worker was created
        workers = db_session.query(WorkerJob).filter(
            WorkerJob.supervisor_run_id == run.id
        ).all()
        assert len(workers) == 1, f"Phase 1: Expected 1 worker, got {len(workers)}"
        first_worker = workers[0]
        assert first_worker.task == "Check disk space on cube"

        # Mark first worker as complete WITH artifacts (simulating real worker completion)
        first_worker.status = "success"
        first_worker.worker_id = "test-worker-resume-001"
        db_session.commit()

        # Create artifact files (required for idempotency to return cached result)
        artifact_store = WorkerArtifactStore()
        worker_dir = artifact_store._get_worker_dir("test-worker-resume-001")
        os.makedirs(worker_dir, exist_ok=True)

        with open(worker_dir / "result.txt", "w") as f:
            f.write("Disk usage on cube: 45% used. Docker images are the largest consumer.")
        with open(worker_dir / "metadata.json", "w") as f:
            json.dump({
                "worker_id": "test-worker-resume-001",
                "status": "success",
                "summary": "Cube is at 45% disk usage, Docker is largest consumer.",
                "owner_id": test_user.id,
            }, f)

        # Phase 2: Second spawn_worker call - the LLM rephrases on resume
        # Task: "Check disk space on cube real quick" (what the LLM said on resume)
        # THIS IS THE BUG SCENARIO - the idempotency should catch this
        result2 = await spawn_worker_async("Check disk space on cube real quick", model=TEST_WORKER_MODEL)

        # Count workers AFTER second spawn
        db_session.expire_all()  # Force refresh
        workers_after = db_session.query(WorkerJob).filter(
            WorkerJob.supervisor_run_id == run.id
        ).all()

        # THE KEY ASSERTION - This catches the bug
        # If idempotency is broken, we'll have 2 workers
        # The prefix matching should detect "Check disk space on cube" is a prefix of
        # "Check disk space on cube real quick" and return the cached result
        assert len(workers_after) == 1, (
            f"BUG REPRODUCED: {len(workers_after)} workers created!\n"
            f"Workers: {[(w.task, w.status) for w in workers_after]}\n"
            f"First spawn result: {result1}\n"
            f"Second spawn result: {result2}\n"
            f"Expected idempotency to catch the rephrased task."
        )

        # Second call should return the cached result
        assert "completed" in result2.lower(), (
            f"Second spawn should return cached result, got: {result2}"
        )

    finally:
        reset_supervisor_context(sup_token)
