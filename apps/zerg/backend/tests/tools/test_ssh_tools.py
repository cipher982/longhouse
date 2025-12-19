"""Tests for SSH tools."""

import subprocess
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.tools.builtin.ssh_tools import MAX_OUTPUT_SIZE
from zerg.tools.builtin.ssh_tools import _parse_host
from zerg.tools.builtin.ssh_tools import ssh_exec


class TestParseHost:
    """Test the _parse_host helper function."""

    def test_parse_user_at_hostname(self):
        """Test parsing 'user@hostname' format."""
        result = _parse_host("ubuntu@example.com")
        assert result is not None
        user, hostname, port = result
        assert user == "ubuntu"
        assert hostname == "example.com"
        assert port == "22"

    def test_parse_user_at_ip(self):
        """Test parsing 'user@ip' format."""
        result = _parse_host("admin@192.168.1.100")
        assert result is not None
        user, hostname, port = result
        assert user == "admin"
        assert hostname == "192.168.1.100"
        assert port == "22"

    def test_parse_user_at_hostname_with_port(self):
        """Test parsing 'user@hostname:port' format."""
        result = _parse_host("deploy@server.example.com:2222")
        assert result is not None
        user, hostname, port = result
        assert user == "deploy"
        assert hostname == "server.example.com"
        assert port == "2222"

    def test_parse_user_at_ip_with_port(self):
        """Test parsing 'user@ip:port' format."""
        result = _parse_host("root@10.0.0.5:2222")
        assert result is not None
        user, hostname, port = result
        assert user == "root"
        assert hostname == "10.0.0.5"
        assert port == "2222"

    def test_parse_invalid_host_no_at(self):
        """Test parsing invalid host without @ symbol."""
        result = _parse_host("invalidhost")
        assert result is None

    def test_parse_invalid_host_empty(self):
        """Test parsing empty host string."""
        result = _parse_host("")
        assert result is None

    def test_parse_invalid_host_only_at(self):
        """Test parsing host with only @ symbol."""
        result = _parse_host("@")
        assert result is None

    def test_parse_invalid_host_no_user(self):
        """Test parsing host with @ but no user."""
        result = _parse_host("@hostname")
        assert result is None

    def test_parse_invalid_host_no_hostname(self):
        """Test parsing host with @ but no hostname."""
        result = _parse_host("user@")
        assert result is None

    def test_parse_invalid_host_no_port_after_colon(self):
        """Test parsing host with colon but no port."""
        result = _parse_host("user@hostname:")
        assert result is None

    def test_parse_invalid_host_non_numeric_port(self):
        """Test parsing host with non-numeric port."""
        result = _parse_host("user@hostname:abc")
        assert result is None


class TestSshExecValidation:
    """Test validation errors in ssh_exec function."""

    def test_empty_host(self):
        """Test that empty host returns validation error."""
        result = ssh_exec(host="", command="echo test")
        assert result["ok"] is False
        assert result["error_type"] == "validation_error"
        assert "host parameter is required" in result["user_message"]

    def test_empty_command(self):
        """Test that empty command returns validation error."""
        result = ssh_exec(host="user@host.com", command="")
        assert result["ok"] is False
        assert result["error_type"] == "validation_error"
        assert "command parameter is required" in result["user_message"]

    def test_invalid_host_format(self):
        """Test that invalid host format returns validation error."""
        result = ssh_exec(host="invalidhost", command="echo test")
        assert result["ok"] is False
        assert result["error_type"] == "validation_error"
        assert "Invalid host format" in result["user_message"]

    def test_valid_user_at_host_format(self):
        """Test that user@host format passes validation."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test output",
                stderr="",
            )
            result = ssh_exec(host="testuser@testhost.com", command="echo test")
            assert result["ok"] is True

    def test_valid_user_at_host_with_port_format(self):
        """Test that user@host:port format passes validation."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test output",
                stderr="",
            )
            result = ssh_exec(host="testuser@testhost.com:2222", command="echo test")
            assert result["ok"] is True


