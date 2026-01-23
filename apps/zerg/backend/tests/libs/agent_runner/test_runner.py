"""Tests for agent_runner library."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.libs.agent_runner import Backend
from zerg.libs.agent_runner import run
from zerg.libs.agent_runner.backends import configure_bedrock
from zerg.libs.agent_runner.backends import configure_codex
from zerg.libs.agent_runner.backends import configure_gemini
from zerg.libs.agent_runner.backends import configure_zai
from zerg.libs.agent_runner.context import ExecutionContext
from zerg.libs.agent_runner.context import detect_context


class TestContextDetection:
    """Tests for execution context detection."""

    def test_laptop_context(self) -> None:
        """Test context detection on laptop (no container markers)."""
        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", side_effect=FileNotFoundError),
            patch.object(ExecutionContext, "__init__", lambda self, **kw: None),
        ):
            # Just verify the function runs without error
            ctx = detect_context()
            # Cache is used, so clear it for fresh test
            detect_context.cache_clear()

    def test_effective_home_in_container_readonly(self) -> None:
        """Test that effective_home returns /tmp when home not writable."""
        ctx = ExecutionContext(in_container=True, home_writable=False)
        assert ctx.effective_home == "/tmp"

    def test_effective_home_on_laptop(self) -> None:
        """Test that effective_home returns actual HOME on laptop."""
        ctx = ExecutionContext(in_container=False, home_writable=True)
        with patch.dict(os.environ, {"HOME": "/Users/test"}):
            assert ctx.effective_home == "/Users/test"


class TestBackendConfigs:
    """Tests for backend configuration."""

    @pytest.fixture
    def laptop_ctx(self) -> ExecutionContext:
        return ExecutionContext(in_container=False, home_writable=True)

    @pytest.fixture
    def container_ctx(self) -> ExecutionContext:
        return ExecutionContext(in_container=True, home_writable=False)

    def test_zai_config_laptop(self, laptop_ctx: ExecutionContext) -> None:
        """Test z.ai config on laptop."""
        config = configure_zai(
            prompt="test prompt",
            ctx=laptop_ctx,
            api_key="test-key",
        )

        assert config.cmd[0] == "claude"
        assert "--print" in config.cmd
        assert "-" in config.cmd  # Reads from stdin
        assert "--dangerously-skip-permissions" in config.cmd
        assert config.env["ANTHROPIC_AUTH_TOKEN"] == "test-key"
        assert config.env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
        assert "CLAUDE_CODE_USE_BEDROCK" in config.env_unset
        # Prompt should be in stdin_data, not cmd
        assert config.stdin_data == b"test prompt"

    def test_zai_config_container(self, container_ctx: ExecutionContext) -> None:
        """Test z.ai config in container sets HOME=/tmp."""
        config = configure_zai(
            prompt="test",
            ctx=container_ctx,
            api_key="test-key",
        )

        assert config.env.get("HOME") == "/tmp"

    def test_zai_requires_api_key(self, laptop_ctx: ExecutionContext) -> None:
        """Test z.ai raises when no API key available."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="ZAI_API_KEY"):
                configure_zai(prompt="test", ctx=laptop_ctx)

    def test_bedrock_config(self, laptop_ctx: ExecutionContext) -> None:
        """Test Bedrock config."""
        config = configure_bedrock(
            prompt="test prompt",
            ctx=laptop_ctx,
            model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        )

        assert config.cmd[0] == "claude"
        assert config.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert config.env["AWS_PROFILE"] == "zh-qa-engineer"
        assert config.env["AWS_REGION"] == "us-east-1"
        assert config.stdin_data == b"test prompt"

    def test_bedrock_config_container(self, container_ctx: ExecutionContext) -> None:
        """Test Bedrock config in container sets HOME=/tmp."""
        config = configure_bedrock(
            prompt="test",
            ctx=container_ctx,
        )

        assert config.env.get("HOME") == "/tmp"

    def test_codex_config(self, laptop_ctx: ExecutionContext) -> None:
        """Test Codex config."""
        config = configure_codex(
            prompt="test prompt",
            ctx=laptop_ctx,
            api_key="sk-test",
        )

        assert config.cmd[0] == "codex"
        assert "--approval-mode" in config.cmd
        assert "full-auto" in config.cmd
        assert config.env["OPENAI_API_KEY"] == "sk-test"
        assert config.stdin_data == b"test prompt"

    def test_codex_config_container(self, container_ctx: ExecutionContext) -> None:
        """Test Codex config in container sets HOME."""
        config = configure_codex(
            prompt="test",
            ctx=container_ctx,
            api_key="sk-test",
        )

        assert config.env.get("HOME") == "/tmp"

    def test_gemini_config(self, laptop_ctx: ExecutionContext) -> None:
        """Test Gemini config."""
        config = configure_gemini(
            prompt="test prompt",
            ctx=laptop_ctx,
        )

        assert config.cmd[0] == "gemini"
        assert "-p" in config.cmd
        assert "-" in config.cmd  # Reads from stdin
        # Gemini uses OAuth, no API key in env
        assert "OPENAI_API_KEY" not in config.env
        assert "ANTHROPIC_API_KEY" not in config.env
        assert config.stdin_data == b"test prompt"

    def test_gemini_config_container(self, container_ctx: ExecutionContext) -> None:
        """Test Gemini config in container sets HOME."""
        config = configure_gemini(
            prompt="test",
            ctx=container_ctx,
        )

        assert config.env.get("HOME") == "/tmp"


