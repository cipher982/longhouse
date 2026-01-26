"""Integration test: concierge → spawn_commis → interrupt → commis_complete → resume → final response.

This covers the master/commis flow used by Jarvis chat using the LangGraph-free resume pattern:
- Concierge calls spawn_commis and raises CourseInterrupted
- Run is marked WAITING (interrupted waiting for commis completion)
- Commis completes, triggers resume via FicheRunner.run_continuation
- Concierge continues and generates final response

NOTE: This was rewritten during the concierge refactor (Jan 2026). The old
continuation pattern (DEFERRED + run_continuation) was replaced with
interrupt/resume via CourseInterrupted + DB-based continuation.

See: docs/work/concierge-continuation-refactor.md
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.managers.fiche_runner import CourseInterrupted
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.models import Course
from zerg.models.models import CommisJob
from zerg.routers.jarvis_sse import stream_course_events
from zerg.services.event_store import emit_course_event
from zerg.services.concierge_context import set_concierge_context
from zerg.services.concierge_service import ConciergeService


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_concierge_commis_interrupt_resume_flow(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    """Test the interrupt/resume flow for concierge → commis → final response.

    This test verifies:
    1. Concierge run becomes WAITING when spawn_commis triggers CourseInterrupted
    2. Commis job is created and correlated to the concierge run
    3. Resume completes the concierge run with final response
    """
    service = ConciergeService(db_session)
    fiche = service.get_or_create_concierge_fiche(test_user.id)
    thread = service.get_or_create_concierge_thread(test_user.id, fiche)

    # Create a run record the same way /api/jarvis/chat does (course_id known before streaming).
    run = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Start consuming SSE stream BEFORE running the concierge to avoid missing early events.
    events: list[dict] = []

    async def consume_stream() -> None:
        async for evt in stream_course_events(run.id, test_user.id):
            events.append(evt)
            if evt.get("event") == "concierge_complete":
                break

    consumer_task = asyncio.create_task(consume_stream())

    # Create a commis job first (simulating what spawn_commis does before interrupt)
    commis_job = CommisJob(
        owner_id=test_user.id,
        concierge_course_id=run.id,
        task="Check disk space on cube",
        model=TEST_COMMIS_MODEL,
        status="queued",
    )
    db_session.add(commis_job)
    db_session.commit()
    db_session.refresh(commis_job)

    async def fake_run_thread_with_interrupt(_self, _db, _thread):
        """Simulate concierge calling spawn_commis which triggers CourseInterrupted."""
        # Raise CourseInterrupted to simulate the interrupt path inside spawn_commis
        # Note: No "message" field - frontend shows typing indicator, commis card shows task
        raise CourseInterrupted(
            {
                "type": "commis_pending",
                "job_id": commis_job.id,
                "task": "Check disk space on cube",
            }
        )

    # Test Phase 1: Concierge run should become WAITING when interrupted
    with patch("zerg.managers.fiche_runner.FicheRunner.run_thread", new=fake_run_thread_with_interrupt):
        result = await service.run_concierge(
            owner_id=test_user.id,
            task="can you check disk space on cube",
            course_id=run.id,
            timeout=30,
        )
        # With interrupt pattern, status should be "waiting" not "deferred"
        assert result.status == "waiting"

    # Verify run is WAITING (this is the key assertion for interrupt pattern)
    db_session.refresh(run)
    assert run.status == CourseStatus.WAITING

    # Test Phase 2: Simulate commis completion events
    await emit_course_event(
        db=db_session,
        course_id=run.id,
        event_type="commis_complete",
        payload={
            "job_id": commis_job.id,
            "commis_id": "test-commis-1",
            "status": "success",
            "duration_ms": 1234,
            "owner_id": test_user.id,
        },
    )

    await emit_course_event(
        db=db_session,
        course_id=run.id,
        event_type="commis_summary_ready",
        payload={
            "job_id": commis_job.id,
            "commis_id": "test-commis-1",
            "summary": "Cube at 45% disk; Docker is largest.",
            "owner_id": test_user.id,
        },
    )

    # Test Phase 3: Simulate resume with commis result
    # Mock the resume function to update run status and emit completion event
    async def mock_resume(db, course_id, commis_result):
        run_to_update = db.query(Course).filter(Course.id == course_id).first()
        run_to_update.status = CourseStatus.SUCCESS
        db.commit()

        # Emit concierge_complete event (same as real resume does)
        await emit_course_event(
            db=db,
            course_id=course_id,
            event_type="concierge_complete",
            payload={
                "thread_id": thread.id,
                "result": f"Based on the commis's findings: {commis_result}",
                "status": "success",
                "owner_id": test_user.id,
            },
        )
        return {"status": "success", "result": commis_result}

    with patch(
        "zerg.services.commis_resume.resume_concierge_with_commis_result",
        side_effect=mock_resume,
    ):
        from zerg.services.commis_resume import resume_concierge_with_commis_result

        # Call resume (normally triggered by commis_runner when commis completes)
        await resume_concierge_with_commis_result(
            db=db_session,
            course_id=run.id,
            commis_result="Cube at 45% disk; Docker is largest.",
        )

    # Wait for the stream to receive concierge_complete
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

    # Verify concierge_waiting event was emitted (new pattern)
    waiting_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "concierge_waiting":
            waiting_payload = payload
            break
    assert waiting_payload is not None
    assert waiting_payload.get("job_id") == commis_job.id

    # Verify concierge_complete event was emitted
    complete_payload = None
    for event_name, payload, _wrapper in parsed:
        if event_name == "concierge_complete":
            complete_payload = payload
            break
    assert complete_payload is not None
    assert "45% disk" in (complete_payload.get("result") or "")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_spawn_commis_fallback_when_outside_runnable_context(
    db_session,
    test_user,
    credential_context,
    temp_artifact_path,
):
    """Test that spawn_commis queues a job when called outside concierge context.

    This tests the graceful degradation when spawn_commis is called directly
    (e.g., from tests or CLI) rather than from within the concierge loop.
    """
    from zerg.tools.builtin.concierge_tools import spawn_commis_async

    # Set up concierge context for the tool
    service = ConciergeService(db_session)
    fiche = service.get_or_create_concierge_fiche(test_user.id)
    thread = service.get_or_create_concierge_thread(test_user.id, fiche)

    run = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Set concierge context (normally done by concierge_service)
    token = set_concierge_context(course_id=run.id, owner_id=test_user.id, message_id="test-message-id")

    try:
        # Call spawn_commis directly (outside concierge loop context)
        # This should trigger the fallback path since no CourseInterrupted handling exists
        result = await spawn_commis_async(task="Test fallback task", model=TEST_COMMIS_MODEL)

        # Should return "queued successfully" (fallback pattern)
        assert "queued successfully" in result

        # Commis job should have been created
        job = db_session.query(CommisJob).filter(CommisJob.task == "Test fallback task").first()
        assert job is not None
        assert job.concierge_course_id == run.id
        assert job.owner_id == test_user.id

    finally:
        from zerg.services.concierge_context import reset_concierge_context

        reset_concierge_context(token)

    # NOTE: When called outside the graph, we only enqueue the commis job.
    # Commis execution is handled by CommisJobProcessor in a running backend.


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_resume_completes_interrupted_run(
    db_session,
    test_user,
    credential_context,  # noqa: ARG001 - fixture activates resolver context
    temp_artifact_path,  # noqa: ARG001 - ensures artifact store is writable if used
):
    """Test that resume_concierge_with_commis_result completes an interrupted run.

    This test:
    1. Sets up a run in WAITING state (simulating post-interrupt)
    2. Calls the REAL resume function with mocked run_continuation
    3. Verifies the run completes with SUCCESS status and final response
    """
    from unittest.mock import patch

    from zerg.crud import crud
    from zerg.services.commis_resume import resume_concierge_with_commis_result

    # Set up concierge fiche/thread
    service = ConciergeService(db_session)
    fiche = service.get_or_create_concierge_fiche(test_user.id)
    thread = service.get_or_create_concierge_thread(test_user.id, fiche)

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

    run = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.WAITING,
        trigger=CourseTrigger.API,
        assistant_message_id=str(uuid.uuid4()),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Create corresponding commis job with tool_call_id for resume lookup
    tool_call_id = f"call_{uuid.uuid4().hex[:12]}"
    commis_job = CommisJob(
        owner_id=test_user.id,
        concierge_course_id=run.id,
        tool_call_id=tool_call_id,
        task="Check disk space on cube",
        model=TEST_COMMIS_MODEL,
        status="success",
    )
    db_session.add(commis_job)
    db_session.commit()

    # Mock FicheRunner.run_continuation to return a final response
    from unittest.mock import MagicMock

    mock_created_rows = [
        MagicMock(role="assistant", content="Cube is at 45% disk usage. Docker images are the largest consumer."),
    ]

    async def mock_run_continuation(self, db, thread, tool_call_id, tool_result, course_id, trace_id=None):
        return mock_created_rows

    # Call REAL resume function with mocked run_continuation
    with patch(
        "zerg.managers.fiche_runner.FicheRunner.run_continuation",
        new=mock_run_continuation,
    ):
        result = await resume_concierge_with_commis_result(
            db=db_session,
            course_id=run.id,
            commis_result="Cube disk usage: 45% used. Docker images are largest.",
            job_id=commis_job.id,
        )

    # Verify resume succeeded
    assert result is not None, "Resume should return a result"
    assert result.get("status") == "success", f"Resume should succeed but got {result}"

    # Verify run is now SUCCESS
    db_session.refresh(run)
    assert run.status == CourseStatus.SUCCESS, f"Run should be SUCCESS but is {run.status}"

    # Verify final response was captured
    final_result = result.get("result", "")
    assert "45%" in final_result, f"Final response should contain '45%', got: {final_result}"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_different_tasks_create_separate_commis(
    db_session,
    test_user,
    credential_context,
    temp_artifact_path,
):
    """Verify that different tasks create separate commis (EXACT match only).

    Scenario from Docker logs (2026-01-13 01:58):
    1. LLM call 1: spawn_commis("Check disk space on cube")
    2. Commis completes
    3. LLM call 2: spawn_commis("Check disk space on cube real quick")

    With EXACT matching only, these are treated as different tasks and create
    separate commis. This is the safer default - prefix matching was removed
    because near-matches could return wrong commis results.

    If you need idempotency for rephrased tasks, use tool_call_id.
    """
    import json
    import os

    from zerg.services.concierge_context import reset_concierge_context
    from zerg.services.commis_artifact_store import CommisArtifactStore
    from zerg.tools.builtin.concierge_tools import spawn_commis_async

    # Set up concierge fiche/thread
    service = ConciergeService(db_session)
    fiche = service.get_or_create_concierge_fiche(test_user.id)
    thread = service.get_or_create_concierge_thread(test_user.id, fiche)

    # Create run
    run = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.RUNNING,
        trigger=CourseTrigger.API,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Set concierge context for spawn_commis tool
    sup_token = set_concierge_context(
        course_id=run.id,
        owner_id=test_user.id,
        message_id="test-msg-1",
    )

    try:
        # Phase 1: First spawn_commis call - creates commis
        result1 = await spawn_commis_async("Check disk space on cube", model=TEST_COMMIS_MODEL)
        assert "queued successfully" in result1, f"First spawn should create job, got: {result1}"

        # Verify first commis was created
        commis = db_session.query(CommisJob).filter(CommisJob.concierge_course_id == run.id).all()
        assert len(commis) == 1, f"Phase 1: Expected 1 commis, got {len(commis)}"
        first_commis = commis[0]
        assert first_commis.task == "Check disk space on cube"

        # Mark first commis as complete WITH artifacts
        first_commis.status = "success"
        first_commis.commis_id = "test-commis-resume-001"
        db_session.commit()

        # Create artifact files
        artifact_store = CommisArtifactStore()
        commis_dir = artifact_store._get_commis_dir("test-commis-resume-001")
        os.makedirs(commis_dir, exist_ok=True)

        with open(commis_dir / "result.txt", "w") as f:
            f.write("Disk usage on cube: 45% used. Docker images are the largest consumer.")
        with open(commis_dir / "metadata.json", "w") as f:
            json.dump(
                {
                    "commis_id": "test-commis-resume-001",
                    "status": "success",
                    "summary": "Cube is at 45% disk usage, Docker is largest consumer.",
                    "owner_id": test_user.id,
                },
                f,
            )

        # Phase 2: Second spawn_commis with DIFFERENT task
        # With exact matching only, this creates a new commis (expected behavior)
        result2 = await spawn_commis_async("Check disk space on cube real quick", model=TEST_COMMIS_MODEL)

        # Count commis AFTER second spawn
        db_session.expire_all()  # Force refresh
        commis_after = db_session.query(CommisJob).filter(CommisJob.concierge_course_id == run.id).all()

        # Different tasks = different commis (exact matching only)
        assert len(commis_after) == 2, (
            f"Expected 2 commis for different tasks, got {len(commis_after)}\n"
            f"Commis: {[(w.task, w.status) for w in commis_after]}\n"
            f"First spawn result: {result1}\n"
            f"Second spawn result: {result2}"
        )

        # Second call creates new commis (different task = no cache hit)
        assert "queued successfully" in result2, f"Second spawn should create new job (different task), got: {result2}"

    finally:
        reset_concierge_context(sup_token)