class TestSshExecExecution:
    """Test actual execution behavior (with mocked subprocess)."""

    @patch("subprocess.run")
    def test_successful_command_execution(self, mock_run):
        """Test successful command execution returns success envelope."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="CONTAINER ID   IMAGE\nabc123        nginx",
            stderr="",
        )

        result = ssh_exec(host="deploy@server.example.com", command="docker ps")

        assert result["ok"] is True
        assert result["data"]["host"] == "deploy@server.example.com"
        assert result["data"]["command"] == "docker ps"
        assert result["data"]["exit_code"] == 0
        assert "abc123" in result["data"]["stdout"]
        assert result["data"]["stderr"] == ""
        assert "duration_ms" in result["data"]
        assert isinstance(result["data"]["duration_ms"], int)

    @patch("subprocess.run")
    def test_command_with_non_zero_exit_code(self, mock_run):
        """Test that non-zero exit codes still return success envelope."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="grep: no matches found",
        )

        result = ssh_exec(host="admin@192.168.1.1", command="docker ps | grep nonexistent")

        # Non-zero exit code is NOT an error - command ran successfully
        assert result["ok"] is True
        assert result["data"]["exit_code"] == 1
        assert result["data"]["stderr"] == "grep: no matches found"

    @patch("subprocess.run")
    def test_exit_code_255_is_treated_as_connection_error(self, mock_run):
        """Test that SSH connection failures (exit code 255) return error envelope."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stdout="",
            stderr="Failed to add the host to the list of known hosts.",
        )

        result = ssh_exec(host="user@unreachable.host", command="df -h")

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "SSH connection failed" in result["user_message"]

    @patch("subprocess.run")
    def test_command_with_stderr_output(self, mock_run):
        """Test command with stderr output."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="Warning: something happened",
        )

        result = ssh_exec(host="deploy@prod.example.com", command="some-command")

        assert result["ok"] is True
        assert result["data"]["exit_code"] == 0
        assert result["data"]["stderr"] == "Warning: something happened"

    @patch("subprocess.run")
    def test_output_truncation_stdout(self, mock_run):
        """Test that stdout is truncated if > 10KB."""
        large_output = "x" * (MAX_OUTPUT_SIZE + 1000)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=large_output,
            stderr="",
        )

        result = ssh_exec(host="user@server.example.com", command="cat large-file.txt")

        assert result["ok"] is True
        assert len(result["data"]["stdout"]) < len(large_output)
        assert "[stdout truncated]" in result["data"]["stdout"]
        assert len(result["data"]["stdout"]) <= MAX_OUTPUT_SIZE + 100  # Allow for truncation message

    @patch("subprocess.run")
    def test_output_truncation_stderr(self, mock_run):
        """Test that stderr is truncated if > 10KB."""
        large_error = "e" * (MAX_OUTPUT_SIZE + 1000)
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=large_error,
        )

        result = ssh_exec(host="user@server.example.com", command="failing-command")

        assert result["ok"] is True
        assert len(result["data"]["stderr"]) < len(large_error)
        assert "[stderr truncated]" in result["data"]["stderr"]

    @patch("subprocess.run")
    def test_ssh_command_construction_default_port(self, mock_run):
        """Test that SSH command is constructed correctly with default port."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ssh_exec(host="deploy@server.example.com", command="echo test")

        # Verify SSH command was called with correct arguments
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "ssh"
        assert "-o" in call_args
        assert "StrictHostKeyChecking=no" in call_args
        assert "ConnectTimeout=5" in call_args
        assert "UserKnownHostsFile=/tmp/zerg_known_hosts" in call_args
        assert "GlobalKnownHostsFile=/dev/null" in call_args
        assert "-p" in call_args
        assert "22" in call_args  # default port
        assert "deploy@server.example.com" in call_args
        assert "echo test" in call_args

    @patch("subprocess.run")
    def test_ssh_command_construction_custom_port(self, mock_run):
        """Test that SSH command is constructed correctly with custom port."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ssh_exec(host="admin@10.0.0.5:2222", command="pwd")

        # Verify SSH command was called with correct arguments
        call_args = mock_run.call_args[0][0]
        assert "admin@10.0.0.5" in call_args
        assert "-p" in call_args
        assert "2222" in call_args

    @patch("subprocess.run")
    def test_custom_timeout(self, mock_run):
        """Test that custom timeout is passed to subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ssh_exec(host="user@host.com", command="long-command", timeout_secs=60)

        # Verify timeout was passed
        assert mock_run.call_args[1]["timeout"] == 60

    @patch("subprocess.run")
    def test_timeout_returns_error(self, mock_run):
        """Test that timeout returns execution error."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=30)

        result = ssh_exec(host="user@host.com", command="sleep 100")

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "timed out after 30 seconds" in result["user_message"]

    @patch("subprocess.run")
    def test_ssh_connection_failure(self, mock_run):
        """Test that SSH connection failure returns error."""
        mock_run.side_effect = subprocess.CalledProcessError(returncode=255, cmd="ssh")

        result = ssh_exec(host="user@host.com", command="echo test")

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"

    @patch("subprocess.run")
    def test_ssh_binary_not_found(self, mock_run):
        """Test that missing SSH binary returns error."""
        mock_run.side_effect = FileNotFoundError()

        result = ssh_exec(host="user@host.com", command="echo test")

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "SSH client not found" in result["user_message"]

    @patch("subprocess.run")
    def test_unexpected_exception(self, mock_run):
        """Test that unexpected exceptions return error."""
        mock_run.side_effect = RuntimeError("Unexpected error")

        result = ssh_exec(host="user@host.com", command="echo test")

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
        assert "Unexpected error" in result["user_message"]


