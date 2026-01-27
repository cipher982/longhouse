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
        assert config.cmd[1] == "exec"
        assert "-" in config.cmd  # Read from stdin
        assert "--full-auto" in config.cmd
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
            mock_subprocess.return_value = ("Fiche output here", "", 0, False)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is True
            assert result.output == "Fiche output here"
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


class TestBackendConfigBuildEnv:
    """Tests for BackendConfig.build_env() method."""

    def test_build_env_merges_with_current(self) -> None:
        """Test that build_env merges backend env with current environment."""
        from zerg.libs.agent_runner.backends import BackendConfig

        config = BackendConfig(
            cmd=["test"],
            env={"NEW_VAR": "new_value"},
        )

        with patch.dict(os.environ, {"EXISTING_VAR": "existing"}, clear=False):
            result = config.build_env()
            assert result["EXISTING_VAR"] == "existing"
            assert result["NEW_VAR"] == "new_value"

    def test_build_env_unsets_vars(self) -> None:
        """Test that build_env removes vars in env_unset."""
        from zerg.libs.agent_runner.backends import BackendConfig

        config = BackendConfig(
            cmd=["test"],
            env={"KEEP_VAR": "keep"},
            env_unset=["REMOVE_VAR"],
        )

        with patch.dict(os.environ, {"REMOVE_VAR": "to_remove", "KEEP_VAR": "old"}, clear=False):
            result = config.build_env()
            assert "REMOVE_VAR" not in result
            assert result["KEEP_VAR"] == "keep"  # Backend env overrides existing

    def test_build_env_unset_missing_key_ok(self) -> None:
        """Test that unsetting a non-existent key doesn't error."""
        from zerg.libs.agent_runner.backends import BackendConfig

        config = BackendConfig(
            cmd=["test"],
            env={},
            env_unset=["NONEXISTENT_VAR"],
        )

        # Should not raise
        result = config.build_env()
        assert "NONEXISTENT_VAR" not in result


class TestCodexOptions:
    """Tests for Codex backend optional parameters."""

    @pytest.fixture
    def laptop_ctx(self) -> ExecutionContext:
        return ExecutionContext(in_container=False, home_writable=True)

    def test_codex_full_auto_default(self, laptop_ctx: ExecutionContext) -> None:
        """Test Codex full_auto=True (default) adds flag."""
        config = configure_codex(
            prompt="test",
            ctx=laptop_ctx,
            api_key="sk-test",
        )

        assert "--full-auto" in config.cmd

    def test_codex_full_auto_disabled(self, laptop_ctx: ExecutionContext) -> None:
        """Test Codex full_auto=False doesn't add flag."""
        config = configure_codex(
            prompt="test",
            ctx=laptop_ctx,
            api_key="sk-test",
            full_auto=False,
        )

        assert "--full-auto" not in config.cmd

    def test_codex_model_override(self, laptop_ctx: ExecutionContext) -> None:
        """Test Codex model parameter adds -m flag."""
        config = configure_codex(
            prompt="test",
            ctx=laptop_ctx,
            api_key="sk-test",
            model="o3",
        )

        assert "-m" in config.cmd
        model_idx = config.cmd.index("-m")
        assert config.cmd[model_idx + 1] == "o3"

    def test_codex_no_model_override(self, laptop_ctx: ExecutionContext) -> None:
        """Test Codex without model parameter doesn't add -m flag."""
        config = configure_codex(
            prompt="test",
            ctx=laptop_ctx,
            api_key="sk-test",
        )

        assert "-m" not in config.cmd


