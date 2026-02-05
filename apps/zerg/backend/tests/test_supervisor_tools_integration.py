"""Integration tests for oikos tools with real fiche execution."""

import tempfile

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.crud import crud
from zerg.managers.fiche_runner import FicheRunner
from zerg.services.thread_service import ThreadService
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.registry import ImmutableToolRegistry


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path and set environment variable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("LONGHOUSE_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def oikos_fiche(db_session, test_user):
    """Create a oikos fiche with oikos tools enabled."""
    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Oikos Fiche",
        model=TEST_MODEL,  # Use smarter model - gpt-5-mini is unreliable for tool calling
        system_instructions=(
            "You are a oikos fiche that MUST delegate ALL tasks to commis. "
            "You have access to the spawn_commis tool. "
            "IMPORTANT: When asked to do anything, you MUST call spawn_commis immediately. "
            "Never respond with text - always use the spawn_commis tool."
        ),
        task_instructions="",
    )
    # Set allowed_tools - required for tools to be passed to the LLM
    fiche.allowed_tools = [
        "spawn_commis",
        "list_commiss",
        "read_commis_result",
        "read_commis_file",
        "grep_commiss",
        "get_commis_metadata",
    ]
    db_session.commit()
    db_session.refresh(fiche)
    return fiche


@pytest.mark.asyncio
async def test_oikos_spawns_commis_via_tool(oikos_fiche, db_session, test_user, temp_artifact_path):
    """Test that a oikos fiche can use spawn_commis tool (triggers interrupt for barrier).

    When spawn_commis is called (even in parallel), the oikos should raise FicheInterrupted
    with interrupt_value containing job_ids for barrier creation. This allows the
    oikos_service to create a CommisBarrier and set the run to WAITING state.

    We verify:
    1. FicheInterrupted is raised with correct interrupt_value
    2. A CommisJob was created with status='created' (not 'queued' yet)
    """
    from zerg.managers.fiche_runner import FicheInterrupted
    from zerg.models.models import CommisJob

    # Create a thread for the oikos
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        oikos_fiche,
        title="Test Oikos Thread",
        thread_type="manual",
        active=False,
    )

    # Add user message asking oikos to spawn a commis
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="Spawn a commis to calculate 10 + 15",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=oikos_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the oikos fiche - spawn_commis should raise FicheInterrupted for barrier creation
        runner = FicheRunner(oikos_fiche)

        with pytest.raises(FicheInterrupted) as exc_info:
            await runner.run_thread(db_session, thread)

        # Verify the interrupt has correct structure for barrier creation
        interrupt_value = exc_info.value.interrupt_value
        assert interrupt_value is not None, "FicheInterrupted should have interrupt_value"
        assert interrupt_value.get("type") == "commiss_pending", (
            f"interrupt_value.type should be 'commiss_pending', got: {interrupt_value.get('type')}"
        )
        assert "job_ids" in interrupt_value, "interrupt_value should contain job_ids"
        assert "created_jobs" in interrupt_value, "interrupt_value should contain created_jobs"
        assert len(interrupt_value["job_ids"]) >= 1, "Should have at least one job_id"

        # Verify a commis JOB was created
        jobs = db_session.query(CommisJob).filter(CommisJob.owner_id == test_user.id).all()
        assert len(jobs) >= 1, "At least one commis job should have been created"

        # Verify job is in 'created' status (NOT 'queued' yet - oikos_service handles that)
        job = jobs[0]
        assert job.status == "created", (
            f"Commis job should be in 'created' status (two-phase commit), got: {job.status}"
        )
        assert len(job.task) > 0, "Commis job should have a task"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_oikos_can_list_commiss(oikos_fiche, db_session, test_user, temp_artifact_path):
    """Test that a oikos can use list_commiss tool."""
    from datetime import datetime
    from datetime import timezone

    from zerg.models.models import CommisJob

    # Create a CommisJob record (simulating a queued job)
    commis_job = CommisJob(
        owner_id=test_user.id,
        task="Test task for listing",
        model=TEST_COMMIS_MODEL,
        status="queued",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(commis_job)
    db_session.commit()

    # Create a thread for the oikos
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        oikos_fiche,
        title="Test List Commis",
        thread_type="manual",
        active=False,
    )

    # Add user message asking oikos to list commis
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="Use the list_commiss tool to show me all recent commis",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=oikos_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the oikos fiche
        fiche_runner = FicheRunner(oikos_fiche)
        messages = await fiche_runner.run_thread(db_session, thread)

        # Verify the oikos called list_commiss
        list_commiss_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "list_commiss":
                        list_commiss_called = True
                        break

        assert list_commiss_called, "Oikos should have called list_commiss"

        # Check that the response mentions the commis
        final_message = messages[-1]
        assert final_message.role == "assistant"
        # The response should contain information about commis
        assert len(final_message.content) > 0

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_oikos_reads_commis_result(oikos_fiche, db_session, test_user, temp_artifact_path):
    """Test that a oikos can read commis results."""
    from datetime import datetime
    from datetime import timezone

    from zerg.models.models import CommisJob
    from zerg.services.commis_runner import CommisRunner

    # First spawn a commis directly via CommisRunner (creates artifacts)
    artifact_store = CommisArtifactStore(base_path=temp_artifact_path)
    runner = CommisRunner(artifact_store=artifact_store)

    result = await runner.run_commis(
        db=db_session,
        task="Calculate 5 * 8",
        fiche=None,
        fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
    )

    commis_id = result.commis_id

    # Create a CommisJob record linking to this commis (so tools can find it)
    commis_job = CommisJob(
        owner_id=test_user.id,
        task="Calculate 5 * 8",
        model=TEST_COMMIS_MODEL,
        status="success",
        commis_id=commis_id,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(commis_job)
    db_session.commit()
    db_session.refresh(commis_job)

    job_id = commis_job.id

    # Create a thread for the oikos
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        oikos_fiche,
        title="Test Read Commis Result",
        thread_type="manual",
        active=False,
    )

    # Add user message asking oikos to read the commis result (using job_id)
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content=f"Read the result from commis job {job_id}",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=oikos_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the oikos fiche
        fiche_runner = FicheRunner(oikos_fiche)
        messages = await fiche_runner.run_thread(db_session, thread)

        # Verify the oikos called read_commis_result
        read_commis_result_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "read_commis_result":
                        read_commis_result_called = True
                        break

        assert read_commis_result_called, "Oikos should have called read_commis_result"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_tools_registered_in_builtin(db_session):
    """Test that oikos tools are registered in BUILTIN_TOOLS."""
    # Build registry
    registry = ImmutableToolRegistry.build([BUILTIN_TOOLS])

    # Verify all oikos tools are registered
    assert registry.get("spawn_commis") is not None
    assert registry.get("list_commiss") is not None
    assert registry.get("read_commis_result") is not None
    assert registry.get("read_commis_file") is not None
    assert registry.get("grep_commiss") is not None
    assert registry.get("get_commis_metadata") is not None

    # Verify tool descriptions
    spawn_tool = registry.get("spawn_commis")
    assert "delegate" in spawn_tool.description.lower() or "spawn" in spawn_tool.description.lower()


