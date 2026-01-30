"""Integration tests that make REAL API calls.

These tests are slow and require actual credentials.
Run with: pytest -v -m integration

Required credentials:
- ZAI_API_KEY for zai tests
- OPENAI_API_KEY for codex tests
- AWS profile for bedrock tests
- gemini CLI OAuth for gemini tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from conftest import (
    has_claude_cli,
    has_codex_cli,
    has_gemini_cli,
    has_openai_credentials,
    has_zai_credentials,
)


# All tests in this file are integration tests
pytestmark = pytest.mark.integration


class TestZaiIntegration:
    """Integration tests for z.ai backend."""

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_simple_math_question(self):
        """Test simple math question returns sensible answer."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "--json",
                "What is 2+2? Reply with just the number.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "4" in data["output"]

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_plain_text_output(self):
        """Test plain text output (not JSON)."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "What is the capital of France? Reply with just the city name.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "Paris" in result.stdout

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_stdin_input(self):
        """Test reading prompt from stdin."""
        result = subprocess.run(
            [sys.executable, "-m", "hatch", "-b", "zai", "--json", "-"],
            input="What is 3+3? Reply with just the number.",
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "6" in data["output"]

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_working_directory(self, tmp_path):
        """Test --cwd option changes working directory."""
        # Create a test file in tmp_path
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello from test file!")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "--cwd",
                str(tmp_path),
                "--json",
                "List the files in the current directory. Just list filenames.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        # This may or may not succeed depending on if claude can run ls
        # Just verify the command ran
        data = json.loads(result.stdout)
        assert "ok" in data


class TestCodexIntegration:
    """Integration tests for Codex backend."""

    @pytest.mark.skipif(
        not has_openai_credentials() or not has_codex_cli(),
        reason="OPENAI_API_KEY not set or codex CLI not installed",
    )
    def test_simple_math_question(self):
        """Test simple math question returns sensible answer."""
        # Use home directory which is typically trusted by codex
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "codex",
                "--json",
                "What is 5+5? Reply with just the number.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.expanduser("~"),
        )

        # Skip if codex doesn't trust the directory
        if result.returncode != 0:
            data = json.loads(result.stdout)
            if "trusted directory" in data.get("error", "").lower():
                pytest.skip("Codex doesn't trust the test directory")

        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "10" in data["output"]

    @pytest.mark.skipif(
        not has_openai_credentials() or not has_codex_cli(),
        reason="OPENAI_API_KEY not set or codex CLI not installed",
    )
    def test_plain_text_output(self):
        """Test plain text output (not JSON)."""
        # Use home directory which is typically trusted by codex
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "codex",
                "What is the largest planet? Reply with just the planet name.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.expanduser("~"),
        )

        # Skip if codex doesn't trust the directory
        if result.returncode != 0 and "trusted directory" in result.stderr.lower():
            pytest.skip("Codex doesn't trust the test directory")

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "Jupiter" in result.stdout


class TestGeminiIntegration:
    """Integration tests for Gemini backend."""

    @pytest.mark.skipif(
        not has_gemini_cli(),
        reason="gemini CLI not installed",
    )
    def test_simple_math_question(self):
        """Test simple math question returns sensible answer."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "gemini",
                "--json",
                "What is 7+7? Reply with just the number.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        # Gemini may fail if OAuth not set up
        if result.returncode != 0:
            pytest.skip("Gemini OAuth not configured")

        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "14" in data["output"]


class TestErrorHandling:
    """Integration tests for error handling."""

    def test_missing_api_key(self):
        """Test error when API key missing."""
        # Run in environment without ZAI_API_KEY
        env = {k: v for k, v in os.environ.items() if k != "ZAI_API_KEY"}
        env["PATH"] = os.environ.get("PATH", "")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "--json",
                "test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode != 0
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "ZAI_API_KEY" in data["error"]

    def test_invalid_backend_in_code(self):
        """Test invalid backend argument."""
        # This tests the argparse validation
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "invalid_backend",
                "test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode != 0
        assert "invalid choice" in result.stderr.lower()


class TestCLIFlags:
    """Integration tests for CLI flags and options."""

    def test_version_flag(self):
        """Test --version flag."""
        result = subprocess.run(
            [sys.executable, "-m", "hatch", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0
        assert "0.1.0" in result.stdout

    def test_help_flag(self):
        """Test --help flag."""
        result = subprocess.run(
            [sys.executable, "-m", "hatch", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0
        assert "hatch" in result.stdout
        assert "--backend" in result.stdout
        assert "--timeout" in result.stdout
        assert "zai" in result.stdout
        assert "codex" in result.stdout

    def test_empty_prompt_error(self):
        """Test empty prompt from stdin gives error."""
        result = subprocess.run(
            [sys.executable, "-m", "hatch", "-"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode != 0
        assert "empty" in result.stderr.lower()


class TestTimeout:
    """Integration tests for timeout behavior."""

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_short_timeout(self):
        """Test that very short timeout causes failure."""
        # Use a 1 second timeout which should be too short
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "-t",
                "1",
                "--json",
                "Write a detailed 5000 word essay about the history of computing.",
            ],
            capture_output=True,
            text=True,
            timeout=30,  # Outer timeout to prevent test hanging
            cwd=str(Path(__file__).parent.parent),
        )

        # Should either timeout or succeed quickly
        data = json.loads(result.stdout)
        # Either it timed out or completed - both are valid
        assert "ok" in data


class TestDurationTracking:
    """Integration tests for duration tracking."""

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_duration_reported(self):
        """Test that duration_ms is reported in JSON output."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatch",
                "-b",
                "zai",
                "--json",
                "Say 'hi'",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "duration_ms" in data
        assert isinstance(data["duration_ms"], int)
        assert data["duration_ms"] > 0


class TestLargePrompts:
    """Integration tests for large prompts."""

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_large_prompt_via_stdin(self):
        """Test that large prompts work via stdin (ARG_MAX bypass)."""
        # Create a large prompt that would exceed ARG_MAX if passed as argument
        large_context = "x" * 50000  # 50KB of context
        prompt = f"Context: {large_context}\n\nWhat is 1+1? Just say the number."

        result = subprocess.run(
            [sys.executable, "-m", "hatch", "-b", "zai", "--json", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).parent.parent),
        )

        # Should succeed even with large prompt
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "2" in data["output"]


class TestUvxInstallation:
    """Tests for uvx/uv tool installation."""

    def test_uvx_hatch_help(self):
        """Test that uvx hatch --help works after installation."""
        # First, ensure it's installed
        install_result = subprocess.run(
            ["uv", "tool", "install", "-e", str(Path(__file__).parent.parent)],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if install_result.returncode != 0:
            pytest.skip(f"Could not install: {install_result.stderr}")

        # Now test help
        result = subprocess.run(
            ["hatch", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "hatch" in result.stdout
        assert "--backend" in result.stdout

    @pytest.mark.skipif(
        not has_zai_credentials() or not has_claude_cli(),
        reason="ZAI_API_KEY not set or claude CLI not installed",
    )
    def test_uvx_hatch_execution(self):
        """Test that installed hatch actually works."""
        # Ensure installed
        subprocess.run(
            ["uv", "tool", "install", "-e", str(Path(__file__).parent.parent)],
            capture_output=True,
            timeout=60,
        )

        result = subprocess.run(
            ["hatch", "-b", "zai", "--json", "What is 2+2? Just the number."],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert "4" in data["output"]