class TestContainerDetection:
    """Tests for container detection logic."""

    def test_detect_docker(self) -> None:
        """Test Docker container detection via /.dockerenv."""
        from zerg.libs.agent_runner.context import _detect_container

        def mock_exists(path: str) -> bool:
            return path == "/.dockerenv"

        with patch("os.path.exists", mock_exists):
            with patch("builtins.open", side_effect=FileNotFoundError):
                assert _detect_container() is True

    def test_detect_podman(self) -> None:
        """Test Podman container detection via /run/.containerenv."""
        from zerg.libs.agent_runner.context import _detect_container

        def mock_exists(path: str) -> bool:
            return path == "/run/.containerenv"

        with patch("os.path.exists", mock_exists):
            with patch("builtins.open", side_effect=FileNotFoundError):
                assert _detect_container() is True

    def test_detect_cgroup_docker(self) -> None:
        """Test container detection via cgroup containing 'docker'."""
        from zerg.libs.agent_runner.context import _detect_container
        from io import StringIO

        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", return_value=StringIO("12:blkio:/docker/abc123\n")),
        ):
            assert _detect_container() is True

    def test_detect_cgroup_kubernetes(self) -> None:
        """Test container detection via cgroup containing 'kubepods'."""
        from zerg.libs.agent_runner.context import _detect_container
        from io import StringIO

        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", return_value=StringIO("1:name=systemd:/kubepods/pod123\n")),
        ):
            assert _detect_container() is True

    def test_detect_no_container(self) -> None:
        """Test non-container detection."""
        from zerg.libs.agent_runner.context import _detect_container

        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", side_effect=FileNotFoundError),
        ):
            assert _detect_container() is False


class TestHomeWritableCheck:
    """Tests for home writable check."""

    def test_home_writable_permission_error(self) -> None:
        """Test that PermissionError returns False."""
        from zerg.libs.agent_runner.context import _check_home_writable

        with (
            patch.dict(os.environ, {"HOME": "/root"}),
            patch("pathlib.Path.write_text", side_effect=PermissionError),
        ):
            assert _check_home_writable() is False

    def test_home_writable_os_error(self) -> None:
        """Test that OSError returns False."""
        from zerg.libs.agent_runner.context import _check_home_writable

        with (
            patch.dict(os.environ, {"HOME": "/nonexistent"}),
            patch("pathlib.Path.write_text", side_effect=OSError("Read-only filesystem")),
        ):
            assert _check_home_writable() is False


class TestRunnerEdgeCases:
    """Edge case tests for run() function."""

    @pytest.mark.asyncio
    async def test_generic_exception(self) -> None:
        """Test that generic exceptions are caught and returned as errors."""
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
                cmd=["test"],
                stdin_data=None,
                build_env=MagicMock(return_value={}),
            )
            mock_subprocess.side_effect = RuntimeError("Unexpected error")

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is False
            assert result.exit_code == -3
            assert "Unexpected error" in (result.error or "")

    @pytest.mark.asyncio
    async def test_whitespace_only_output_is_empty(self) -> None:
        """Test that whitespace-only output is treated as empty."""
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
            mock_subprocess.return_value = ("   \n\t  \n", "", 0, False)

            result = await run(
                prompt="test",
                backend=Backend.ZAI,
                api_key="test-key",
            )

            assert result.ok is False
            assert result.error == "Empty output from agent"


class TestGetConfig:
    """Tests for get_config dispatcher."""

    @pytest.fixture
    def laptop_ctx(self) -> ExecutionContext:
        return ExecutionContext(in_container=False, home_writable=True)

    def test_get_config_zai(self, laptop_ctx: ExecutionContext) -> None:
        """Test get_config routes to configure_zai."""
        from zerg.libs.agent_runner.backends import get_config

        config = get_config(Backend.ZAI, "test", laptop_ctx, api_key="key")
        assert config.cmd[0] == "claude"
        assert "ANTHROPIC_AUTH_TOKEN" in config.env

    def test_get_config_bedrock(self, laptop_ctx: ExecutionContext) -> None:
        """Test get_config routes to configure_bedrock."""
        from zerg.libs.agent_runner.backends import get_config

        config = get_config(Backend.BEDROCK, "test", laptop_ctx)
        assert config.cmd[0] == "claude"
        assert config.env.get("CLAUDE_CODE_USE_BEDROCK") == "1"

    def test_get_config_codex(self, laptop_ctx: ExecutionContext) -> None:
        """Test get_config routes to configure_codex."""
        from zerg.libs.agent_runner.backends import get_config

        config = get_config(Backend.CODEX, "test", laptop_ctx, api_key="sk-test")
        assert config.cmd[0] == "codex"

    def test_get_config_gemini(self, laptop_ctx: ExecutionContext) -> None:
        """Test get_config routes to configure_gemini."""
        from zerg.libs.agent_runner.backends import get_config

        config = get_config(Backend.GEMINI, "test", laptop_ctx)
        assert config.cmd[0] == "gemini"
