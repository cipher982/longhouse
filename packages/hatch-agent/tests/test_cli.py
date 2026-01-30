"""Tests for CLI interface."""

from __future__ import annotations

import io
import json
import os
import sys
from unittest import mock

import pytest

from hatch.cli import (
    EXIT_AGENT_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_NOT_FOUND,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    create_parser,
    get_prompt,
    main,
    result_to_exit_code,
)
from hatch.runner import AgentResult


class TestCreateParser:
    """Tests for argument parser creation."""

    def test_parser_creates(self):
        """Parser is created successfully."""
        parser = create_parser()
        assert parser is not None
        assert parser.prog == "hatch"

    def test_default_backend(self):
        """Default backend is zai."""
        parser = create_parser()
        args = parser.parse_args(["test prompt"])
        assert args.backend == "zai"

    def test_backend_short_flag(self):
        """Short -b flag works."""
        parser = create_parser()
        args = parser.parse_args(["-b", "codex", "test"])
        assert args.backend == "codex"

    def test_backend_long_flag(self):
        """Long --backend flag works."""
        parser = create_parser()
        args = parser.parse_args(["--backend", "bedrock", "test"])
        assert args.backend == "bedrock"

    def test_all_backends_valid(self):
        """All backend choices are valid."""
        parser = create_parser()
        for backend in ["zai", "bedrock", "codex", "gemini"]:
            args = parser.parse_args(["-b", backend, "test"])
            assert args.backend == backend

    def test_invalid_backend_rejected(self):
        """Invalid backend raises error."""
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["-b", "invalid", "test"])

    def test_timeout_default(self):
        """Default timeout is 300."""
        parser = create_parser()
        args = parser.parse_args(["test"])
        assert args.timeout == 300

    def test_timeout_short_flag(self):
        """Short -t flag works."""
        parser = create_parser()
        args = parser.parse_args(["-t", "60", "test"])
        assert args.timeout == 60

    def test_timeout_long_flag(self):
        """Long --timeout flag works."""
        parser = create_parser()
        args = parser.parse_args(["--timeout", "120", "test"])
        assert args.timeout == 120

    def test_cwd_flag(self):
        """--cwd flag works."""
        parser = create_parser()
        args = parser.parse_args(["--cwd", "/path/to/dir", "test"])
        assert args.cwd == "/path/to/dir"

    def test_cwd_short_flag(self):
        """Short -C flag works."""
        parser = create_parser()
        args = parser.parse_args(["-C", "/path", "test"])
        assert args.cwd == "/path"

    def test_model_flag(self):
        """--model flag works."""
        parser = create_parser()
        args = parser.parse_args(["--model", "gpt-5", "test"])
        assert args.model == "gpt-5"

    def test_output_format_flag(self):
        """--output-format flag works."""
        parser = create_parser()
        args = parser.parse_args(["--output-format", "stream-json", "test"])
        assert args.output_format == "stream-json"

    def test_output_format_default(self):
        """Default output format is text."""
        parser = create_parser()
        args = parser.parse_args(["test"])
        assert args.output_format == "text"

    def test_include_partial_messages_flag(self):
        """--include-partial-messages flag works."""
        parser = create_parser()
        args = parser.parse_args(["--include-partial-messages", "test"])
        assert args.include_partial_messages is True

    def test_api_key_flag(self):
        """--api-key flag works."""
        parser = create_parser()
        args = parser.parse_args(["--api-key", "sk-xxx", "test"])
        assert args.api_key == "sk-xxx"

    def test_json_flag(self):
        """--json flag works."""
        parser = create_parser()
        args = parser.parse_args(["--json", "test"])
        assert args.json_output is True

    def test_json_flag_default_false(self):
        """JSON output is off by default."""
        parser = create_parser()
        args = parser.parse_args(["test"])
        assert args.json_output is False

    def test_prompt_captured(self):
        """Prompt is captured from positional argument."""
        parser = create_parser()
        args = parser.parse_args(["my test prompt"])
        assert args.prompt == "my test prompt"

    def test_prompt_with_spaces(self):
        """Prompt with spaces works."""
        parser = create_parser()
        args = parser.parse_args(["fix the bug in auth.py"])
        assert args.prompt == "fix the bug in auth.py"

    def test_prompt_optional(self):
        """Prompt is optional (can read from stdin)."""
        parser = create_parser()
        args = parser.parse_args([])
        assert args.prompt is None

    def test_dash_means_stdin(self):
        """'-' as prompt means read from stdin."""
        parser = create_parser()
        args = parser.parse_args(["-"])
        assert args.prompt == "-"

    def test_version_flag(self):
        """--version exits cleanly."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0

    def test_help_flag(self):
        """--help exits cleanly."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0