class TestSshExecHostValidation:
    """Test host format validation."""

    def test_invalid_hosts_rejected(self):
        """Test that invalid host formats are rejected."""
        invalid_hosts = [
            "production-server",  # No @ symbol
            "database-01",
            "api-gateway",
            "unknown",
            "@hostname",  # No user
            "user@",  # No hostname
        ]

        for host in invalid_hosts:
            result = ssh_exec(host=host, command="echo test")
            assert result["ok"] is False
            assert result["error_type"] == "validation_error"
            assert "Invalid host format" in result["user_message"]

    @patch("subprocess.run")
    def test_valid_user_at_host_format_works(self, mock_run):
        """Test that valid user@host format works."""
        mock_run.return_value = MagicMock(returncode=0, stdout="test", stderr="")

        result = ssh_exec(host="customuser@custom.host.com", command="echo test")

        assert result["ok"] is True
        assert result["data"]["host"] == "customuser@custom.host.com"

    @patch("subprocess.run")
    def test_valid_user_at_ip_format_works(self, mock_run):
        """Test that valid user@ip format works."""
        mock_run.return_value = MagicMock(returncode=0, stdout="test", stderr="")

        result = ssh_exec(host="admin@192.168.1.100", command="echo test")

        assert result["ok"] is True
        assert result["data"]["host"] == "admin@192.168.1.100"


class TestSshExecResponseStructure:
    """Test the response structure matches expected format."""

    @patch("subprocess.run")
    def test_success_response_structure(self, mock_run):
        """Test that success response has all required fields."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="output",
            stderr="",
        )

        result = ssh_exec(host="user@server.example.com", command="echo test")

        # Check envelope structure
        assert "ok" in result
        assert result["ok"] is True
        assert "data" in result

        # Check data fields
        data = result["data"]
        assert "host" in data
        assert "command" in data
        assert "exit_code" in data
        assert "stdout" in data
        assert "stderr" in data
        assert "duration_ms" in data

        # Check types
        assert isinstance(data["host"], str)
        assert isinstance(data["command"], str)
        assert isinstance(data["exit_code"], int)
        assert isinstance(data["stdout"], str)
        assert isinstance(data["stderr"], str)
        assert isinstance(data["duration_ms"], int)

    def test_error_response_structure(self):
        """Test that error response has all required fields."""
        result = ssh_exec(host="", command="test")

        # Check envelope structure
        assert "ok" in result
        assert result["ok"] is False
        assert "error_type" in result
        assert "user_message" in result

        # Should not have data field
        assert "data" not in result

        # Check types
        assert isinstance(result["error_type"], str)
        assert isinstance(result["user_message"], str)

    @patch("subprocess.run")
    def test_duration_is_reasonable(self, mock_run):
        """Test that duration_ms is a reasonable value."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = ssh_exec(host="user@server.example.com", command="echo test")

        assert result["ok"] is True
        duration = result["data"]["duration_ms"]

        # Duration should be positive and reasonable (< 1 minute for mocked call)
        assert duration >= 0
        assert duration < 60000  # Less than 60 seconds


class TestSshToolsIntegration:
    """Test integration with tool registry."""

    def test_tools_list_exists(self):
        """Test that TOOLS list is exported."""
        from zerg.tools.builtin.ssh_tools import TOOLS

        assert TOOLS is not None
        assert isinstance(TOOLS, list)
        assert len(TOOLS) > 0

    def test_ssh_exec_tool_registered(self):
        """Test that ssh_exec tool is in TOOLS list."""
        from zerg.tools.builtin.ssh_tools import TOOLS

        tool_names = [tool.name for tool in TOOLS]
        assert "ssh_exec" in tool_names

    def test_ssh_exec_tool_has_description(self):
        """Test that ssh_exec tool has a description."""
        from zerg.tools.builtin.ssh_tools import TOOLS

        ssh_tool = next(tool for tool in TOOLS if tool.name == "ssh_exec")
        assert ssh_tool.description is not None
        assert len(ssh_tool.description) > 0
        assert "SSH" in ssh_tool.description or "remote" in ssh_tool.description.lower()
