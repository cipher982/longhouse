"""Tests for the runner module."""

from __future__ import annotations

import asyncio
import subprocess
from unittest import mock

import pytest

from hatch.backends import Backend
from hatch.runner import AgentResult, _run_subprocess, run, run_sync


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_ok_result_status(self):
        """OK result has 'ok' status."""
        result = AgentResult(
            ok=True, output="output", exit_code=0, duration_ms=100
        )
        assert result.status == "ok"

    def test_timeout_status(self):
        """Timeout has 'timeout' status."""
        result = AgentResult(
            ok=False, output="", exit_code=-1, duration_ms=5000
        )
        assert result.status == "timeout"

    def test_not_found_status(self):
        """Not found has 'not_found' status."""
        result = AgentResult(
            ok=False, output="", exit_code=-2, duration_ms=10
        )
        assert result.status == "not_found"

    def test_error_status(self):
        """Other errors have 'error' status."""
        result = AgentResult(
            ok=False, output="", exit_code=1, duration_ms=100
        )
        assert result.status == "error"

    def test_to_dict(self):
        """to_dict includes all fields."""
        result = AgentResult(
            ok=True,
            output="test output",
            exit_code=0,
            duration_ms=150,
            error=None,
            stderr="some stderr",
        )
        d = result.to_dict()
        assert d["ok"] is True
        assert d["status"] == "ok"
        assert d["output"] == "test output"
        assert d["exit_code"] == 0
        assert d["duration_ms"] == 150
        assert d["error"] is None
        assert d["stderr"] == "some stderr"

    def test_to_dict_with_error(self):
        """to_dict includes error field."""
        result = AgentResult(
            ok=False,
            output="",
            exit_code=1,
            duration_ms=100,
            error="Something failed",
        )
        d = result.to_dict()
        assert d["ok"] is False
        assert d["error"] == "Something failed"


class TestRunSubprocess:
    """Tests for _run_subprocess function."""

    def test_successful_execution(self):
        """Successful command returns output."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["echo", "hello"],
            stdin_data=None,
            env=env,
            cwd=None,
            timeout_s=10,
        )
        assert stdout.strip() == "hello"
        assert code == 0
        assert timed_out is False

    def test_stdin_data(self):
        """Stdin data is passed to command."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["cat"],
            stdin_data=b"test input",
            env=env,
            cwd=None,
            timeout_s=10,
        )
        assert stdout == "test input"
        assert code == 0

    def test_nonzero_exit_code(self):
        """Non-zero exit code is captured."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["false"],
            stdin_data=None,
            env=env,
            cwd=None,
            timeout_s=10,
        )
        assert code != 0
        assert timed_out is False

    def test_stderr_captured(self):
        """Stderr is captured separately."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["sh", "-c", "echo error >&2"],
            stdin_data=None,
            env=env,
            cwd=None,
            timeout_s=10,
        )
        assert "error" in stderr
        assert code == 0

    def test_timeout_kills_process(self):
        """Timeout kills the process and returns timed_out=True."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["sleep", "10"],
            stdin_data=None,
            env=env,
            cwd=None,
            timeout_s=1,
        )
        assert timed_out is True
        assert code == -1

    def test_working_directory(self, tmp_path):
        """Working directory is respected."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = _run_subprocess(
            cmd=["pwd"],
            stdin_data=None,
            env=env,
            cwd=str(tmp_path),
            timeout_s=10,
        )
        assert stdout.strip() == str(tmp_path)


class TestRunSync:
    """Tests for run_sync function."""

    def test_basic_execution(self):
        """Basic command execution works."""
        import os
        env = dict(os.environ)
        stdout, stderr, code, timed_out = run_sync(
            cmd=["echo", "test"],
            stdin_data=None,
            env=env,
            cwd=None,
            timeout_s=10,
        )
        assert "test" in stdout
        assert code == 0


