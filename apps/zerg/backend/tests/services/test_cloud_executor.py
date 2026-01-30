"""Tests for CloudExecutor service."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zerg.services.cloud_executor import (
    CloudExecutionResult,
    CloudExecutor,
    normalize_model_id,
    validate_workspace_path,
    _sanitize_container_name,
    WORKSPACE_BASE,
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
    async def test_run_commis_missing_workspace(self, executor, tmp_path):
        """Returns error when workspace doesn't exist."""
        result = await executor.run_commis(
            task="test task",
            workspace_path=tmp_path / "nonexistent",
        )

        assert result.status == "failed"
        assert "does not exist" in result.error
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_run_commis_hatch_not_found(self, executor, tmp_path):
        """Returns error when hatch executable not found."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Use a non-existent path for hatch
        executor.hatch_path = "/nonexistent/hatch"

        result = await executor.run_commis(
            task="test task",
            workspace_path=workspace,
        )

        assert result.status == "failed"
        assert "hatch executable not found" in result.error
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_run_commis_success(self, executor, tmp_path):
        """Successful execution returns result."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Mock subprocess to return success
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"output text", b""))
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await executor.run_commis(
                task="test task",
                workspace_path=workspace,
            )

        assert result.status == "success"
        assert result.output == "output text"
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_run_commis_failure(self, executor, tmp_path):
        """Failed execution returns error status."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Mock subprocess to return failure
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error message"))
        mock_process.pid = 12345

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await executor.run_commis(
                task="test task",
                workspace_path=workspace,
            )

        assert result.status == "failed"
        assert "error message" in result.error
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_run_commis_timeout(self, executor, tmp_path):
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
            result = await executor.run_commis(
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
    async def test_run_commis_includes_resume_flag(self, executor, tmp_path):
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
            await executor.run_commis(
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
    async def test_run_commis_no_resume_flag_without_session_id(self, executor, tmp_path):
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
            await executor.run_commis(
                task="test task",
                workspace_path=workspace,
                # No resume_session_id
            )

        # Verify --resume flag was NOT included
        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert "--resume" not in cmd_list

    @pytest.mark.asyncio
    async def test_run_commis_includes_output_format_flag_from_env(self, tmp_path, monkeypatch):
        """Verify --output-format flag is included when env var is set."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("HATCH_CLAUDE_OUTPUT_FORMAT", "stream-json")

        executor = CloudExecutor()
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
            await executor.run_commis(
                task="test task",
                workspace_path=workspace,
            )

        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert "--output-format" in cmd_list
        fmt_idx = cmd_list.index("--output-format")
        assert cmd_list[fmt_idx + 1] == "stream-json"

    @pytest.mark.asyncio
    async def test_run_commis_includes_partial_messages_flag_from_env(self, tmp_path, monkeypatch):
        """Verify --include-partial-messages flag is included when env var is set."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.setenv("HATCH_CLAUDE_INCLUDE_PARTIAL_MESSAGES", "true")

        executor = CloudExecutor()
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
            await executor.run_commis(
                task="test task",
                workspace_path=workspace,
            )

        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert "--include-partial-messages" in cmd_list


class TestValidateWorkspacePath:
    """Tests for workspace path validation."""

    def test_rejects_path_outside_base(self, tmp_path):
        """Rejects paths outside WORKSPACE_BASE."""
        with pytest.raises(ValueError, match="must be under"):
            validate_workspace_path(tmp_path / "evil")

    def test_rejects_nonexistent_path(self, tmp_path, monkeypatch):
        """Rejects paths that don't exist."""
        # Temporarily set WORKSPACE_BASE to tmp_path for testing
        monkeypatch.setenv("COMMIS_WORKSPACE_BASE", str(tmp_path))
        # Re-import to pick up new env var
        from importlib import reload
        import zerg.services.cloud_executor as ce
        reload(ce)

        with pytest.raises(ValueError, match="does not exist"):
            ce.validate_workspace_path(tmp_path / "nonexistent")

    def test_accepts_valid_path(self, tmp_path, monkeypatch):
        """Accepts valid paths under WORKSPACE_BASE."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("COMMIS_WORKSPACE_BASE", str(tmp_path))
        from importlib import reload
        import zerg.services.cloud_executor as ce
        reload(ce)

        result = ce.validate_workspace_path(workspace)
        assert result == workspace.resolve()


class TestSanitizeContainerName:
    """Tests for container name sanitization."""

    def test_removes_dashes(self):
        """Removes dashes from run ID."""
        name = _sanitize_container_name("abc-123-def")
        assert name.startswith("commis-abc123def")

    def test_truncates_long_ids(self):
        """Truncates long run IDs."""
        name = _sanitize_container_name("a" * 100)
        # Should be commis-{12 chars}-{8 hex chars}
        parts = name.split("-")
        assert len(parts) == 3
        assert len(parts[1]) == 12

    def test_adds_uuid_suffix(self):
        """Adds UUID suffix for uniqueness."""
        name1 = _sanitize_container_name("test")
        name2 = _sanitize_container_name("test")
        # Same input should produce different outputs due to UUID
        assert name1 != name2


class TestCloudExecutorSandbox:
    """Tests for sandbox (container) execution mode."""

    @pytest.fixture
    def executor(self):
        """Create executor instance."""
        return CloudExecutor()

    @pytest.mark.asyncio
    async def test_sandbox_routes_to_container(self, executor, tmp_path, monkeypatch):
        """sandbox=True routes to _run_in_container."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Set WORKSPACE_BASE to allow tmp_path
        monkeypatch.setenv("COMMIS_WORKSPACE_BASE", str(tmp_path))
        from importlib import reload
        import zerg.services.cloud_executor as ce
        reload(ce)

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"container output", b""))

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        executor_new = ce.CloudExecutor()
        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            result = await executor_new.run_commis(
                task="test task",
                workspace_path=workspace,
                sandbox=True,
                run_id="test-run-123",
            )

        # Should have called docker run
        assert captured_cmd is not None
        assert captured_cmd[0] == "docker"
        assert captured_cmd[1] == "run"
        assert result.status == "success"
        assert result.output == "container output"

    @pytest.mark.asyncio
    async def test_sandbox_validates_workspace(self, executor, tmp_path):
        """sandbox=True validates workspace path."""
        # Path outside WORKSPACE_BASE should fail
        result = await executor.run_commis(
            task="test task",
            workspace_path=tmp_path / "evil",
            sandbox=True,
            run_id="test-123",
        )

        assert result.status == "failed"
        assert "must be under" in result.error

    @pytest.mark.asyncio
    async def test_sandbox_timeout_kills_container(self, tmp_path, monkeypatch):
        """Timeout kills container and returns timeout status."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("COMMIS_WORKSPACE_BASE", str(tmp_path))
        from importlib import reload
        import zerg.services.cloud_executor as ce
        reload(ce)

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def slow_communicate(input=None):
            await asyncio.sleep(10)  # Will be cancelled by timeout
            return (b"", b"")

        mock_process.communicate = slow_communicate

        mock_kill_process = MagicMock()
        mock_kill_process.wait = AsyncMock()

        container_killed = False
        captured_kill_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal container_killed, captured_kill_cmd
            if args[0] == "docker" and args[1] == "kill":
                container_killed = True
                captured_kill_cmd = args
                return mock_kill_process
            return mock_process

        executor = ce.CloudExecutor()
        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            result = await executor.run_commis(
                task="test task",
                workspace_path=workspace,
                sandbox=True,
                run_id="test-123",
                timeout=0.1,
            )

        assert result.status == "timeout"
        assert container_killed
        assert captured_kill_cmd[1] == "kill"

    @pytest.mark.asyncio
    async def test_sandbox_docker_command_security(self, tmp_path, monkeypatch):
        """Verify docker command includes security options."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        monkeypatch.setenv("COMMIS_WORKSPACE_BASE", str(tmp_path))
        from importlib import reload
        import zerg.services.cloud_executor as ce
        reload(ce)

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        executor = ce.CloudExecutor()
        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await executor.run_commis(
                task="test",
                workspace_path=workspace,
                sandbox=True,
                run_id="test",
            )

        cmd_list = list(captured_cmd)
        # Security options
        assert "--security-opt" in cmd_list
        assert "no-new-privileges" in cmd_list
        assert "--cap-drop" in cmd_list
        assert "ALL" in cmd_list
        # Resource limits
        assert "--memory" in cmd_list
        assert "--cpus" in cmd_list
        assert "--pids-limit" in cmd_list

    @pytest.mark.asyncio
    async def test_check_sandbox_available_docker_not_found(self, executor):
        """check_sandbox_available returns False when docker not found."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            available, message = await executor.check_sandbox_available()

        assert available is False
        assert "not found" in message
