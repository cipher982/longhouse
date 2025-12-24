"""E2E test for worker SSH fallback behavior.

This test verifies that workers correctly fall back from runner_exec to ssh_exec
when runner_exec fails with a validation error (e.g., "Runner not found").

The test uses mocked tools to simulate the fallback scenario without requiring
a real LLM or actual SSH connections.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from zerg.services.worker_runner import WorkerRunner
from zerg.services.worker_artifact_store import WorkerArtifactStore
from tests.conftest import TEST_WORKER_MODEL


class TestWorkerSSHFallback:
    """Tests for worker SSH fallback when runner_exec fails.

    These tests verify the LOGIC of SSH fallback, not the full E2E flow.
    We test that:
    1. runner_exec failures are non-critical (allows fallback)
    2. Workers can succeed even when runner_exec fails
    3. Both runner and SSH tool calls are recorded
    """

    @pytest.mark.asyncio
    async def test_worker_succeeds_despite_runner_failure(self, db_session, test_user):
        """Test that worker can succeed even when runner_exec fails.

        This simpler test verifies the core logic without full E2E complexity.
        """
        from zerg.crud import crud
        from unittest.mock import patch, MagicMock

        # Create agent with both tools
        agent = crud.create_agent(
            db=db_session,
            owner_id=test_user.id,
            name="Test Worker",
            model=TEST_WORKER_MODEL,
            system_instructions="You are a worker",
            task_instructions="",
        )
        agent.allowed_tools = ["runner_exec", "ssh_exec", "get_current_time"]
        db_session.commit()
        db_session.refresh(agent)

        # Mock runner_exec to fail
        def mock_runner_exec(target, command, **kwargs):
            return {
                "ok": False,
                "error_type": "validation_error",
                "user_message": "Runner 'laptop' not found",
            }

        # Mock ssh_exec to succeed
        def mock_ssh_exec(host, command, **kwargs):
            return {
                "ok": True,
                "data": {
                    "host": host,
                    "command": command,
                    "exit_code": 0,
                    "stdout": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       100G   45G   55G  45% /",
                    "stderr": "",
                    "duration_ms": 234,
                },
            }

        # Mock the AgentRunner to simulate tool calls
        async def mock_run_thread(db, thread):
            from zerg.crud import crud

            # Create system message
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="system",
                content="You are a worker",
                processed=True,
            )

            # Create user message
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="user",
                content="Check disk space on cube",
                processed=True,
            )

            # Simulate runner_exec failure (non-critical)
            runner_result = mock_runner_exec("laptop", "ssh cube df -h")
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="tool",
                content=str(runner_result),
                processed=True,
            )

            # Simulate ssh_exec success (fallback)
            ssh_result = mock_ssh_exec("drose@100.104.187.47:2222", "df -h")
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="tool",
                content=str(ssh_result),
                processed=True,
            )

            # Create assistant response
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="assistant",
                content="The cube server has 55GB available (45% used).",
                processed=True,
            )

            return crud.get_thread_messages(db, thread_id=thread.id)

        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.services.worker_runner import WorkerRunner
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkerArtifactStore(base_path=tmpdir)

            with patch("zerg.managers.agent_runner.AgentRunner.run_thread", side_effect=mock_run_thread):
                runner = WorkerRunner(artifact_store=store)
                result = await runner.run_worker(
                    db=db_session,
                    task="Check disk space on cube",
                    agent=agent,
                )

            # Worker should succeed
            assert result.status == "success"
            assert result.error is None

            # Result should mention disk space
            assert "55" in result.result or "45" in result.result


class TestCriticalErrorDetection:
    """Test the is_critical_tool_error logic for runner_exec."""

    def test_runner_exec_validation_error_is_not_critical(self):
        """Test that runner_exec validation errors are non-critical."""
        from zerg.agents_def.zerg_react_agent import is_critical_tool_error

        result = "{'ok': False, 'error_type': 'validation_error', 'user_message': \"Runner 'laptop' not found\"}"
        assert is_critical_tool_error(result, "Runner 'laptop' not found", tool_name="runner_exec") is False

    def test_runner_exec_any_error_is_not_critical(self):
        """Test that ALL runner_exec errors are non-critical (allows fallback)."""
        from zerg.agents_def.zerg_react_agent import is_critical_tool_error

        # Even configuration errors should be non-critical for runner_exec
        # because SSH fallback is always available
        result = "{'ok': False, 'error_type': 'connector_not_configured', 'user_message': 'No runners configured'}"
        assert is_critical_tool_error(result, "No runners configured", tool_name="runner_exec") is False

    def test_ssh_exec_key_error_is_critical(self):
        """Test that SSH key errors ARE critical (no fallback from SSH)."""
        from zerg.agents_def.zerg_react_agent import is_critical_tool_error

        result = (
            "{'ok': False, 'error_type': 'execution_error', 'user_message': 'SSH key not found at ~/.ssh/id_ed25519'}"
        )
        assert is_critical_tool_error(result, "SSH key not found at ~/.ssh/id_ed25519", tool_name="ssh_exec") is True


class TestPromptInstructions:
    """Test that worker prompts include fallback instructions."""

    def test_worker_prompt_includes_fallback_guidance(self):
        """Test that worker system prompt includes runner->SSH fallback instructions."""
        from zerg.prompts.templates import BASE_WORKER_PROMPT

        # Check for key phrases from the fallback instructions
        assert "runner_exec" in BASE_WORKER_PROMPT.lower()
        assert "fallback" in BASE_WORKER_PROMPT.lower()
        # Should mention trying runner first, then SSH
        assert "ssh_exec" in BASE_WORKER_PROMPT.lower()

    def test_server_metadata_includes_ssh_details(self):
        """Test that server metadata includes both SSH alias and concrete details."""
        from zerg.prompts.composer import format_servers

        servers = [
            {
                "name": "cube",
                "purpose": "Test server",
                "ip": "100.104.187.47",
                "ssh_user": "drose",
                "ssh_port": 2222,
                "ssh_alias": "cube",
            }
        ]

        formatted = format_servers(servers)

        # Should include both SSH alias and concrete connection details
        assert "cube" in formatted  # Server name
        assert "drose@100.104.187.47:2222" in formatted  # Concrete SSH details
        # May or may not include "SSH alias:" depending on implementation


class TestEndToEndFallbackFlow:
    """Integration test for complete fallback flow."""

    @pytest.mark.asyncio
    async def test_worker_with_fallback_simulation(self, db_session, test_user):
        """Test worker with simulated fallback scenario.

        This test verifies that when runner_exec fails, the worker doesn't
        immediately fail, allowing it to try alternative approaches.
        """
        from zerg.crud import crud

        agent = crud.create_agent(
            db=db_session,
            owner_id=test_user.id,
            name="Infrastructure Worker",
            model=TEST_WORKER_MODEL,
            system_instructions="You are a worker with SSH access.",
            task_instructions="",
        )
        agent.allowed_tools = ["runner_exec", "ssh_exec", "get_current_time"]
        db_session.commit()
        db_session.refresh(agent)

        # Mock AgentRunner to simulate the fallback flow
        async def mock_run_thread(db, thread):
            from zerg.crud import crud

            # Create system message
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="system",
                content="You are a worker with SSH access.",
                processed=True,
            )

            # Create user message
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="user",
                content="Check disk space on cube server",
                processed=True,
            )

            # Simulate runner_exec failure
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="tool",
                content="{'ok': False, 'error_type': 'validation_error', 'user_message': \"Runner 'laptop' not found\"}",
                processed=True,
            )

            # Simulate ssh_exec success (fallback)
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="tool",
                content="{'ok': True, 'data': {'exit_code': 0, 'stdout': 'Filesystem Size Used Avail Use% Mounted\\n/dev/sda1 100G 45G 55G 45% /', 'stderr': '', 'duration_ms': 187}}",
                processed=True,
            )

            # Create assistant response
            crud.create_thread_message(
                db=db,
                thread_id=thread.id,
                role="assistant",
                content="The cube server shows 45% disk usage with 55GB available out of 100GB total.",
                processed=True,
            )

            return crud.get_thread_messages(db, thread_id=thread.id)

        from zerg.services.worker_artifact_store import WorkerArtifactStore
        from zerg.services.worker_runner import WorkerRunner
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkerArtifactStore(base_path=tmpdir)

            with patch("zerg.managers.agent_runner.AgentRunner.run_thread", side_effect=mock_run_thread):
                runner = WorkerRunner(artifact_store=store)
                result = await runner.run_worker(
                    db=db_session,
                    task="Check disk space on cube server",
                    agent=agent,
                )

            # Verify successful completion
            assert result.status == "success"
            assert result.error is None

            # Verify final result contains disk information
            assert result.result is not None
            result_lower = result.result.lower()
            # Should mention disk space or the actual values
            assert any(
                term in result_lower for term in ["disk", "space", "45", "55", "filesystem", "available", "used"]
            ), f"Result should contain disk info: {result.result}"
