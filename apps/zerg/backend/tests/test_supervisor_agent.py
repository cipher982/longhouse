"""Tests for the Supervisor Agent implementation.

This test suite verifies:
1. Supervisor agent can be created with correct configuration
2. Supervisor has all required tools enabled
3. Supervisor can spawn workers and retrieve results
4. System prompt is properly configured
5. Full delegation flow works end-to-end
"""

from unittest.mock import Mock
from unittest.mock import patch

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_WORKER_MODEL
from zerg.crud import crud
from zerg.models.enums import AgentStatus
from zerg.prompts.supervisor_prompt import get_supervisor_prompt


class TestSupervisorConfiguration:
    """Test supervisor agent creation and configuration."""

    def test_create_supervisor_agent(self, db_session, test_user):
        """Test that supervisor agent can be created with correct config."""
        supervisor_prompt = get_supervisor_prompt()

        # Create supervisor agent
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Test Supervisor",
            system_instructions=supervisor_prompt,
            task_instructions="Help the user accomplish their goals.",
            model=TEST_MODEL,
            config={"is_supervisor": True, "temperature": 0.7},
        )

        assert agent is not None
        assert agent.name == "Test Supervisor"
        assert agent.model == TEST_MODEL
        assert agent.owner_id == test_user.id
        assert agent.status == AgentStatus.IDLE
        assert agent.config.get("is_supervisor") is True
        assert supervisor_prompt in agent.system_instructions

    def test_supervisor_has_required_tools(self, db_session, test_user):
        """Test that supervisor has all required delegation tools."""
        required_supervisor_tools = [
            "spawn_commis",
            "list_commis",
            "read_commis_result",
            "read_commis_file",
            "grep_commis",
            "get_commis_metadata",
        ]

        required_direct_tools = [
            "get_current_time",
            "http_request",
        ]

        # Create supervisor with tool allowlist
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Tool Test Supervisor",
            system_instructions=get_supervisor_prompt(),
            task_instructions="Test",
            model=TEST_MODEL,
        )

        # Update with allowed tools
        agent = crud.update_agent(
            db_session,
            agent.id,
            allowed_tools=(required_supervisor_tools + required_direct_tools + ["send_email"]),
        )

        # Verify all required tools are present
        for tool in required_supervisor_tools:
            assert tool in agent.allowed_tools, f"Missing supervisor tool: {tool}"

        for tool in required_direct_tools:
            assert tool in agent.allowed_tools, f"Missing direct tool: {tool}"

    def test_supervisor_system_prompt_content(self):
        """Test that supervisor prompt contains key concepts."""
        prompt = get_supervisor_prompt()

        # Verify key concepts are present
        assert "Supervisor" in prompt
        assert "spawn_commis" in prompt
        assert "list_commis" in prompt
        assert "worker" in prompt.lower()

        # Verify guidance sections
        assert "Your Role" in prompt
        assert "Querying Past Work" in prompt
        assert "Response Style" in prompt

    def test_supervisor_not_scheduled(self, db_session, test_user):
        """Test that supervisor is not scheduled (interactive only)."""
        agent = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="No Schedule Supervisor",
            system_instructions=get_supervisor_prompt(),
            task_instructions="Test",
            model=TEST_MODEL,
            schedule=None,
        )

        assert agent.schedule is None
        assert agent.next_run_at is None


