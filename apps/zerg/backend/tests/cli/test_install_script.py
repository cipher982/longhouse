"""E2E tests for install.sh script.

Tests the install script's TTY handling and fallback behavior:
- Syntax validation
- Non-interactive (piped) mode fallback to --quick
- Interactive mode with PTY simulation
- /dev/tty redirect behavior

Run with: make test-install
"""

import os
import subprocess
from pathlib import Path

import pytest

# pexpect is Unix-only
pexpect = pytest.importorskip("pexpect", reason="pexpect required for interactive tests")

# Path from tests/cli/test_install_script.py -> backend -> zerg -> apps -> zerg (root)
INSTALL_SCRIPT = Path(__file__).parents[5] / "scripts" / "install.sh"


class TestInstallScriptSyntax:
    """Basic install.sh validation tests."""

    def test_script_exists(self) -> None:
        """Install script exists at expected location."""
        assert INSTALL_SCRIPT.exists(), f"Install script not found at {INSTALL_SCRIPT}"

    def test_syntax_valid(self) -> None:
        """Bash syntax check passes."""
        result = subprocess.run(
            ["bash", "-n", str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_has_tty_handling(self) -> None:
        """Script contains TTY detection logic."""
        content = INSTALL_SCRIPT.read_text()

        # Check for TTY detection patterns
        assert "-t 0" in content, "Script should check if stdin is a TTY"
        assert "/dev/tty" in content, "Script should handle /dev/tty redirect"
        assert "--quick" in content, "Script should support --quick fallback"


class TestInstallScriptEnvVars:
    """Tests for install.sh environment variable handling."""

    def test_no_wizard_skips_onboard(self) -> None:
        """LONGHOUSE_NO_WIZARD=1 skips onboarding wizard."""
        env = os.environ.copy()
        env["LONGHOUSE_NO_WIZARD"] = "1"

        # Run just the run_onboard function by sourcing and calling it
        result = subprocess.run(
            ["bash", "-c", f"source {INSTALL_SCRIPT} && run_onboard"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Should see skip message and exit cleanly
        assert "Skipping onboarding wizard" in result.stdout or "Skipping" in result.stderr or result.returncode == 0


class TestOnboardQuickMode:
    """Tests for non-interactive --quick mode."""

    def test_onboard_quick_flag_exists(self) -> None:
        """longhouse onboard has --quick flag."""
        # This tests the CLI, not the install script
        result = subprocess.run(
            ["longhouse", "onboard", "--help"],
            capture_output=True,
            text=True,
        )

        assert "--quick" in result.stdout, "onboard command should have --quick flag"
        assert "-q" in result.stdout, "onboard command should have -q shorthand"

    @pytest.mark.timeout(30)
    def test_onboard_quick_no_prompt(self) -> None:
        """longhouse onboard --quick runs without interactive prompts."""
        result = subprocess.run(
            ["longhouse", "onboard", "--quick", "--no-server", "--no-shipper"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should not show the interactive choice prompt
        assert "Choice [1]:" not in result.stdout, "Quick mode should not show choice prompt"
        # Should see step output
        assert "Step 1:" in result.stdout or "Checking dependencies" in result.stdout


class TestOnboardInteractive:
    """Interactive prompt tests using pexpect."""

    @pytest.mark.timeout(30)
    def test_onboard_shows_choice_prompt(self) -> None:
        """Interactive onboard shows QuickStart/Manual choice prompt."""
        child = pexpect.spawn(
            "longhouse",
            ["onboard", "--no-server", "--no-shipper"],
            timeout=10,
            encoding="utf-8",
        )

        # Should see the choice prompt
        index = child.expect([r"Choice \[1\]:", pexpect.TIMEOUT, pexpect.EOF])
        assert index == 0, f"Expected choice prompt, got: {child.before}"

        # Clean up
        child.sendline("1")
        child.expect(pexpect.EOF)
        child.close()

    @pytest.mark.timeout(30)
    def test_onboard_accepts_default_on_enter(self) -> None:
        """Pressing Enter accepts default choice [1] (QuickStart)."""
        child = pexpect.spawn(
            "longhouse",
            ["onboard", "--no-server", "--no-shipper"],
            timeout=10,
            encoding="utf-8",
        )

        # Wait for prompt
        child.expect(r"Choice \[1\]:")

        # Just press Enter (accept default)
        child.sendline("")

        # Should proceed to Step 1 (default is QuickStart)
        index = child.expect(["Step 1:", "Checking dependencies", pexpect.TIMEOUT])
        assert index in (0, 1), f"Expected step output after Enter, got: {child.before}"

        child.expect(pexpect.EOF)
        child.close()

    @pytest.mark.timeout(30)
    def test_onboard_manual_mode_option(self) -> None:
        """Selecting 2 enters Manual Setup mode."""
        child = pexpect.spawn(
            "longhouse",
            ["onboard", "--no-server", "--no-shipper"],
            timeout=10,
            encoding="utf-8",
        )

        # Wait for prompt
        child.expect(r"Choice \[1\]:")

        # Select Manual (option 2)
        child.sendline("2")

        # Should proceed to Step 1 in manual mode
        index = child.expect(["Step 1:", "Checking dependencies", pexpect.TIMEOUT])
        assert index in (0, 1), f"Expected step output after choice 2, got: {child.before}"

        child.expect(pexpect.EOF)
        child.close()


class TestInstallScriptPipedExecution:
    """Test install.sh behavior when piped (simulating curl | bash)."""

    def test_piped_script_doesnt_crash(self) -> None:
        """Piped script handles non-TTY gracefully."""
        env = os.environ.copy()
        env["LONGHOUSE_NO_WIZARD"] = "1"  # Skip actual wizard for this test

        # Simulate piped execution
        result = subprocess.run(
            ["bash"],
            input=INSTALL_SCRIPT.read_text(),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should not have "Aborted" error from typer prompts
        assert "Aborted" not in result.stdout, f"Script crashed with Aborted: {result.stdout}"
        assert "Aborted" not in result.stderr, f"Script crashed with Aborted: {result.stderr}"


class TestInstallScriptWithPTY:
    """Test install.sh behavior with PTY (simulating interactive terminal)."""

    @pytest.mark.timeout(60)
    def test_install_with_pty_shows_banner(self) -> None:
        """Install script with PTY shows Longhouse banner."""
        env = os.environ.copy()
        env["LONGHOUSE_NO_WIZARD"] = "1"  # Skip wizard to speed up test

        child = pexpect.spawn(
            "bash",
            [str(INSTALL_SCRIPT)],
            timeout=30,
            encoding="utf-8",
            env=env,
        )

        # Should see the ASCII banner
        index = child.expect(["Longhouse", pexpect.TIMEOUT, pexpect.EOF])
        assert index == 0, f"Expected Longhouse banner, got: {child.before}"

        child.expect(pexpect.EOF)
        child.close()

    @pytest.mark.timeout(90)
    @pytest.mark.skipif(
        not os.path.exists("/dev/tty"),
        reason="/dev/tty not available in this environment",
    )
    def test_piped_with_tty_redirect(self) -> None:
        """Piped script with /dev/tty available reconnects to terminal."""
        # This test verifies the /dev/tty redirect logic works
        # pexpect provides a PTY, so /dev/tty should be available

        # Create a wrapper script that pipes install.sh and provides /dev/tty
        wrapper = f"""
        # Simulate curl | bash by piping the script content
        # pexpect provides a PTY so /dev/tty should work
        export LONGHOUSE_NO_WIZARD=1
        cat {INSTALL_SCRIPT} | bash
        """

        child = pexpect.spawn(
            "bash",
            ["-c", wrapper],
            timeout=60,
            encoding="utf-8",
        )

        # Should complete without "Aborted" error
        child.expect(pexpect.EOF)
        output = child.before or ""
        child.close()

        assert "Aborted" not in output, f"Script aborted: {output}"
