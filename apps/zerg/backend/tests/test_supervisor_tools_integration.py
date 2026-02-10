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
from zerg.tools import ImmutableToolRegistry
from zerg.tools.builtin import BUILTIN_TOOLS


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
