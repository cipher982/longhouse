"""Tests for command validation service.

Tests capability enforcement for both exec.readonly and exec.full modes,
including special handling for systemctl, journalctl, and docker commands.
"""

import pytest

from zerg.services.command_validator import CommandValidator


@pytest.fixture
def validator():
    """Create a CommandValidator instance for testing."""
    return CommandValidator()


# ---------------------------------------------------------------------------
# exec.readonly tests
# ---------------------------------------------------------------------------


def test_simple_allowed_command(validator):
    """df -h should be allowed in readonly mode."""
    allowed, reason = validator.validate("df -h", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_multiple_args_allowed(validator):
    """Commands with multiple args should work if base command is allowed."""
    allowed, reason = validator.validate("ps aux", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_echo_allowed(validator):
    """echo command should be allowed (safe for testing/debugging)."""
    allowed, reason = validator.validate("echo 'hello world'", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_true_false_allowed(validator):
    """true/false commands should be allowed (safe test commands)."""
    allowed, reason = validator.validate("true", ["exec.readonly"])
    assert allowed is True
    assert reason is None

    allowed, reason = validator.validate("false", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_shell_metacharacters_denied(validator):
    """df -h; rm -rf / should be denied due to semicolon."""
    allowed, reason = validator.validate("df -h; rm -rf /", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_pipe_denied(validator):
    """ps aux | grep python should be denied due to pipe."""
    allowed, reason = validator.validate("ps aux | grep python", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_redirect_denied(validator):
    """echo foo > /etc/passwd should be denied due to redirect."""
    allowed, reason = validator.validate("echo foo > /etc/passwd", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_subshell_denied(validator):
    """$(whoami) should be denied due to subshell."""
    allowed, reason = validator.validate("$(whoami)", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_backticks_denied(validator):
    """`whoami` should be denied due to command substitution."""
    allowed, reason = validator.validate("`whoami`", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_variable_expansion_denied(validator):
    """$HOME should be denied due to variable expansion."""
    allowed, reason = validator.validate("echo $HOME", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_command_not_in_allowlist(validator):
    """Commands not in allowlist should be denied."""
    allowed, reason = validator.validate("vim file.txt", ["exec.readonly"])
    assert allowed is False
    assert "allowlist" in reason.lower()


def test_destructive_command_denied(validator):
    """rm -rf should be explicitly denied."""
    allowed, reason = validator.validate("rm -rf /tmp/test", ["exec.readonly"])
    assert allowed is False
    assert "blocked" in reason.lower() or "destructive" in reason.lower()


def test_empty_command_denied(validator):
    """Empty command should be denied."""
    allowed, reason = validator.validate("", ["exec.readonly"])
    assert allowed is False
    assert "empty" in reason.lower()


def test_whitespace_only_denied(validator):
    """Whitespace-only command should be denied."""
    allowed, reason = validator.validate("   ", ["exec.readonly"])
    assert allowed is False
    assert "empty" in reason.lower()


# ---------------------------------------------------------------------------
# systemctl tests
# ---------------------------------------------------------------------------


def test_systemctl_status_allowed(validator):
    """systemctl status nginx should be allowed."""
    allowed, reason = validator.validate("systemctl status nginx", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_systemctl_restart_denied(validator):
    """systemctl restart nginx should be denied."""
    allowed, reason = validator.validate("systemctl restart nginx", ["exec.readonly"])
    assert allowed is False
    assert "status" in reason.lower()


def test_systemctl_start_denied(validator):
    """systemctl start nginx should be denied."""
    allowed, reason = validator.validate("systemctl start nginx", ["exec.readonly"])
    assert allowed is False
    assert "status" in reason.lower()


def test_systemctl_stop_denied(validator):
    """systemctl stop nginx should be denied."""
    allowed, reason = validator.validate("systemctl stop nginx", ["exec.readonly"])
    assert allowed is False
    assert "status" in reason.lower()


def test_systemctl_no_subcommand_denied(validator):
    """systemctl without subcommand should be denied."""
    allowed, reason = validator.validate("systemctl", ["exec.readonly"])
    assert allowed is False


# ---------------------------------------------------------------------------
# journalctl tests
# ---------------------------------------------------------------------------


def test_journalctl_with_no_pager_allowed(validator):
    """journalctl --no-pager -n 100 should be allowed."""
    allowed, reason = validator.validate(
        "journalctl --no-pager -n 100", ["exec.readonly"]
    )
    assert allowed is True
    assert reason is None


def test_journalctl_without_no_pager_denied(validator):
    """journalctl -n 100 should be denied (can hang)."""
    allowed, reason = validator.validate("journalctl -n 100", ["exec.readonly"])
    assert allowed is False
    assert "no-pager" in reason.lower()


def test_journalctl_no_pager_flag_order_doesnt_matter(validator):
    """journalctl -n 100 --no-pager should be allowed."""
    allowed, reason = validator.validate(
        "journalctl -n 100 --no-pager", ["exec.readonly"]
    )
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------------
# docker tests
# ---------------------------------------------------------------------------


def test_docker_ps_allowed_with_capability(validator):
    """docker ps should be allowed with docker capability."""
    allowed, reason = validator.validate("docker ps", ["exec.readonly", "docker"])
    assert allowed is True
    assert reason is None


def test_docker_ps_denied_without_capability(validator):
    """docker ps should be denied without docker capability."""
    allowed, reason = validator.validate("docker ps", ["exec.readonly"])
    assert allowed is False
    assert "docker" in reason.lower()
    assert "capability" in reason.lower()


def test_docker_logs_allowed_with_capability(validator):
    """docker logs should be allowed with docker capability."""
    allowed, reason = validator.validate(
        "docker logs --tail 100 my-container", ["exec.readonly", "docker"]
    )
    assert allowed is True
    assert reason is None


def test_docker_stats_allowed_with_capability(validator):
    """docker stats should be allowed with docker capability."""
    allowed, reason = validator.validate(
        "docker stats --no-stream", ["exec.readonly", "docker"]
    )
    assert allowed is True
    assert reason is None


def test_docker_inspect_allowed_with_capability(validator):
    """docker inspect should be allowed with docker capability."""
    allowed, reason = validator.validate(
        "docker inspect my-container", ["exec.readonly", "docker"]
    )
    assert allowed is True
    assert reason is None


def test_docker_run_denied(validator):
    """docker run should be denied even with docker capability."""
    allowed, reason = validator.validate(
        "docker run ubuntu echo hello", ["exec.readonly", "docker"]
    )
    assert allowed is False
    assert "readonly" in reason.lower() or "allowed" in reason.lower()


def test_docker_rm_denied(validator):
    """docker rm should be denied even with docker capability."""
    allowed, reason = validator.validate(
        "docker rm my-container", ["exec.readonly", "docker"]
    )
    assert allowed is False


def test_docker_stop_denied(validator):
    """docker stop should be denied even with docker capability."""
    allowed, reason = validator.validate(
        "docker stop my-container", ["exec.readonly", "docker"]
    )
    assert allowed is False


# ---------------------------------------------------------------------------
# exec.full tests
# ---------------------------------------------------------------------------


def test_exec_full_allows_simple_commands(validator):
    """exec.full should allow simple commands."""
    allowed, reason = validator.validate("df -h", ["exec.full"])
    assert allowed is True
    assert reason is None


def test_exec_full_allows_everything(validator):
    """exec.full should allow any command."""
    allowed, reason = validator.validate(
        "rm -rf /tmp/test", ["exec.full"]
    )
    assert allowed is True
    assert reason is None


def test_exec_full_allows_pipes(validator):
    """exec.full should allow pipes."""
    allowed, reason = validator.validate("ps aux | grep python", ["exec.full"])
    assert allowed is True
    assert reason is None


def test_exec_full_allows_redirects(validator):
    """exec.full should allow redirects."""
    allowed, reason = validator.validate("echo foo > /tmp/test.txt", ["exec.full"])
    assert allowed is True
    assert reason is None


def test_exec_full_allows_subshells(validator):
    """exec.full should allow subshells."""
    allowed, reason = validator.validate("echo $(whoami)", ["exec.full"])
    assert allowed is True
    assert reason is None


def test_exec_full_allows_systemctl_restart(validator):
    """exec.full should allow systemctl restart."""
    allowed, reason = validator.validate("systemctl restart nginx", ["exec.full"])
    assert allowed is True
    assert reason is None


def test_exec_full_allows_docker_run(validator):
    """exec.full should allow docker run."""
    allowed, reason = validator.validate(
        "docker run ubuntu echo hello", ["exec.full"]
    )
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_absolute_path_command(validator):
    """/usr/bin/df should be allowed (extracts base command)."""
    allowed, reason = validator.validate("/usr/bin/df -h", ["exec.readonly"])
    assert allowed is True
    assert reason is None


def test_absolute_path_destructive_command(validator):
    """/bin/rm should be denied (extracts base command)."""
    allowed, reason = validator.validate("/bin/rm -rf /tmp/test", ["exec.readonly"])
    assert allowed is False
    assert "blocked" in reason.lower() or "destructive" in reason.lower()


def test_command_with_newline_denied(validator):
    """Commands with newlines should be denied."""
    allowed, reason = validator.validate("df -h\nrm -rf /", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_command_with_backslash_denied(validator):
    """Commands with backslashes should be denied (line continuation)."""
    allowed, reason = validator.validate("df \\\n-h", ["exec.readonly"])
    assert allowed is False
    assert "metacharacters" in reason.lower()


def test_multiple_capabilities(validator):
    """Multiple capabilities should work together."""
    allowed, reason = validator.validate(
        "docker ps", ["exec.readonly", "docker", "other-capability"]
    )
    assert allowed is True
    assert reason is None


def test_case_sensitive_command_names(validator):
    """Command names should be case-sensitive (DF is not df)."""
    allowed, reason = validator.validate("DF -h", ["exec.readonly"])
    assert allowed is False
    assert "allowlist" in reason.lower()