class TestGetPrompt:
    """Tests for get_prompt function."""

    def test_prompt_from_args(self):
        """Returns prompt from args."""
        parser = create_parser()
        args = parser.parse_args(["test prompt"])
        assert get_prompt(args) == "test prompt"

    def test_stdin_on_dash(self):
        """Reads stdin when prompt is '-'."""
        parser = create_parser()
        args = parser.parse_args(["-"])

        with mock.patch.object(sys, "stdin", io.StringIO("stdin prompt\n")):
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                prompt = get_prompt(args)

        assert prompt == "stdin prompt\n"

    def test_stdin_on_none(self):
        """Reads stdin when prompt is None."""
        parser = create_parser()
        args = parser.parse_args([])

        with mock.patch.object(sys, "stdin", io.StringIO("stdin prompt")):
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                prompt = get_prompt(args)

        assert prompt == "stdin prompt"

    def test_empty_stdin_exits(self):
        """Empty stdin causes exit."""
        parser = create_parser()
        args = parser.parse_args([])

        with mock.patch.object(sys, "stdin", io.StringIO("")):
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    get_prompt(args)

        assert exc_info.value.code == EXIT_CONFIG_ERROR


class TestResultToExitCode:
    """Tests for result_to_exit_code function."""

    def test_success(self):
        """OK result returns EXIT_SUCCESS."""
        result = AgentResult(ok=True, output="out", exit_code=0, duration_ms=100)
        assert result_to_exit_code(result) == EXIT_SUCCESS

    def test_timeout(self):
        """Timeout returns EXIT_TIMEOUT."""
        result = AgentResult(ok=False, output="", exit_code=-1, duration_ms=5000)
        assert result_to_exit_code(result) == EXIT_TIMEOUT

    def test_not_found(self):
        """Not found returns EXIT_NOT_FOUND."""
        result = AgentResult(ok=False, output="", exit_code=-2, duration_ms=10)
        assert result_to_exit_code(result) == EXIT_NOT_FOUND

    def test_agent_error(self):
        """Agent error returns EXIT_AGENT_ERROR."""
        result = AgentResult(ok=False, output="", exit_code=1, duration_ms=100)
        assert result_to_exit_code(result) == EXIT_AGENT_ERROR