class TestRunner:
    """Tests for the main run() function."""

    @pytest.mark.asyncio
    async def test_successful_run(self) -> None:
        """Test successful agent run."""
        with (
            patch("zerg.libs.agent_runner.runner.detect_context") as mock_ctx,
            patch("zerg.libs.agent_runner.runner.get_config") as mock_config,
            patch(
                "zerg.libs.agent_runner.runner._run_subprocess"
            ) as mock_subprocess,
        ):
            mock_ctx.return_value = ExecutionContext(
                in_container=False, home_writable=True
            )
            mock_config.return_value = MagicMock(
                cmd=["echo", "test"],
                stdin_data=b"test prompt",
                build_env=MagicMock(return_value={}),
            )
            # _run_subprocess returns (stdout, stderr, return_code, timed_out)
            mock_subprocess.return_value = ("Agent output here", "", 0, False)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is True
            assert result.output == "Agent output here"
            assert result.exit_code == 0
            assert result.status == "ok"

    @pytest.mark.asyncio
    async def test_failed_run(self) -> None:
        """Test failed agent run."""
        with (
            patch("zerg.libs.agent_runner.runner.detect_context") as mock_ctx,
            patch("zerg.libs.agent_runner.runner.get_config") as mock_config,
            patch(
                "zerg.libs.agent_runner.runner._run_subprocess"
            ) as mock_subprocess,
        ):
            mock_ctx.return_value = ExecutionContext(
                in_container=False, home_writable=True
            )
            mock_config.return_value = MagicMock(
                cmd=["false"],
                stdin_data=None,
                build_env=MagicMock(return_value={}),
            )
            mock_subprocess.return_value = ("", "Error occurred", 1, False)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is False
            assert result.exit_code == 1
            assert result.status == "error"
            assert "Error occurred" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """Test agent timeout properly kills subprocess."""
        with (
            patch("zerg.libs.agent_runner.runner.detect_context") as mock_ctx,
            patch("zerg.libs.agent_runner.runner.get_config") as mock_config,
            patch(
                "zerg.libs.agent_runner.runner._run_subprocess"
            ) as mock_subprocess,
        ):
            mock_ctx.return_value = ExecutionContext(
                in_container=False, home_writable=True
            )
            mock_config.return_value = MagicMock(
                cmd=["sleep", "100"],
                stdin_data=None,
                build_env=MagicMock(return_value={}),
            )
            # _run_subprocess returns timed_out=True when timeout occurs
            mock_subprocess.return_value = ("", "", -1, True)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
                timeout_s=1,
            )

            assert result.ok is False
            assert result.exit_code == -1
            assert result.status == "timeout"

    @pytest.mark.asyncio
    async def test_cli_not_found(self) -> None:
        """Test CLI not found error."""
        with (
            patch("zerg.libs.agent_runner.runner.detect_context") as mock_ctx,
            patch("zerg.libs.agent_runner.runner.get_config") as mock_config,
            patch(
                "zerg.libs.agent_runner.runner._run_subprocess"
            ) as mock_subprocess,
        ):
            mock_ctx.return_value = ExecutionContext(
                in_container=False, home_writable=True
            )
            mock_config.return_value = MagicMock(
                cmd=["nonexistent-cli"],
                stdin_data=None,
                build_env=MagicMock(return_value={}),
            )
            mock_subprocess.side_effect = FileNotFoundError("nonexistent-cli")

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is False
            assert result.exit_code == -2
            assert result.status == "not_found"

    @pytest.mark.asyncio
    async def test_empty_output(self) -> None:
        """Test empty output is treated as error."""
        with (
            patch("zerg.libs.agent_runner.runner.detect_context") as mock_ctx,
            patch("zerg.libs.agent_runner.runner.get_config") as mock_config,
            patch(
                "zerg.libs.agent_runner.runner._run_subprocess"
            ) as mock_subprocess,
        ):
            mock_ctx.return_value = ExecutionContext(
                in_container=False, home_writable=True
            )
            mock_config.return_value = MagicMock(
                cmd=["echo"],
                stdin_data=None,
                build_env=MagicMock(return_value={}),
            )
            mock_subprocess.return_value = ("", "", 0, False)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is False
            assert result.exit_code == 0
            assert result.error == "Empty output from agent"
