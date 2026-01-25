"""Tests for CloudExecutor service."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zerg.services.cloud_executor import (
    CloudExecutionResult,
    CloudExecutor,
    normalize_model_id,
)


class TestNormalizeModelId:
    """Tests for model ID normalization."""

    def test_already_prefixed(self):
        """Model with provider prefix passes through."""
        assert normalize_model_id("bedrock/claude-sonnet") == ("bedrock", "claude-sonnet")
        assert normalize_model_id("codex/gpt-5") == ("codex", "gpt-5")

    def test_maps_known_models(self):
        """Known models map to correct backend."""
        assert normalize_model_id("claude-sonnet") == ("bedrock", "claude-sonnet")
        assert normalize_model_id("gpt-5") == ("codex", "gpt-5")
        assert normalize_model_id("glm-4.7") == ("zai", "glm-4.7")

    def test_unknown_defaults_to_zai(self):
        """Unknown models default to zai backend."""
        assert normalize_model_id("unknown-model") == ("zai", "unknown-model")


class TestCloudExecutor:
    """Tests for CloudExecutor."""

    @pytest.fixture
    def executor(self):
        """Create executor instance."""
        return CloudExecutor()

    @pytest.mark.asyncio
    async def test_run_agent_missing_workspace(self, executor, tmp_path):
        """Returns error when workspace doesn't exist."""
        result = await executor.run_agent(
            task="test task",
            workspace_path=tmp_path / "nonexistent",
        )

        assert result.status == "failed"
        assert "does not exist" in result.error
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_run_agent_hatch_not_found(self, executor, tmp_path):
        """Returns error when hatch executable not found."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Use a non-existent path for hatch
        executor.hatch_path = "/nonexistent/hatch"

        result = await executor.run_agent(
            task="test task",
            workspace_path=workspace,
        )

        assert result.status == "failed"
        assert "hatch executable not found" in result.error
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_run_agent_success(self, executor, tmp_path):
        """Successful execution returns result."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Mock subprocess to return success
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"output text", b""))
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await executor.run_agent(
                task="test task",
                workspace_path=workspace,
            )

        assert result.status == "success"
        assert result.output == "output text"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_run_agent_failure(self, executor, tmp_path):
        """Failed execution returns error status."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Mock subprocess to return failure
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error message"))
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await executor.run_agent(
                task="test task",
                workspace_path=workspace,
            )

        assert result.status == "failed"
        assert "error message" in result.error
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_run_agent_timeout(self, executor, tmp_path):
        """Timeout kills process group and returns timeout status."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Mock subprocess that times out
        mock_process = MagicMock()
        mock_process.pid = 12345

        async def slow_communicate():
            await asyncio.sleep(10)  # Will be cancelled by timeout
            return (b"", b"")

        mock_process.communicate = slow_communicate
        mock_process.wait = AsyncMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch("os.killpg") as mock_killpg,
        ):
            result = await executor.run_agent(
                task="test task",
                workspace_path=workspace,
                timeout=0.1,  # Very short timeout
            )

        assert result.status == "timeout"
        assert "timed out" in result.error
        # Verify process group was killed
        mock_killpg.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_hatch_available_success(self, executor):
        """check_hatch_available returns True when hatch works."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"hatch usage", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            available, message = await executor.check_hatch_available()

        assert available is True
        assert "available" in message

    @pytest.mark.asyncio
    async def test_check_hatch_available_not_found(self, executor):
        """check_hatch_available returns False when hatch not found."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            available, message = await executor.check_hatch_available()

        assert available is False
        assert "not found" in message

    def test_builds_correct_command(self, executor, tmp_path):
        """Verify hatch command is built correctly."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # We can't easily test the exact command without running it,
        # but we can verify the executor has correct attributes
        assert executor.hatch_path == "hatch"
        assert executor.default_model == "zai/glm-4.7"

    @pytest.mark.asyncio
    async def test_run_agent_includes_resume_flag(self, executor, tmp_path):
        """Verify --resume flag is included when resume_session_id is provided."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.pid = 12345

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await executor.run_agent(
                task="test task",
                workspace_path=workspace,
                resume_session_id="abc123-session-id",
            )

        # Verify --resume flag was included with correct session ID
        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert "--resume" in cmd_list
        resume_idx = cmd_list.index("--resume")
        assert cmd_list[resume_idx + 1] == "abc123-session-id"

    @pytest.mark.asyncio
    async def test_run_agent_no_resume_flag_without_session_id(self, executor, tmp_path):
        """Verify --resume flag is NOT included when resume_session_id is None."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"output", b""))
        mock_process.pid = 12345

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await executor.run_agent(
                task="test task",
                workspace_path=workspace,
                # No resume_session_id
            )

        # Verify --resume flag was NOT included
        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert "--resume" not in cmd_list
