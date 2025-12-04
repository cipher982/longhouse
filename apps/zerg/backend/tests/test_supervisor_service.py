"""Tests for the SupervisorService - manages supervisor agent and thread lifecycle."""

import tempfile

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.models import AgentRun
from zerg.models.enums import RunStatus
from zerg.services.supervisor_context import (
    get_supervisor_run_id,
    set_supervisor_run_id,
    reset_supervisor_run_id,
)
from zerg.services.supervisor_service import (
    SupervisorService,
    SUPERVISOR_THREAD_TYPE,
)


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
        assert "spawn_worker" in agent.allowed_tools
        assert "list_workers" in agent.allowed_tools

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
            "spawn_worker",
            "list_workers",
            "read_worker_result",
            "read_worker_file",
            "grep_workers",
            "get_worker_metadata",
            "get_current_time",
            "http_request",
            "send_email",
        ]

        for tool in expected_tools:
            assert tool in agent.allowed_tools, f"Missing tool: {tool}"

    def test_get_or_create_supervisor_thread_creates_agent_if_needed(
        self, db_session, test_user
    ):
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
        assert get_supervisor_run_id() is None

    def test_supervisor_context_set_and_get(self):
        """Test setting and getting supervisor run_id."""
        token = set_supervisor_run_id(123)
        try:
            assert get_supervisor_run_id() == 123
        finally:
            reset_supervisor_run_id(token)

        # After reset, should be back to default
        assert get_supervisor_run_id() is None

    def test_supervisor_context_reset_restores_previous(self):
        """Test that reset restores previous value."""
        # Set first value
        token1 = set_supervisor_run_id(100)
        assert get_supervisor_run_id() == 100

        # Set second value
        token2 = set_supervisor_run_id(200)
        assert get_supervisor_run_id() == 200

        # Reset second - should restore first
        reset_supervisor_run_id(token2)
        assert get_supervisor_run_id() == 100

        # Reset first - should restore None
        reset_supervisor_run_id(token1)
        assert get_supervisor_run_id() is None


class TestWorkerSupervisorCorrelation:
    """Tests for worker-supervisor correlation via run_id."""

    def test_spawn_worker_stores_supervisor_run_id(
        self, db_session, test_user, credential_context, temp_artifact_path
    ):
        """Test that spawn_worker stores supervisor_run_id from context."""
        from zerg.tools.builtin.supervisor_tools import spawn_worker
        from zerg.models.models import WorkerJob
        from tests.conftest import TEST_WORKER_MODEL

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
        token = set_supervisor_run_id(run.id)
        try:
            result = spawn_worker(task="Test task", model=TEST_WORKER_MODEL)
            assert "queued successfully" in result

            # Find the created job
            job = db_session.query(WorkerJob).filter(
                WorkerJob.task == "Test task"
            ).first()
            assert job is not None
            assert job.supervisor_run_id == run.id
        finally:
            reset_supervisor_run_id(token)

    def test_spawn_worker_without_context_has_null_supervisor_run_id(
        self, db_session, test_user, credential_context, temp_artifact_path
    ):
        """Test that spawn_worker without context sets supervisor_run_id to None."""
        from zerg.tools.builtin.supervisor_tools import spawn_worker
        from zerg.models.models import WorkerJob
        from tests.conftest import TEST_WORKER_MODEL

        # Ensure no supervisor context
        assert get_supervisor_run_id() is None

        result = spawn_worker(task="Standalone task", model=TEST_WORKER_MODEL)
        assert "queued successfully" in result

        # Find the created job
        job = db_session.query(WorkerJob).filter(
            WorkerJob.task == "Standalone task"
        ).first()
        assert job is not None
        assert job.supervisor_run_id is None
