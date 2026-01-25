"""Tests for the SupervisorService - manages supervisor agent and thread lifecycle."""

import tempfile

import pytest

from tests.conftest import TEST_WORKER_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.enums import RunStatus
from zerg.models.models import AgentRun
from zerg.services.supervisor_context import get_next_seq
from zerg.services.supervisor_context import get_supervisor_context
from zerg.services.supervisor_context import reset_seq
from zerg.services.supervisor_context import reset_supervisor_context
from zerg.services.supervisor_context import set_supervisor_context
from zerg.services.supervisor_service import SUPERVISOR_THREAD_TYPE
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


class TestSupervisorService:
    """Test suite for SupervisorService."""

    def test_get_or_create_supervisor_agent_creates_new(self, db_session, test_user):
        """Test that a new supervisor agent is created when none exists."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        assert agent is not None
        assert agent.name == "Supervisor"
        assert agent.owner_id == test_user.id
        assert agent.config.get("is_supervisor") is True
        assert "spawn_commis" in agent.allowed_tools
        assert "list_commis" in agent.allowed_tools
        # V1.1: knowledge_search should be available to supervisor
        assert "knowledge_search" in agent.allowed_tools
        # V1.2: web research tools should be available to supervisor
        assert "web_search" in agent.allowed_tools
        assert "web_fetch" in agent.allowed_tools

    def test_get_or_create_supervisor_agent_returns_existing(self, db_session, test_user):
        """Test that existing supervisor agent is returned on subsequent calls."""
        service = SupervisorService(db_session)

        # Create first time
        agent1 = service.get_or_create_supervisor_agent(test_user.id)
        agent1_id = agent1.id

        # Get again - should return same agent
        agent2 = service.get_or_create_supervisor_agent(test_user.id)

        assert agent2.id == agent1_id

    def test_get_or_create_supervisor_thread_creates_new(self, db_session, test_user):
        """Test that a new supervisor thread is created when none exists."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        assert thread is not None
        assert thread.thread_type == SUPERVISOR_THREAD_TYPE
        assert thread.agent_id == agent.id
        assert thread.title == "Supervisor"

    def test_get_or_create_supervisor_thread_returns_existing(self, db_session, test_user):
        """Test that existing supervisor thread is returned on subsequent calls."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        # Create first time
        thread1 = service.get_or_create_supervisor_thread(test_user.id, agent)
        thread1_id = thread1.id

        # Get again - should return same thread (one brain per user)
        thread2 = service.get_or_create_supervisor_thread(test_user.id, agent)

        assert thread2.id == thread1_id

    def test_supervisor_per_user_isolation(self, db_session, test_user, other_user):
        """Test that each user gets their own supervisor agent and thread."""
        service = SupervisorService(db_session)

        # Get supervisor for test_user
        agent1 = service.get_or_create_supervisor_agent(test_user.id)
        thread1 = service.get_or_create_supervisor_thread(test_user.id, agent1)

        # Get supervisor for other_user
        agent2 = service.get_or_create_supervisor_agent(other_user.id)
        thread2 = service.get_or_create_supervisor_thread(other_user.id, agent2)

        # Should be different agents and threads
        assert agent1.id != agent2.id
        assert thread1.id != thread2.id

        # Each owned by their respective user
        assert agent1.owner_id == test_user.id
        assert agent2.owner_id == other_user.id

    def test_supervisor_agent_has_correct_tools(self, db_session, test_user):
        """Test that supervisor agent is configured with correct tools."""
        service = SupervisorService(db_session)

        agent = service.get_or_create_supervisor_agent(test_user.id)

        expected_tools = [
            "spawn_commis",
            "list_commis",
            "read_commis_result",
            "read_commis_file",
            "grep_commis",
            "get_commis_metadata",
            "get_current_time",
            "http_request",
            "send_email",
        ]

        for tool in expected_tools:
            assert tool in agent.allowed_tools, f"Missing tool: {tool}"

    def test_get_or_create_supervisor_thread_creates_agent_if_needed(self, db_session, test_user):
        """Test that thread creation also creates agent if not provided."""
        service = SupervisorService(db_session)

        # Call without providing agent - should create both
        thread = service.get_or_create_supervisor_thread(test_user.id, agent=None)

        assert thread is not None
        assert thread.thread_type == SUPERVISOR_THREAD_TYPE

        # Verify agent was created
        agent = service.get_or_create_supervisor_agent(test_user.id)
        assert thread.agent_id == agent.id


class TestSupervisorContext:
    """Tests for supervisor context (run_id threading)."""

    def test_supervisor_context_default_is_none(self):
        """Test that supervisor context defaults to None."""
        assert get_supervisor_context() is None

    def test_supervisor_context_set_and_get(self):
        """Test setting and getting supervisor context."""
        token = set_supervisor_context(run_id=123, owner_id=1, message_id="test-msg-1")
        try:
            ctx = get_supervisor_context()
            assert ctx is not None
            assert ctx.run_id == 123
            assert ctx.owner_id == 1
            assert ctx.message_id == "test-msg-1"
        finally:
            reset_supervisor_context(token)

        # After reset, should be back to default
        assert get_supervisor_context() is None

    def test_supervisor_context_reset_restores_previous(self):
        """Test that reset restores previous value."""
        # Set first value
        token1 = set_supervisor_context(run_id=100, owner_id=1, message_id="msg-100")
        ctx1 = get_supervisor_context()
        assert ctx1 is not None
        assert ctx1.run_id == 100

        # Set second value
        token2 = set_supervisor_context(run_id=200, owner_id=1, message_id="msg-200")
        ctx2 = get_supervisor_context()
        assert ctx2 is not None
        assert ctx2.run_id == 200

        # Reset second - should restore first
        reset_supervisor_context(token2)
        ctx_after = get_supervisor_context()
        assert ctx_after is not None
        assert ctx_after.run_id == 100

        # Reset first - should restore None
        reset_supervisor_context(token1)
        assert get_supervisor_context() is None

    def test_seq_counter_starts_at_one(self):
        """Test that seq counter starts at 1 for a new run_id."""
        run_id = 999
        try:
            assert get_next_seq(run_id) == 1
        finally:
            reset_seq(run_id)

    def test_seq_counter_increments(self):
        """Test that seq counter increments monotonically."""
        run_id = 1001
        try:
            assert get_next_seq(run_id) == 1
            assert get_next_seq(run_id) == 2
            assert get_next_seq(run_id) == 3
        finally:
            reset_seq(run_id)

    def test_seq_counter_per_run_isolation(self):
        """Test that different run_ids have separate counters."""
        run_id_a = 2001
        run_id_b = 2002
        try:
            # Both should start at 1
            assert get_next_seq(run_id_a) == 1
            assert get_next_seq(run_id_b) == 1

            # Incrementing one doesn't affect the other
            assert get_next_seq(run_id_a) == 2
            assert get_next_seq(run_id_a) == 3
            assert get_next_seq(run_id_b) == 2
        finally:
            reset_seq(run_id_a)
            reset_seq(run_id_b)

    def test_seq_reset_clears_counter(self):
        """Test that reset_seq clears the counter for a run_id."""
        run_id = 3001
        try:
            assert get_next_seq(run_id) == 1
            assert get_next_seq(run_id) == 2
            reset_seq(run_id)
            # After reset, should start at 1 again
            assert get_next_seq(run_id) == 1
        finally:
            reset_seq(run_id)


class TestWorkerSupervisorCorrelation:
    """Tests for worker-supervisor correlation via run_id."""

    def test_spawn_commis_stores_supervisor_run_id(self, db_session, test_user, credential_context, temp_artifact_path):
        """Test that spawn_commis stores supervisor_run_id from context."""
        from tests.conftest import TEST_WORKER_MODEL
        from zerg.models.models import WorkerJob
        from zerg.tools.builtin.supervisor_tools import spawn_commis

        # Create a real supervisor agent and run for FK constraint
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Create a run
        from zerg.models.enums import RunTrigger

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.API,
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        # Set supervisor context with real run_id
        token = set_supervisor_context(run_id=run.id, owner_id=test_user.id, message_id="test-message-id")
        try:
            result = spawn_commis(task="Test task", model=TEST_WORKER_MODEL)
            assert "queued successfully" in result

            # Find the created job
            job = db_session.query(WorkerJob).filter(WorkerJob.task == "Test task").first()
            assert job is not None
            assert job.supervisor_run_id == run.id
        finally:
            reset_supervisor_context(token)

    def test_spawn_commis_without_context_has_null_supervisor_run_id(
        self, db_session, test_user, credential_context, temp_artifact_path
    ):
        """Test that spawn_commis without context sets supervisor_run_id to None."""
        from tests.conftest import TEST_WORKER_MODEL
        from zerg.models.models import WorkerJob
        from zerg.tools.builtin.supervisor_tools import spawn_commis

        # Ensure no supervisor context
        assert get_supervisor_context() is None

        result = spawn_commis(task="Standalone task", model=TEST_WORKER_MODEL)
        assert "queued successfully" in result

        # Find the created job
        job = db_session.query(WorkerJob).filter(WorkerJob.task == "Standalone task").first()
        assert job is not None
        assert job.supervisor_run_id is None

    # NOTE: test_run_continuation_inherits_model was removed during the supervisor
    # resume refactor (Jan 2026). The continuation pattern now uses
    # AgentInterrupted + AgentRunner.run_continuation instead of separate runs.
    # See: docs/work/supervisor-continuation-refactor.md


class TestRecentWorkerHistoryInjection:
    """Tests for v2.0 recent worker history auto-injection.

    This feature injects recent worker results into supervisor context
    to prevent redundant worker spawns.
    """

    def test_build_recent_worker_context_no_workers(self, db_session, test_user):
        """Should return None when no recent workers exist."""
        service = SupervisorService(db_session)
        context, jobs_to_ack = service._build_recent_worker_context(test_user.id)
        assert context is None
        assert jobs_to_ack == []

    def test_build_recent_worker_context_with_workers(self, db_session, test_user, temp_artifact_path):
        """Should return formatted context when recent workers exist."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import WorkerJob

        # Create a recent worker job
        job = WorkerJob(
            owner_id=test_user.id,
            task="Check disk usage on cube",
            model=TEST_WORKER_MODEL,
            status="success",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        service = SupervisorService(db_session)
        context, jobs_to_ack = service._build_recent_worker_context(test_user.id)

        assert context is not None
        assert "Worker Inbox" in context
        assert f"Job {job.id}" in context
        assert "SUCCESS" in context
        assert "Check disk usage" in context
        # Unacknowledged job should be in the list to acknowledge
        assert job.id in jobs_to_ack

    def test_build_recent_worker_context_respects_limit(self, db_session, test_user, temp_artifact_path):
        """Should only return up to RECENT_WORKER_HISTORY_LIMIT workers."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import WorkerJob
        from zerg.services.supervisor_service import RECENT_WORKER_HISTORY_LIMIT

        # Create more workers than the limit
        for i in range(RECENT_WORKER_HISTORY_LIMIT + 3):
            job = WorkerJob(
                owner_id=test_user.id,
                task=f"Task {i}",
                model=TEST_WORKER_MODEL,
                status="success",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(job)
        db_session.commit()

        service = SupervisorService(db_session)
        context, jobs_to_ack = service._build_recent_worker_context(test_user.id)

        assert context is not None
        # Count how many "Job X" entries
        job_count = context.count("Job ")
        assert job_count == RECENT_WORKER_HISTORY_LIMIT
        # Should have RECENT_WORKER_HISTORY_LIMIT jobs to acknowledge
        assert len(jobs_to_ack) == RECENT_WORKER_HISTORY_LIMIT

    def test_build_recent_worker_context_includes_running(self, db_session, test_user, temp_artifact_path):
        """Should include running workers in context."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import WorkerJob

        job = WorkerJob(
            owner_id=test_user.id,
            task="Long running investigation",
            model=TEST_WORKER_MODEL,
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()

        service = SupervisorService(db_session)
        context, jobs_to_ack = service._build_recent_worker_context(test_user.id)

        assert context is not None
        assert "RUNNING" in context
        assert "Long running investigation" in context
        # Running jobs are not acknowledged (only completed jobs)
        assert jobs_to_ack == []

    def test_build_recent_worker_context_includes_marker(self, db_session, test_user, temp_artifact_path):
        """Context should include marker for cleanup identification."""
        from datetime import datetime
        from datetime import timezone

        from zerg.models.models import WorkerJob
        from zerg.services.supervisor_service import RECENT_WORKER_CONTEXT_MARKER

        job = WorkerJob(
            owner_id=test_user.id,
            task="Test task",
            model=TEST_WORKER_MODEL,
            status="success",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(job)
        db_session.commit()

        service = SupervisorService(db_session)
        context, jobs_to_ack = service._build_recent_worker_context(test_user.id)

        assert context is not None
        assert RECENT_WORKER_CONTEXT_MARKER in context
        # Completed job should be in acknowledgement list
        assert job.id in jobs_to_ack

    def test_cleanup_stale_worker_context(self, db_session, test_user, temp_artifact_path):
        """Should delete messages containing the marker (older than min_age)."""
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.supervisor_service import RECENT_WORKER_CONTEXT_MARKER

        # Create supervisor agent and thread
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Add a stale context message
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_WORKER_CONTEXT_MARKER}\n## Stale context",
            processed=True,
        )
        db_session.commit()

        # Verify message exists
        messages_before = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages_before) == 1

        # Cleanup with min_age_seconds=0 to delete immediately (for testing)
        deleted_count = service._cleanup_stale_worker_context(thread.id, min_age_seconds=0)
        db_session.commit()

        assert deleted_count == 1

        # Verify message is gone
        messages_after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages_after) == 0

    def test_cleanup_does_not_affect_other_messages(self, db_session, test_user, temp_artifact_path):
        """Cleanup should only delete messages with the marker."""
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.supervisor_service import RECENT_WORKER_CONTEXT_MARKER

        # Create supervisor agent and thread
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Count existing messages (thread may have a system prompt)
        initial_count = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
            )
            .count()
        )

        # Add a normal system message (no marker)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content="Important system instructions",
            processed=True,
        )
        # Add a stale context message (with marker)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_WORKER_CONTEXT_MARKER}\n## Stale context",
            processed=True,
        )
        db_session.commit()

        # Verify marker message exists
        marker_messages = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(marker_messages) == 1

        # Cleanup with min_age_seconds=0 to delete immediately (for testing)
        deleted_count = service._cleanup_stale_worker_context(thread.id, min_age_seconds=0)
        db_session.commit()

        assert deleted_count == 1

        # Verify marker message is gone
        marker_messages_after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(marker_messages_after) == 0

        # Verify our "Important system instructions" message still exists
        important_msg = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains("Important system instructions"),
            )
            .first()
        )
        assert important_msg is not None

    def test_cleanup_respects_min_age_for_race_condition_protection(self, db_session, test_user, temp_artifact_path):
        """Fresh context messages (< min_age) should NOT be deleted.

        This prevents race conditions where concurrent requests could
        delete each other's freshly injected context.
        """
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.supervisor_service import RECENT_WORKER_CONTEXT_MARKER

        # Create supervisor agent and thread
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Add a context message (just created, so fresh)
        crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_WORKER_CONTEXT_MARKER}\n## Fresh context",
            processed=True,
        )
        db_session.commit()

        # Cleanup with default min_age_seconds=5.0
        # Message was just created, so it should NOT be deleted
        deleted_count = service._cleanup_stale_worker_context(thread.id)
        db_session.commit()

        # Should not delete fresh messages (race condition protection)
        assert deleted_count == 0

        # Message should still exist
        messages = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(messages) == 1

    def test_cleanup_removes_older_duplicates_but_keeps_fresh(self, db_session, test_user, temp_artifact_path):
        """Back-to-back requests should not accumulate multiple context blocks.

        When there are multiple context messages and the newest is fresh,
        only the newest should be kept (all older ones deleted).
        """
        from zerg.crud import crud
        from zerg.models.models import ThreadMessage
        from zerg.services.supervisor_service import RECENT_WORKER_CONTEXT_MARKER

        # Create supervisor agent and thread
        service = SupervisorService(db_session)
        agent = service.get_or_create_supervisor_agent(test_user.id)
        thread = service.get_or_create_supervisor_thread(test_user.id, agent)

        # Simulate back-to-back requests by adding multiple context messages
        # First message (older)
        msg1 = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_WORKER_CONTEXT_MARKER}\n## Old context 1",
            processed=True,
        )
        # Second message (newer, fresh - should be kept)
        msg2 = crud.create_thread_message(
            db=db_session,
            thread_id=thread.id,
            role="system",
            content=f"{RECENT_WORKER_CONTEXT_MARKER}\n## Fresh context 2",
            processed=True,
        )
        db_session.commit()

        # Verify both exist
        before = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(before) == 2

        # Cleanup with default min_age - newest is fresh so kept, older deleted
        deleted_count = service._cleanup_stale_worker_context(thread.id)
        db_session.commit()

        # Should have deleted the older one
        assert deleted_count == 1

        # Only the fresh one should remain
        after = (
            db_session.query(ThreadMessage)
            .filter(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.content.contains(RECENT_WORKER_CONTEXT_MARKER),
            )
            .all()
        )
        assert len(after) == 1
        assert "Fresh context 2" in after[0].content