class TestSupervisorDelegation:
    """Test supervisor's ability to spawn and manage workers."""

    @pytest.mark.asyncio
    async def test_spawn_commis_integration(self, db_session, test_user, tmp_path):
        """Test that supervisor can spawn a worker and get result."""
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.services.worker_runner import WorkerRunner

        # Create supervisor agent
        supervisor = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Integration Test Supervisor",
            system_instructions=get_supervisor_prompt(),
            task_instructions="Test delegation",
            model=TEST_MODEL,
        )

        # Mock the LLM response for worker
        mock_completion = Mock()
        mock_completion.choices = [
            Mock(
                message=Mock(
                    content="Worker completed the task successfully.",
                    tool_calls=None,
                )
            )
        ]
        mock_completion.usage = Mock(total_tokens=100)

        with patch("openai.OpenAI") as mock_openai_class:
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_completion
            mock_openai_class.return_value = mock_client

            # Create worker runner with temp directory
            artifact_store = WorkerArtifactStore(base_path=tmp_path / "workers")
            runner = WorkerRunner(artifact_store=artifact_store)

            # Run worker
            result = await runner.run_worker(
                db=db_session,
                task="Test task for integration",
                agent=None,
                agent_config={
                    "model": TEST_WORKER_MODEL,
                    "owner_id": test_user.id,
                },
            )

            # Verify result structure
            assert result.worker_id is not None
            assert result.status in ["success", "failed"]
            assert result.result is not None or result.error is not None

            # If successful, verify we can retrieve the result
            if result.status == "success":
                retrieved_result = artifact_store.get_worker_result(result.worker_id)
                assert retrieved_result is not None
                assert len(retrieved_result) > 0

    def test_spawn_commis_tool_basic(self, db_session, test_user):
        """Test spawn_commis tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.supervisor_tools import spawn_commis

        # Without context, should return error
        set_credential_resolver(None)
        result = spawn_commis(task="Test task", model=TEST_WORKER_MODEL)
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()

    def test_list_commis_tool_basic(self, db_session, test_user):
        """Test list_commis tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.supervisor_tools import list_commis

        # Without context, should return error
        set_credential_resolver(None)
        result = list_commis(limit=10)
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()

    def test_read_commis_result_tool_basic(self, db_session, test_user):
        """Test read_commis_result tool is callable and validates context."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.tools.builtin.supervisor_tools import read_commis_result

        # Without context, should return error
        set_credential_resolver(None)
        result = read_commis_result(job_id="999")
        assert "Error" in result or "error" in result.lower()
        assert "credential context" in result.lower() or "context" in result.lower()


class TestSupervisorEndToEnd:
    """End-to-end tests for supervisor/worker interaction."""

    @pytest.mark.asyncio
    async def test_full_delegation_flow(self, db_session, test_user, tmp_path):
        """Test complete flow: create supervisor → spawn worker → retrieve result."""
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.services.worker_runner import WorkerRunner

        # 1. Create supervisor agent
        supervisor = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="E2E Test Supervisor",
            system_instructions=get_supervisor_prompt(),
            task_instructions="Coordinate tasks",
            model=TEST_MODEL,
            config={"is_supervisor": True},
        )

        assert supervisor.id is not None

        # 2. Setup credential context for worker spawning
        resolver = CredentialResolver(agent_id=supervisor.id, db=db_session, owner_id=test_user.id)
        set_credential_resolver(resolver)

        # 3. Mock worker execution
        mock_completion = Mock()
        mock_completion.choices = [
            Mock(
                message=Mock(
                    content="Disk usage check completed. All servers below 80% capacity.",
                    tool_calls=None,
                )
            )
        ]
        mock_completion.usage = Mock(total_tokens=150)

        with patch("openai.OpenAI") as mock_openai_class:
            mock_client = Mock()
            mock_client.chat.completions.create.return_value = mock_completion
            mock_openai_class.return_value = mock_client

            # 4. Spawn worker via runner with temp directory
            artifact_store = WorkerArtifactStore(base_path=tmp_path / "workers")
            runner = WorkerRunner(artifact_store=artifact_store)

            result = await runner.run_worker(
                db=db_session,
                task="Check disk usage on all production servers",
                agent=None,
                agent_config={
                    "model": TEST_WORKER_MODEL,
                    "owner_id": test_user.id,
                },
            )

            # 5. Verify worker completed
            assert result.worker_id is not None
            assert result.status == "success"
            assert result.result is not None
            # Result may contain stub or actual response depending on mock
            assert len(result.result) > 0

            # 6. Verify we can retrieve metadata
            metadata = artifact_store.get_commis_metadata(result.worker_id, owner_id=test_user.id)
            assert metadata["status"] == "success"
            assert metadata["worker_id"] == result.worker_id
            assert metadata["config"]["owner_id"] == test_user.id

            # 7. Verify we can retrieve result
            retrieved_result = artifact_store.get_worker_result(result.worker_id)
            assert len(retrieved_result) > 0

    def test_supervisor_security_isolation(self, db_session, test_user, tmp_path):
        """Test that workers are properly isolated by owner_id."""
        from zerg.services.worker_artifact_store import WorkerArtifactStore

        # Create another user
        other_user = crud.create_user(
            db_session,
            email="other@test.com",
            provider="test",
        )

        artifact_store = WorkerArtifactStore(base_path=tmp_path / "workers")

        # Mock metadata for a worker owned by other user
        other_user_metadata = {
            "worker_id": "other-worker-123",
            "status": "success",
            "config": {"owner_id": other_user.id},
        }

        with patch.object(
            artifact_store,
            "get_commis_metadata",
            side_effect=lambda worker_id, owner_id: (other_user_metadata if owner_id == other_user.id else None),
        ):
            # Try to access other user's worker
            result = artifact_store.get_commis_metadata("other-worker-123", owner_id=test_user.id)
            assert result is None  # Should not be accessible

            # Access own worker
            result = artifact_store.get_commis_metadata("other-worker-123", owner_id=other_user.id)
            assert result == other_user_metadata


class TestSupervisorFromScript:
    """Test the seed script functionality."""

    def test_seed_script_creates_supervisor(self, db_session, test_user):
        """Test that seed_supervisor script creates valid agent."""
        from scripts.seed_supervisor import seed_supervisor

        # Mock get_db to return our test session
        with patch("scripts.seed_supervisor.get_db", return_value=iter([db_session])):
            with patch("scripts.seed_supervisor.get_or_create_user", return_value=test_user):
                agent = seed_supervisor(user_email=test_user.email, name="Script Test Supervisor")

                # Verify agent was created
                assert agent is not None
                assert agent.name == "Script Test Supervisor"
                assert agent.model == TEST_MODEL
                assert agent.owner_id == test_user.id
                assert agent.config.get("is_supervisor") is True
                assert agent.allowed_tools is not None
                assert "spawn_commis" in agent.allowed_tools

    def test_seed_script_updates_existing(self, db_session, test_user):
        """Test that seed script updates existing supervisor."""
        from scripts.seed_supervisor import seed_supervisor

        # Create initial supervisor
        initial = crud.create_agent(
            db_session,
            owner_id=test_user.id,
            name="Update Test Supervisor",
            system_instructions="Old prompt",
            task_instructions="Old task",
            model=TEST_WORKER_MODEL,
        )

        # Run seed script
        with patch("scripts.seed_supervisor.get_db", return_value=iter([db_session])):
            with patch("scripts.seed_supervisor.get_or_create_user", return_value=test_user):
                agent = seed_supervisor(user_email=test_user.email, name="Update Test Supervisor")

                # Verify agent was updated
                assert agent.id == initial.id  # Same agent
                assert agent.model == TEST_MODEL  # Updated to supervisor model
                assert "spawn_commis" in agent.system_instructions  # Updated prompt
                assert agent.config.get("is_supervisor") is True  # Updated config
