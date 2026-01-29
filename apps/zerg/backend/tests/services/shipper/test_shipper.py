"""Tests for the session shipper."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper.state import ShipperState


@pytest.fixture
def mock_projects_dir(tmp_path: Path) -> Path:
    """Create a mock projects directory structure."""
    projects_dir = tmp_path / ".claude" / "projects"
    projects_dir.mkdir(parents=True)

    # Create a project directory
    project_dir = projects_dir / "-Users-test-project"
    project_dir.mkdir()

    # Create a session file
    session_file = project_dir / "test-session-123.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "msg-1",
                "timestamp": "2026-01-28T10:00:00Z",
                "cwd": "/Users/test/project",
                "gitBranch": "main",
                "message": {"content": "Hello Claude"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "msg-2",
                "timestamp": "2026-01-28T10:01:00Z",
                "message": {
                    "content": [{"type": "text", "text": "Hello! How can I help?"}],
                },
            }
        )
    )

    return tmp_path / ".claude"


@pytest.fixture
def shipper_config(mock_projects_dir: Path) -> ShipperConfig:
    """Create shipper config pointing to mock directory."""
    return ShipperConfig(
        zerg_api_url="http://test:47300",
        claude_config_dir=mock_projects_dir,
    )


@pytest.fixture
def shipper_state(tmp_path: Path) -> ShipperState:
    """Create shipper state with temp file."""
    return ShipperState(state_path=tmp_path / "shipper-state.json")


class TestShipperConfig:
    """Tests for ShipperConfig."""

    def test_default_config(self):
        """Default config uses ~/.claude."""
        config = ShipperConfig()
        assert config.zerg_api_url == "http://localhost:47300"
        assert config.scan_interval_seconds == 30
        assert config.batch_size == 100

    def test_projects_dir(self, mock_projects_dir: Path):
        """Projects dir is derived from claude_config_dir."""
        config = ShipperConfig(claude_config_dir=mock_projects_dir)
        assert config.projects_dir == mock_projects_dir / "projects"


class TestSessionShipper:
    """Tests for SessionShipper."""

    def test_find_session_files(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Find session files in projects directory."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        assert len(files) == 1
        assert files[0].name == "test-session-123.jsonl"

    def test_has_new_content(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Check if file has new content."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        # New file should have new content
        assert shipper._has_new_content(files[0]) is True

        # After setting offset to file size, no new content
        shipper_state.set_offset(
            str(files[0]),
            files[0].stat().st_size,
            "session-id",
            "test-session-123",
        )
        assert shipper._has_new_content(files[0]) is False

    @pytest.mark.asyncio
    async def test_ship_session(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Ship a session to Zerg API."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        # Mock the API call
        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await shipper.ship_session(files[0])

        assert result["events_inserted"] == 2
        assert result["events_skipped"] == 0

        # Check that payload was correct
        call_args = mock_post.call_args[0][0]
        assert call_args["provider"] == "claude"
        assert call_args["cwd"] == "/Users/test/project"
        assert call_args["git_branch"] == "main"
        assert len(call_args["events"]) == 2

    @pytest.mark.asyncio
    async def test_scan_and_ship(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Scan and ship all sessions."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)

        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            result = await shipper.scan_and_ship()

        assert result.sessions_scanned == 1
        assert result.sessions_shipped == 1
        assert result.events_shipped == 2
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_incremental_ship(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Second ship only sends new events."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)
        files = shipper._find_session_files()

        mock_response = {
            "session_id": "zerg-session-abc",
            "events_inserted": 2,
            "events_skipped": 0,
            "session_created": True,
        }

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # First ship
            result1 = await shipper.scan_and_ship()
            assert result1.events_shipped == 2

            # Second ship - no new content
            result2 = await shipper.scan_and_ship()
            assert result2.sessions_shipped == 0
            assert result2.events_shipped == 0

        # Add new content
        with open(files[0], "a") as f:
            f.write(
                "\n"
                + json.dumps(
                    {
                        "type": "user",
                        "uuid": "msg-3",
                        "timestamp": "2026-01-28T10:02:00Z",
                        "message": {"content": "New message"},
                    }
                )
            )

        mock_response["events_inserted"] = 1

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Third ship - only new event
            result3 = await shipper.scan_and_ship()
            assert result3.sessions_shipped == 1
            assert result3.events_shipped == 1

    @pytest.mark.asyncio
    async def test_ship_error_handling(
        self,
        mock_projects_dir: Path,
        shipper_config: ShipperConfig,
        shipper_state: ShipperState,
    ):
        """Handle API errors gracefully."""
        shipper = SessionShipper(config=shipper_config, state=shipper_state)

        with patch.object(shipper, "_post_ingest", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = Exception("Connection refused")
            result = await shipper.scan_and_ship()

        assert result.sessions_scanned == 1
        assert result.sessions_shipped == 0
        assert len(result.errors) == 1
        assert "Connection refused" in result.errors[0]