class TestAsyncRun:
    """Tests for async run function."""

    @pytest.fixture
    def mock_subprocess(self):
        """Mock _run_subprocess for testing."""
        with mock.patch("hatch.runner._run_subprocess") as m:
            m.return_value = ("output", "", 0, False)
            yield m

    @pytest.fixture
    def mock_context(self):
        """Mock detect_context."""
        from hatch.context import ExecutionContext

        ctx = ExecutionContext(in_container=False, home_writable=True)
        with mock.patch("hatch.runner.detect_context", return_value=ctx):
            yield ctx

    @pytest.fixture
    def mock_get_config(self):
        """Mock get_config."""
        from hatch.backends import BackendConfig

        config = BackendConfig(
            cmd=["echo", "test"],
            env={"TEST": "1"},
            stdin_data=b"prompt",
        )
        with mock.patch("hatch.runner.get_config", return_value=config):
            yield config

    async def test_success_result(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key
    ):
        """Successful run returns ok result."""
        mock_subprocess.return_value = ("output text", "", 0, False)

        result = await run("test prompt", Backend.ZAI)

        assert result.ok is True
        assert result.output == "output text"
        assert result.exit_code == 0
        assert result.status == "ok"

    async def test_timeout_result(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key
    ):
        """Timeout returns appropriate result."""
        mock_subprocess.return_value = ("", "", -1, True)

        result = await run("test prompt", Backend.ZAI, timeout_s=5)

        assert result.ok is False
        assert result.exit_code == -1
        assert result.status == "timeout"
        assert "timed out" in result.error.lower()

    async def test_invalid_timeout_returns_error(self):
        """Invalid timeout returns error result (no exception)."""
        result = await run("test prompt", Backend.ZAI, timeout_s=0)

        assert result.ok is False
        assert result.exit_code == -3
        assert "timeout_s must be > 0" in result.error

    async def test_invalid_cwd_returns_error(self, tmp_path):
        """Invalid cwd returns error result (no exception)."""
        bad_path = tmp_path / "missing"

        result = await run("test prompt", Backend.ZAI, cwd=bad_path)

        assert result.ok is False
        assert result.exit_code == -3
        assert "cwd does not exist" in result.error

    async def test_error_result(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key
    ):
        """Non-zero exit returns error result."""
        mock_subprocess.return_value = ("", "error message", 1, False)

        result = await run("test prompt", Backend.ZAI)

        assert result.ok is False
        assert result.exit_code == 1
        assert result.status == "error"
        assert result.stderr == "error message"

    async def test_empty_output_is_error(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key
    ):
        """Empty output is treated as error."""
        mock_subprocess.return_value = ("   ", "", 0, False)

        result = await run("test prompt", Backend.ZAI)

        assert result.ok is False
        assert "empty output" in result.error.lower()

    async def test_cli_not_found(self, mock_context, mock_get_config, mock_zai_key):
        """FileNotFoundError returns not_found result."""
        with mock.patch(
            "hatch.runner._run_subprocess",
            side_effect=FileNotFoundError("claude not found"),
        ):
            result = await run("test prompt", Backend.ZAI)

        assert result.ok is False
        assert result.exit_code == -2
        assert result.status == "not_found"
        assert "not found" in result.error.lower()

    async def test_unexpected_exception(
        self, mock_context, mock_get_config, mock_zai_key
    ):
        """Unexpected exceptions are caught."""
        with mock.patch(
            "hatch.runner._run_subprocess",
            side_effect=RuntimeError("something bad"),
        ):
            result = await run("test prompt", Backend.ZAI)

        assert result.ok is False
        assert result.exit_code == -3
        assert "something bad" in result.error

    async def test_duration_tracked(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key
    ):
        """Duration is tracked in result."""
        mock_subprocess.return_value = ("output", "", 0, False)

        result = await run("test prompt", Backend.ZAI)

        assert result.duration_ms >= 0
        # Should be fast since it's mocked
        assert result.duration_ms < 1000

    async def test_cwd_passed_to_subprocess(
        self, mock_subprocess, mock_context, mock_get_config, mock_zai_key, tmp_path
    ):
        """cwd is passed to subprocess."""
        mock_subprocess.return_value = ("output", "", 0, False)

        await run("test prompt", Backend.ZAI, cwd=tmp_path)

        # Check that cwd was passed
        call_args = mock_subprocess.call_args
        assert call_args[0][3] == str(tmp_path)  # cwd is 4th positional arg

    async def test_backend_kwargs_passed(
        self, mock_subprocess, mock_context, mock_zai_key
    ):
        """Backend kwargs are passed to get_config."""
        mock_subprocess.return_value = ("output", "", 0, False)

        with mock.patch("hatch.runner.get_config") as mock_config:
            from hatch.backends import BackendConfig

            mock_config.return_value = BackendConfig(
                cmd=["test"], env={}, stdin_data=b"test"
            )

            await run("test", Backend.ZAI, model="custom-model")

            mock_config.assert_called_once()
            kwargs = mock_config.call_args[1]
            assert kwargs.get("model") == "custom-model"


class TestRunIntegrationWithRealCommands:
    """Integration tests using real commands (not agents)."""

    async def test_echo_command(self):
        """Test with real echo command."""
        # We can't test real agents without mocking, but we can test
        # the subprocess machinery with echo
        from hatch.context import ExecutionContext

        ctx = ExecutionContext(in_container=False, home_writable=True)

        # Mock get_config to return an echo command
        from hatch.backends import BackendConfig

        config = BackendConfig(
            cmd=["echo", "hello world"],
            env={},
            stdin_data=None,
        )

        with mock.patch("hatch.runner.get_config", return_value=config):
            with mock.patch("hatch.runner.detect_context", return_value=ctx):
                result = await run("ignored", Backend.ZAI)

        assert result.ok is True
        assert "hello world" in result.output