@pytest.mark.asyncio
async def test_read_commis_result_includes_duration(oikos_fiche, db_session, test_user, temp_artifact_path):
    """Test that read_commis_result returns duration_ms from completed commis (Tier 1 visibility)."""
    from datetime import datetime
    from datetime import timezone

    from zerg.connectors.context import set_credential_resolver
    from zerg.connectors.resolver import CredentialResolver
    from zerg.models.models import CommisJob
    from zerg.services.commis_runner import CommisRunner

    # Create and run a commis directly via CommisRunner
    # Set up credential resolver context FIRST (needed for commis execution)
    resolver = CredentialResolver(fiche_id=oikos_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        artifact_store = CommisArtifactStore(base_path=temp_artifact_path)
        runner = CommisRunner(artifact_store=artifact_store)

        result = await runner.run_commis(
            db=db_session,
            task="Calculate 7 * 6",
            fiche=None,
            fiche_config={"model": TEST_COMMIS_MODEL, "owner_id": test_user.id},
        )

        commis_id = result.commis_id

        # Create a CommisJob record linking to this commis
        commis_job = CommisJob(
            owner_id=test_user.id,
            task="Calculate 7 * 6",
            model=TEST_COMMIS_MODEL,
            status="success",
            commis_id=commis_id,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db_session.add(commis_job)
        db_session.commit()
        db_session.refresh(commis_job)

        job_id = commis_job.id

        # Call read_commis_result_async directly (to preserve context in same async loop)
        from zerg.tools.builtin.oikos_tools import read_commis_result_async

        result_text = await read_commis_result_async(str(job_id))

        # Verify the result includes a formatted execution time
        assert "Execution time:" in result_text, f"Result should include execution time. Got: {result_text}"

        import re

        duration_ms: float | None = None

        # Formats: "123ms" or "1.3s"
        match = re.search(r"Execution time: (\d+)(ms|s)\b", result_text)
        if match:
            value = float(match.group(1))
            unit = match.group(2)
            duration_ms = value if unit == "ms" else value * 1000
        else:
            match = re.search(r"Execution time: (\d+\.\d+)s\b", result_text)
            if match:
                duration_ms = float(match.group(1)) * 1000
            else:
                # Format: "2m 15s"
                match = re.search(r"Execution time: (\d+)m (\d+)s\b", result_text)
                if match:
                    minutes = int(match.group(1))
                    seconds = int(match.group(2))
                    duration_ms = (minutes * 60 + seconds) * 1000

        assert duration_ms is not None, "Should find duration in result"
        assert duration_ms > 0, "Duration should be greater than 0ms"

        # Verify we still get the actual result text
        assert "42" in result_text or "result" in result_text.lower(), "Result should include actual commis output"

    finally:
        set_credential_resolver(None)
