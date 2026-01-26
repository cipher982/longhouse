"""Integration tests for concierge tools with real fiche execution."""

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
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def concierge_fiche(db_session, test_user):
    """Create a concierge fiche with concierge tools enabled."""
    fiche = crud.create_fiche(
        db=db_session,
        owner_id=test_user.id,
        name="Concierge Fiche",
        model=TEST_MODEL,  # Use smarter model - gpt-5-mini is unreliable for tool calling
        system_instructions=(
            "You are a concierge fiche that MUST delegate ALL tasks to commis. "
            "You have access to the spawn_commis tool. "
            "IMPORTANT: When asked to do anything, you MUST call spawn_commis immediately. "
            "Never respond with text - always use the spawn_commis tool."
        ),
        task_instructions="",
    )
    # Set allowed_tools - required for tools to be passed to the LLM
    fiche.allowed_tools = [
        "spawn_commis",
        "list_commis",
        "read_commis_result",
        "read_commis_file",
        "grep_commis",
        "get_commis_metadata",
    ]
    db_session.commit()
    db_session.refresh(fiche)
    return fiche


@pytest.mark.asyncio
async def test_concierge_spawns_commis_via_tool(concierge_fiche, db_session, test_user, temp_artifact_path):
    """Test that a concierge fiche can use spawn_commis tool (queues job).

    In async model, spawn_commis returns immediately and the concierge continues.
    We verify the job was created and queued.
    """
    from zerg.models.models import CommisJob

    # Create a thread for the concierge
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        concierge_fiche,
        title="Test Concierge Thread",
        thread_type="manual",
        active=False,
    )

    # Add user message asking concierge to spawn a commis
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="Spawn a commis to calculate 10 + 15",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=concierge_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the concierge fiche - spawn_commis returns immediately in async model
        runner = FicheRunner(concierge_fiche)
        messages = await runner.run_thread(db_session, thread)

        # Verify the concierge completed (not interrupted)
        assert messages is not None, "Concierge should return messages"

        # Verify a commis JOB was created
        jobs = db_session.query(CommisJob).filter(CommisJob.owner_id == test_user.id).all()
        assert len(jobs) >= 1, "At least one commis job should have been created"

        # Verify job is in 'queued' status (async model flips to queued immediately)
        job = jobs[0]
        assert job.status == "queued", "Commis job should be in 'queued' status"
        assert len(job.task) > 0, "Commis job should have a task"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_concierge_can_list_commis(concierge_fiche, db_session, test_user, temp_artifact_path):
    """Test that a concierge can use list_commis tool."""
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

    # Create a thread for the concierge
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        concierge_fiche,
        title="Test List Commis",
        thread_type="manual",
        active=False,
    )

    # Add user message asking concierge to list commis
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content="Use the list_commis tool to show me all recent commis",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=concierge_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the concierge fiche
        fiche_runner = FicheRunner(concierge_fiche)
        messages = await fiche_runner.run_thread(db_session, thread)

        # Verify the concierge called list_commis
        list_commis_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "list_commis":
                        list_commis_called = True
                        break

        assert list_commis_called, "Concierge should have called list_commis"

        # Check that the response mentions the commis
        final_message = messages[-1]
        assert final_message.role == "assistant"
        # The response should contain information about commis
        assert len(final_message.content) > 0

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_concierge_reads_commis_result(concierge_fiche, db_session, test_user, temp_artifact_path):
    """Test that a concierge can read commis results."""
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

    # Create a thread for the concierge
    thread = ThreadService.create_thread_with_system_message(
        db_session,
        concierge_fiche,
        title="Test Read Commis Result",
        thread_type="manual",
        active=False,
    )

    # Add user message asking concierge to read the commis result (using job_id)
    crud.create_thread_message(
        db=db_session,
        thread_id=thread.id,
        role="user",
        content=f"Read the result from commis job {job_id}",
        processed=False,
    )

    # Set up credential resolver context
    resolver = CredentialResolver(fiche_id=concierge_fiche.id, db=db_session, owner_id=test_user.id)
    set_credential_resolver(resolver)

    try:
        # Run the concierge fiche
        fiche_runner = FicheRunner(concierge_fiche)
        messages = await fiche_runner.run_thread(db_session, thread)

        # Verify the concierge called read_commis_result
        read_commis_result_called = False

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if tool_call.get("name") == "read_commis_result":
                        read_commis_result_called = True
                        break

        assert read_commis_result_called, "Concierge should have called read_commis_result"

    finally:
        set_credential_resolver(None)


@pytest.mark.asyncio
async def test_tools_registered_in_builtin(db_session):
    """Test that concierge tools are registered in BUILTIN_TOOLS."""
    # Build registry
    registry = ImmutableToolRegistry.build([BUILTIN_TOOLS])

    # Verify all concierge tools are registered
    assert registry.get("spawn_commis") is not None
    assert registry.get("list_commis") is not None
    assert registry.get("read_commis_result") is not None
    assert registry.get("read_commis_file") is not None
    assert registry.get("grep_commis") is not None
    assert registry.get("get_commis_metadata") is not None

    # Verify tool descriptions
    spawn_tool = registry.get("spawn_commis")
    assert "delegate" in spawn_tool.description.lower() or "spawn" in spawn_tool.description.lower()


@pytest.mark.asyncio
async def test_read_commis_result_includes_duration(concierge_fiche, db_session, test_user, temp_artifact_path):
    """Test that read_commis_result returns duration_ms from completed commis (Tier 1 visibility)."""
    from datetime import datetime
    from datetime import timezone

    from zerg.connectors.context import set_credential_resolver
    from zerg.connectors.resolver import CredentialResolver
    from zerg.models.models import CommisJob
    from zerg.services.commis_runner import CommisRunner

    # Create and run a commis directly via CommisRunner
    # Set up credential resolver context FIRST (needed for commis execution)
    resolver = CredentialResolver(fiche_id=concierge_fiche.id, db=db_session, owner_id=test_user.id)
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
        from zerg.tools.builtin.concierge_tools import read_commis_result_async

        result_text = await read_commis_result_async(str(job_id))

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
        assert "42" in result_text or "result" in result_text.lower(), "Result should include actual commis output"

    finally:
        set_credential_resolver(None)
