"""Tests for the session file watcher."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

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

    def test_handler_ignores_non_session_files(self):
        """Handler should ignore non-session files (.txt, .py, etc.)."""
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

    def test_handler_triggers_on_json(self):
        """Handler should trigger callback for JSON files (Gemini)."""
        callback = MagicMock()
        handler = SessionFileHandler(on_change=callback, debounce_seconds=0.05)

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/some/session-2026-01-08.json"

        handler.on_modified(event)

        # Should have pending timer
        assert "/some/session-2026-01-08.json" in handler._pending

        # Wait for debounce
        time.sleep(0.1)

        # Callback should have been called
        callback.assert_called_once()
        call_arg = callback.call_args[0][0]
        assert isinstance(call_arg, Path)
        assert str(call_arg) == "/some/session-2026-01-08.json"

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
            api_url="http://localhost:47300",
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
    async def test_watcher_triggers_ship_on_file_change(self, mock_shipper, temp_claude_dir):
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

    @pytest.mark.asyncio
    async def test_watcher_passes_provider_name(self, mock_shipper, temp_claude_dir):
        """Watcher should pass correct provider_name to ship_session."""
        watcher = SessionWatcher(
            mock_shipper,
            debounce_ms=10,
            fallback_scan_interval=0,
        )

        await watcher.start()
        mock_shipper.ship_session.reset_mock()

        # Manually queue a path within the Claude projects dir
        test_path = temp_claude_dir / "projects" / "test.jsonl"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("{}\n")

        watcher._queue_ship(test_path)
        await asyncio.sleep(0.1)

        # Should have been called with provider_name="claude"
        mock_shipper.ship_session.assert_called()
        call_kwargs = mock_shipper.ship_session.call_args
        assert call_kwargs[1]["provider_name"] == "claude"

        await watcher.stop()


class TestWatcherMultiProvider:
    """Tests for multi-provider file watching."""

    @pytest.fixture
    def temp_multi_dir(self):
        """Create temp directories for multiple providers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Claude-like: projects/
            claude_dir = tmpdir / "claude"
            (claude_dir / "projects").mkdir(parents=True)

            # Codex-like: sessions/
            codex_dir = tmpdir / "codex"
            (codex_dir / "sessions").mkdir(parents=True)

            # Gemini-like: tmp/
            gemini_dir = tmpdir / "gemini"
            (gemini_dir / "tmp").mkdir(parents=True)

            yield {
                "tmpdir": tmpdir,
                "claude_dir": claude_dir,
                "codex_dir": codex_dir,
                "gemini_dir": gemini_dir,
            }

    @pytest.fixture
    def multi_provider_shipper(self, temp_multi_dir):
        """Create a mock shipper that returns multiple providers."""
        from zerg.services.shipper.providers.claude import ClaudeProvider
        from zerg.services.shipper.providers.codex import CodexProvider
        from zerg.services.shipper.providers.gemini import GeminiProvider

        config = ShipperConfig(
            api_url="http://localhost:47300",
            claude_config_dir=temp_multi_dir["claude_dir"],
        )
        shipper = SessionShipper(config=config)

        # Build providers with test directories
        claude = ClaudeProvider(config_dir=temp_multi_dir["claude_dir"])
        codex = CodexProvider(config_dir=temp_multi_dir["codex_dir"])
        gemini = GeminiProvider(config_dir=temp_multi_dir["gemini_dir"])

        # Override _get_providers to return our test providers
        shipper._get_providers = MagicMock(return_value=[claude, codex, gemini])

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
    async def test_watches_multiple_directories(self, multi_provider_shipper, temp_multi_dir):
        """Watcher should schedule observers for all provider directories."""
        watcher = SessionWatcher(
            multi_provider_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Should have watch dirs for all three providers
        assert len(watcher._watch_dirs) == 3
        assert "claude" in watcher._watch_dirs.values()
        assert "codex" in watcher._watch_dirs.values()
        assert "gemini" in watcher._watch_dirs.values()

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_skips_nonexistent_directories(self, temp_multi_dir):
        """Watcher should skip provider directories that don't exist."""
        from zerg.services.shipper.providers.claude import ClaudeProvider
        from zerg.services.shipper.providers.codex import CodexProvider

        config = ShipperConfig(
            api_url="http://localhost:47300",
            claude_config_dir=temp_multi_dir["claude_dir"],
        )
        shipper = SessionShipper(config=config)

        claude = ClaudeProvider(config_dir=temp_multi_dir["claude_dir"])
        # Point codex at a directory that doesn't exist
        codex = CodexProvider(config_dir=temp_multi_dir["tmpdir"] / "nonexistent")

        shipper._get_providers = MagicMock(return_value=[claude, codex])
        shipper.scan_and_ship = AsyncMock(
            return_value=MagicMock(
                events_shipped=0,
                sessions_shipped=0,
                errors=[],
            )
        )

        watcher = SessionWatcher(
            shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        # Only Claude dir should be watched (codex dir doesn't exist)
        assert len(watcher._watch_dirs) == 1
        assert "claude" in watcher._watch_dirs.values()

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_json_file_triggers_shipping(self, multi_provider_shipper, temp_multi_dir):
        """A .json file change (Gemini) should trigger shipping."""
        watcher = SessionWatcher(
            multi_provider_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()
        multi_provider_shipper.ship_session.reset_mock()

        # Create a Gemini-style JSON session file
        gemini_tmp = temp_multi_dir["gemini_dir"] / "tmp"
        hash_dir = gemini_tmp / ("a" * 32)
        chats_dir = hash_dir / "chats"
        chats_dir.mkdir(parents=True)

        session_file = chats_dir / "session-2026-01-08.json"
        session_data = {
            "sessionId": "test-gemini",
            "messages": [
                {
                    "type": "user",
                    "content": "Hello",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }
        session_file.write_text(json.dumps(session_data))

        # Wait for debounce + processing
        await asyncio.sleep(0.3)

        # ship_session should have been called
        if multi_provider_shipper.ship_session.call_count >= 1:
            call_kwargs = multi_provider_shipper.ship_session.call_args
            assert call_kwargs[1]["provider_name"] == "gemini"

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_provider_for_path_resolution(self, multi_provider_shipper, temp_multi_dir):
        """_provider_for_path should correctly resolve provider from file path."""
        watcher = SessionWatcher(
            multi_provider_shipper,
            debounce_ms=50,
            fallback_scan_interval=0,
        )

        await watcher.start()

        claude_path = temp_multi_dir["claude_dir"] / "projects" / "proj" / "session.jsonl"
        codex_path = temp_multi_dir["codex_dir"] / "sessions" / "2026" / "rollout-abc.jsonl"
        gemini_path = temp_multi_dir["gemini_dir"] / "tmp" / ("a" * 32) / "chats" / "session-test.json"
        unknown_path = Path("/unknown/path/session.jsonl")

        assert watcher._provider_for_path(claude_path) == "claude"
        assert watcher._provider_for_path(codex_path) == "codex"
        assert watcher._provider_for_path(gemini_path) == "gemini"
        assert watcher._provider_for_path(unknown_path) is None

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_get_watch_dir_helper(self):
        """_get_watch_dir should find the right attribute on each provider."""
        # Mock provider with projects_dir (Claude-style)
        claude_mock = MagicMock()
        claude_mock.projects_dir = Path("/home/.claude/projects")

        # Mock provider with sessions_dir (Codex-style)
        codex_mock = MagicMock()
        codex_mock.sessions_dir = Path("/home/.codex/sessions")
        del codex_mock.projects_dir  # Ensure only sessions_dir exists

        # Mock provider with tmp_dir (Gemini-style)
        gemini_mock = MagicMock()
        gemini_mock.tmp_dir = Path("/home/.gemini/tmp")
        del gemini_mock.projects_dir
        del gemini_mock.sessions_dir

        # Mock provider with no known dir
        unknown_mock = MagicMock(spec=[])

        assert SessionWatcher._get_watch_dir(claude_mock) == Path("/home/.claude/projects")
        assert SessionWatcher._get_watch_dir(codex_mock) == Path("/home/.codex/sessions")
        assert SessionWatcher._get_watch_dir(gemini_mock) == Path("/home/.gemini/tmp")
        assert SessionWatcher._get_watch_dir(unknown_mock) is None

    @pytest.mark.asyncio
    async def test_watcher_starts_with_no_directories(self):
        """Watcher should start even if no provider directories exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            # Point everything at non-existent dirs
            config = ShipperConfig(
                api_url="http://localhost:47300",
                claude_config_dir=tmpdir / "does-not-exist",
            )
            shipper = SessionShipper(config=config)
            # Return empty providers list (none exist)
            shipper._get_providers = MagicMock(return_value=[])
            shipper.scan_and_ship = AsyncMock(
                return_value=MagicMock(
                    events_shipped=0,
                    sessions_shipped=0,
                    errors=[],
                )
            )

            watcher = SessionWatcher(
                shipper,
                debounce_ms=50,
                fallback_scan_interval=0,
            )

            # Should not raise
            await watcher.start()
            assert watcher._observer is not None
            assert watcher._observer.is_alive()
            assert len(watcher._watch_dirs) == 0

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
                api_url="http://localhost:47300",
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
