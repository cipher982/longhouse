"""Tests for the connect CLI command."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.services.shipper import ShipResult

# Use env to disable Rich's terminal detection for consistent output in CI
# Rich/Typer can produce inconsistent output based on terminal settings
runner = CliRunner(mix_stderr=False, env={"TERM": "dumb", "NO_COLOR": "1", "COLUMNS": "120"})


@pytest.fixture
def mock_projects_dir(tmp_path: Path) -> Path:
    """Create a mock projects directory structure."""
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)

    project_dir = projects_dir / "-Users-test-project"
    project_dir.mkdir()

    session_file = project_dir / "test-session.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "msg-1",
                "timestamp": "2026-01-28T10:00:00Z",
                "cwd": "/Users/test/project",
                "message": {"content": "Test message"},
            }
        )
    )

    return tmp_path / ".claude"


class TestShipCommand:
    """Tests for the ship command."""

    def test_ship_help(self):
        """Ship command has help."""
        result = runner.invoke(app, ["ship", "--help"])
        assert result.exit_code == 0
        assert "One-shot" in result.output

    def test_ship_success(self, mock_projects_dir: Path):
        """Ship command succeeds."""
        mock_result = ShipResult(
            sessions_scanned=1,
            sessions_shipped=1,
            events_shipped=5,
            events_skipped=0,
            errors=[],
        )

        with patch(
            "zerg.cli.connect._ship_once",
            new_callable=AsyncMock,
        ) as mock_ship:
            mock_ship.return_value = mock_result
            result = runner.invoke(
                app,
                [
                    "ship",
                    "--claude-dir",
                    str(mock_projects_dir),
                ],
            )

        assert result.exit_code == 0
        assert "Sessions scanned: 1" in result.output
        assert "Events shipped: 5" in result.output
        assert "Done" in result.output

    def test_ship_with_errors(self, mock_projects_dir: Path):
        """Ship command reports errors."""
        mock_result = ShipResult(
            sessions_scanned=1,
            sessions_shipped=0,
            events_shipped=0,
            events_skipped=0,
            errors=["Connection refused"],
        )

        with patch(
            "zerg.cli.connect._ship_once",
            new_callable=AsyncMock,
        ) as mock_ship:
            mock_ship.return_value = mock_result
            result = runner.invoke(
                app,
                [
                    "ship",
                    "--claude-dir",
                    str(mock_projects_dir),
                ],
            )

        assert result.exit_code == 1
        assert "Errors (1)" in result.output
        assert "Connection refused" in result.output


class TestConnectCommand:
    """Tests for the connect command."""

    def test_connect_help(self):
        """Connect command has help."""
        result = runner.invoke(app, ["connect", "--help"])
        assert result.exit_code == 0
        assert "Continuous" in result.output
        assert "--interval" in result.output

    def test_connect_options(self):
        """Connect command accepts options."""
        result = runner.invoke(app, ["connect", "--help"])
        assert "--url" in result.output
        assert "--interval" in result.output
        assert "--claude-dir" in result.output


class TestMainCli:
    """Tests for the main CLI."""

    def test_help(self):
        """Main CLI shows help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "ship" in result.output
        assert "connect" in result.output

    def test_no_args(self):
        """No args shows help."""
        result = runner.invoke(app)
        assert result.exit_code == 0
        # no_args_is_help=True shows help
