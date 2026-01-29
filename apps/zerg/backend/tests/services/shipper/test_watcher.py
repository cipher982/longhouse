"""Tests for the session file watcher."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.shipper.shipper import SessionShipper
from zerg.services.shipper.shipper import ShipperConfig
from zerg.services.shipper.watcher import SessionFileHandler
from zerg.services.shipper.watcher import SessionWatcher


class TestSessionFileHandler:
    """Tests for the SessionFileHandler class."""

    def test_handler_ignores_directories(self):
        """Handler should ignore directory events."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=0.01)

        event = MagicMock()
        event.is_directory = True
        event.src_path = "/some/dir"

        handler.on_modified(event)

        # No debounce timer should be set
        assert len(handler._pending) == 0

    def test_handler_ignores_non_jsonl_files(self):
        """Handler should ignore non-JSONL files."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=0.01)

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/some/file.txt"

        handler.on_modified(event)

        # No debounce timer should be set
        assert len(handler._pending) == 0

    def test_handler_triggers_on_jsonl(self):
        """Handler should trigger callback for JSONL files after debounce."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=0.05)

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/some/session.jsonl"

        handler.on_modified(event)

        # Should have pending timer
        assert "/some/session.jsonl" in handler._pending

        # Wait for debounce
        time.sleep(0.1)

        # Callback should have been called
        callback.assert_called_once()
        call_arg = callback.call_args[0][0]
        assert isinstance(call_arg, Path)
        assert str(call_arg) == "/some/session.jsonl"

        handler.cancel_all()

    def test_handler_debounces_rapid_writes(self):
        """Handler should coalesce rapid writes into single callback."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=0.2)

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/some/session.jsonl"

        # Simulate rapid writes
        for _ in range(5):
            handler.on_modified(event)
            time.sleep(0.02)

        # Wait for debounce to complete
        time.sleep(0.3)

        # Callback should have been called only once (coalesced)
        assert callback.call_count == 1

        handler.cancel_all()

    def test_handler_cancel_all(self):
        """cancel_all should clear pending timers."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=1.0)

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/some/session.jsonl"

        handler.on_modified(event)
        assert len(handler._pending) == 1

        handler.cancel_all()
        assert len(handler._pending) == 0

        # Wait and verify callback wasn't called
        time.sleep(0.1)
        callback.assert_not_called()


class TestSessionWatcher:
    """Tests for the SessionWatcher class."""

    @pytest.fixture
    def temp_claude_dir(self):
        """Create a temporary Claude config directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir(parents=True)
            yield Path(tmpdir)

    @pytest.fixture
    def mock_shipper(self, temp_claude_dir):
        """Create a mock shipper with temp directory."""
        config = ShipperConfig(
            zerg_api_url="http://localhost:47300",
            claude_config_dir=temp_claude_dir,
        )
        shipper = SessionShipper(config=config)
        shipper.scan_and_ship = AsyncMock(
            return_value=MagicMock(
                events_shipped=0,
                sessions_shipped=0,
                errors=[],
            )
        )
        shipper.ship_session = AsyncMock(
            return_value={
                "events_inserted": 1,
                "events_skipped": 0,
                "events_spooled": 0,
            }
        )
        return shipper

    @pytest.mark.asyncio
    async def test_watcher_starts_and_stops(self, mock_shipper, temp_claude_dir):
        """Watcher should start and stop cleanly."""
        watcher = SessionWatcher(
            mock_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,  # Disable fallback for test
        )

        await watcher.start()

        # Verify observer is running
        assert watcher._observer is not None
        assert watcher._observer.is_alive()

        await watcher.stop()

        # Verify observer is stopped
        assert not watcher._observer.is_alive()

    @pytest.mark.asyncio
    async def test_watcher_runs_initial_scan(self, mock_shipper, temp_claude_dir):
        """Watcher should run initial scan on start."""
        watcher = SessionWatcher(
            mock_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Initial scan should have been called
        mock_shipper.scan_and_ship.assert_called_once()

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watcher_triggers_ship_on_file_change(
        self, mock_shipper, temp_claude_dir
    ):
        """Watcher should trigger ship when a file changes."""
        watcher = SessionWatcher(
            mock_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Reset mock after initial scan
        mock_shipper.scan_and_ship.reset_mock()
        mock_shipper.ship_session.reset_mock()

        # Create a test session file
        projects_dir = temp_claude_dir / "projects"
        test_session = projects_dir / "test-project" / "session.jsonl"
        test_session.parent.mkdir(parents=True, exist_ok=True)

        # Write initial content
        event_data = {
            "type": "user",
            "uuid": "test-uuid",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"role": "user", "content": "Hello"},
        }
        test_session.write_text(json.dumps(event_data) + "\n")

        # Wait for debounce + processing
        await asyncio.sleep(0.2)

        # ship_session should have been called
        assert mock_shipper.ship_session.call_count >= 1

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_watcher_queues_ship_from_thread(self, mock_shipper, temp_claude_dir):
        """Watcher should properly queue ships from watchdog thread."""
        watcher = SessionWatcher(
            mock_shipper,
            debounce_ms=10,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Manually queue a path (simulating watchdog callback)
        test_path = temp_claude_dir / "projects" / "test.jsonl"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("{}\n")

        watcher._queue_ship(test_path)

        # Wait for processing
        await asyncio.sleep(0.1)

        # ship_session should have been called with the path
        mock_shipper.ship_session.assert_called()

        await watcher.stop()


class TestWatcherIntegration:
    """Integration tests for the watcher with real file system events."""

    @pytest.fixture
    def temp_env(self):
        """Create a complete temporary environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            projects_dir = tmpdir / "projects"
            projects_dir.mkdir()

            config = ShipperConfig(
                zerg_api_url="http://localhost:47300",
                claude_config_dir=tmpdir,
            )

            yield {
                "tmpdir": tmpdir,
                "projects_dir": projects_dir,
                "config": config,
            }

    @pytest.mark.asyncio
    async def test_end_to_end_file_watch(self, temp_env):
        """Test end-to-end file watching with real FS events."""
        config = temp_env["config"]
        projects_dir = temp_env["projects_dir"]

        # Create shipper with mocked API call
        shipper = SessionShipper(config=config)

        ship_calls = []

        async def mock_post_ingest(payload):
            ship_calls.append(payload)
            return {"session_id": "test", "events_inserted": 1, "events_skipped": 0}

        shipper._post_ingest = mock_post_ingest

        watcher = SessionWatcher(
            shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Give the observer time to start
        await asyncio.sleep(0.1)

        # Create a session file
        session_dir = projects_dir / "test-project"
        session_dir.mkdir()
        session_file = session_dir / "abc123.jsonl"

        event_data = {
            "type": "user",
            "uuid": "user-1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"role": "user", "content": "Hello world"},
        }

        session_file.write_text(json.dumps(event_data) + "\n")

        # Wait for debounce + processing
        await asyncio.sleep(0.3)

        # Verify the ship was triggered
        # Note: might be 2 calls - one from initial scan if file was created fast enough
        assert len(ship_calls) >= 1

        await watcher.stop()