class TestMain:
    """Tests for main function."""

    @pytest.fixture
    def mock_run_sync(self):
        """Mock run_sync to avoid actual subprocess calls."""
        with mock.patch("hatch.cli.run_sync") as m:
            m.return_value = ("output", "", 0, False)
            yield m

    @pytest.fixture
    def mock_get_config(self):
        """Mock get_config."""
        from hatch.backends import BackendConfig

        config = BackendConfig(
            cmd=["test"], env={}, stdin_data=b"prompt"
        )
        with mock.patch("hatch.cli.get_config", return_value=config):
            yield config

    @pytest.fixture
    def mock_detect_context(self):
        """Mock detect_context."""
        from hatch.context import ExecutionContext

        ctx = ExecutionContext(in_container=False, home_writable=True)
        with mock.patch("hatch.cli.detect_context", return_value=ctx):
            yield ctx

    def test_success_output(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """Successful run outputs result."""
        mock_run_sync.return_value = ("Hello World", "", 0, False)

        exit_code = main(["test prompt"])

        assert exit_code == EXIT_SUCCESS
        captured = capsys.readouterr()
        assert "Hello World" in captured.out

    def test_json_output(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """JSON output mode works."""
        mock_run_sync.return_value = ("output text", "", 0, False)

        exit_code = main(["--json", "test prompt"])

        assert exit_code == EXIT_SUCCESS
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is True
        assert data["output"] == "output text"

    def test_error_to_stderr(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """Errors go to stderr."""
        mock_run_sync.return_value = ("", "error msg", 1, False)

        exit_code = main(["test prompt"])

        assert exit_code == EXIT_AGENT_ERROR
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_json_error(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """JSON mode includes errors."""
        mock_run_sync.return_value = ("", "error msg", 1, False)

        exit_code = main(["--json", "test prompt"])

        assert exit_code == EXIT_AGENT_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is False
        assert data["error"] is not None

    def test_timeout_exit_code(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key
    ):
        """Timeout returns correct exit code."""
        mock_run_sync.return_value = ("", "", -1, True)

        exit_code = main(["test prompt"])

        assert exit_code == EXIT_TIMEOUT

    def test_missing_api_key(self, clean_env, mock_detect_context, capsys):
        """Missing API key returns config error."""
        # Don't mock get_config - let it fail
        exit_code = main(["-b", "zai", "test prompt"])

        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        assert "ZAI_API_KEY" in captured.err or "Error" in captured.err

    def test_missing_api_key_json(self, clean_env, mock_detect_context, capsys):
        """Missing API key in JSON mode."""
        exit_code = main(["--json", "-b", "zai", "test prompt"])

        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is False
        assert "ZAI_API_KEY" in data["error"]

    def test_cli_not_found(
        self, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """CLI not found returns correct exit code."""
        with mock.patch(
            "hatch.cli.run_sync",
            side_effect=FileNotFoundError("claude not found"),
        ):
            exit_code = main(["test prompt"])

        assert exit_code == EXIT_NOT_FOUND

    def test_backend_passed_correctly(
        self, mock_run_sync, mock_detect_context, mock_zai_key
    ):
        """Backend is passed to get_config."""
        mock_run_sync.return_value = ("output", "", 0, False)

        with mock.patch("hatch.cli.get_config") as mock_config:
            from hatch.backends import BackendConfig, Backend

            mock_config.return_value = BackendConfig(
                cmd=["test"], env={}, stdin_data=b"test"
            )

            main(["-b", "bedrock", "test"])

            mock_config.assert_called_once()
            args = mock_config.call_args[0]
            assert args[0] == Backend.BEDROCK

    def test_model_kwarg_passed(
        self, mock_run_sync, mock_detect_context, mock_zai_key
    ):
        """Model is passed as kwarg."""
        mock_run_sync.return_value = ("output", "", 0, False)

        with mock.patch("hatch.cli.get_config") as mock_config:
            from hatch.backends import BackendConfig

            mock_config.return_value = BackendConfig(
                cmd=["test"], env={}, stdin_data=b"test"
            )

            main(["--model", "custom-model", "test"])

            kwargs = mock_config.call_args[1]
            assert kwargs.get("model") == "custom-model"

    def test_api_key_kwarg_passed(
        self, mock_run_sync, mock_detect_context
    ):
        """API key is passed as kwarg."""
        mock_run_sync.return_value = ("output", "", 0, False)

        with mock.patch("hatch.cli.get_config") as mock_config:
            from hatch.backends import BackendConfig

            mock_config.return_value = BackendConfig(
                cmd=["test"], env={}, stdin_data=b"test"
            )

            main(["--api-key", "sk-test", "-b", "zai", "test"])

            kwargs = mock_config.call_args[1]
            assert kwargs.get("api_key") == "sk-test"

    def test_timeout_passed(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key
    ):
        """Timeout is passed to run_sync."""
        mock_run_sync.return_value = ("output", "", 0, False)

        main(["-t", "60", "test prompt"])

        call_args = mock_run_sync.call_args[0]
        assert call_args[4] == 60  # timeout_s is 5th positional arg

    def test_invalid_timeout(self, capsys):
        """Non-positive timeout returns config error."""
        exit_code = main(["-t", "0", "test prompt"])
        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        assert "timeout" in captured.err.lower()

    def test_invalid_timeout_json(self, capsys):
        """Non-positive timeout returns config error in JSON mode."""
        exit_code = main(["--json", "-t", "-5", "test prompt"])
        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is False
        assert "timeout" in data["error"].lower()

    def test_cwd_passed(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key
    ):
        """CWD is passed to run_sync."""
        mock_run_sync.return_value = ("output", "", 0, False)

        # Use /tmp which exists on all systems
        main(["--cwd", "/tmp", "test prompt"])

        call_args = mock_run_sync.call_args[0]
        assert call_args[3] == "/tmp"  # cwd is 4th positional arg

    def test_invalid_cwd(self, capsys):
        """Invalid cwd returns config error."""
        exit_code = main(["--cwd", "/does/not/exist", "test prompt"])
        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        assert "cwd" in captured.err.lower()

    def test_invalid_cwd_json(self, capsys):
        """Invalid cwd returns config error in JSON mode."""
        exit_code = main(["--json", "--cwd", "/does/not/exist", "test prompt"])
        assert exit_code == EXIT_CONFIG_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["ok"] is False
        assert "cwd" in data["error"].lower()

    def test_strips_trailing_whitespace(
        self, mock_run_sync, mock_get_config, mock_detect_context, mock_zai_key, capsys
    ):
        """Output trailing whitespace is stripped."""
        mock_run_sync.return_value = ("output\n\n\n", "", 0, False)

        main(["test prompt"])

        captured = capsys.readouterr()
        # Should have exactly one newline (from print)
        assert captured.out == "output\n"


class TestMainWithStdin:
    """Tests for main with stdin input."""

    def test_reads_stdin(self, mock_zai_key, capsys):
        """Reads prompt from stdin when not provided."""
        with mock.patch.object(sys, "stdin", io.StringIO("stdin prompt")):
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                with mock.patch("hatch.cli.run_sync") as mock_run:
                    mock_run.return_value = ("output", "", 0, False)

                    with mock.patch("hatch.cli.get_config") as mock_config:
                        from hatch.backends import BackendConfig

                        mock_config.return_value = BackendConfig(
                            cmd=["test"], env={}, stdin_data=b"test"
                        )

                        with mock.patch("hatch.cli.detect_context"):
                            main([])

                    # Verify get_config was called with stdin prompt
                    call_args = mock_config.call_args[0]
                    assert call_args[1] == "stdin prompt"
