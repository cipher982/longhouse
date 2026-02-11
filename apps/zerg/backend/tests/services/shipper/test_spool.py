"""Tests for the offline spool."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from zerg.services.shipper.shipper import SessionShipper
from zerg.services.shipper.shipper import ShipperConfig
from zerg.services.shipper.spool import OfflineSpool
from zerg.services.shipper.spool import SpooledPayload


class TestOfflineSpool:
    """Tests for the OfflineSpool class."""

    @pytest.fixture
    def temp_spool(self):
        """Create a spool with a temporary database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test-spool.db"
            yield OfflineSpool(db_path=db_path)

    def test_spool_init_creates_database(self, temp_spool):
        """Spool should create database and tables on init."""
        assert temp_spool.db_path.exists()

        # Verify table structure
        conn = sqlite3.connect(str(temp_spool.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spool'")
        assert cursor.fetchone() is not None
        conn.close()

    def test_enqueue_stores_payload(self, temp_spool):
        """enqueue should store payload in database."""
        payload = {"id": "test-session", "events": [{"role": "user"}]}

        spool_id = temp_spool.enqueue(payload)

        assert spool_id is not None
        assert len(spool_id) == 36  # UUID length

        # Verify it's in the database
        conn = sqlite3.connect(str(temp_spool.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT payload_json FROM spool WHERE id = ?", (spool_id,))
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert json.loads(row[0]) == payload

    def test_dequeue_batch_returns_pending(self, temp_spool):
        """dequeue_batch should return pending items in order."""
        # Enqueue several payloads
        payloads = [{"id": f"session-{i}", "events": []} for i in range(5)]
        spool_ids = [temp_spool.enqueue(p) for p in payloads]

        # Dequeue batch
        items = temp_spool.dequeue_batch(limit=3)

        assert len(items) == 3
        assert all(isinstance(item, SpooledPayload) for item in items)

        # Should be in order (oldest first)
        for i, item in enumerate(items):
            assert item.payload["id"] == f"session-{i}"
            assert item.retry_count == 0
            assert item.last_error is None

    def test_dequeue_batch_respects_limit(self, temp_spool):
        """dequeue_batch should respect the limit parameter."""
        for i in range(10):
            temp_spool.enqueue({"id": f"session-{i}"})

        items = temp_spool.dequeue_batch(limit=3)
        assert len(items) == 3

        items = temp_spool.dequeue_batch(limit=100)
        # Should return remaining 10 (all are still pending)
        assert len(items) == 10

    def test_mark_shipped_removes_from_pending(self, temp_spool):
        """mark_shipped should change status so item is not dequeued again."""
        spool_id = temp_spool.enqueue({"id": "test"})

        # Verify it's pending
        items = temp_spool.dequeue_batch()
        assert len(items) == 1

        # Mark as shipped
        temp_spool.mark_shipped(spool_id)

        # Should not appear in pending anymore
        items = temp_spool.dequeue_batch()
        assert len(items) == 0

    def test_mark_failed_increments_retry_count(self, temp_spool):
        """mark_failed should increment retry count and store error."""
        spool_id = temp_spool.enqueue({"id": "test"})

        # Mark as failed multiple times
        temp_spool.mark_failed(spool_id, "Connection refused")
        temp_spool.mark_failed(spool_id, "Timeout")

        # Should still be pending but with retry count
        items = temp_spool.dequeue_batch()
        assert len(items) == 1
        assert items[0].retry_count == 2
        assert items[0].last_error == "Timeout"

    def test_pending_count(self, temp_spool):
        """pending_count should return number of pending items."""
        assert temp_spool.pending_count() == 0

        temp_spool.enqueue({"id": "1"})
        temp_spool.enqueue({"id": "2"})
        temp_spool.enqueue({"id": "3"})

        assert temp_spool.pending_count() == 3

        # Mark one as shipped
        items = temp_spool.dequeue_batch(limit=1)
        temp_spool.mark_shipped(items[0].id)

        assert temp_spool.pending_count() == 2

    def test_cleanup_old_removes_shipped_entries(self, temp_spool):
        """cleanup_old should remove old shipped entries."""
        # Enqueue and ship
        spool_id = temp_spool.enqueue({"id": "old"})
        temp_spool.mark_shipped(spool_id)

        # Manually backdate the entry
        conn = sqlite3.connect(str(temp_spool.db_path))
        cursor = conn.cursor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        cursor.execute(
            "UPDATE spool SET created_at = ? WHERE id = ?",
            (old_time, spool_id),
        )
        conn.commit()
        conn.close()

        # Cleanup should remove it
        removed = temp_spool.cleanup_old(max_age_hours=72)
        assert removed == 1

    def test_cleanup_old_preserves_pending(self, temp_spool):
        """cleanup_old should not remove pending entries."""
        spool_id = temp_spool.enqueue({"id": "pending"})

        # Backdate it
        conn = sqlite3.connect(str(temp_spool.db_path))
        cursor = conn.cursor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        cursor.execute(
            "UPDATE spool SET created_at = ? WHERE id = ?",
            (old_time, spool_id),
        )
        conn.commit()
        conn.close()

        # Cleanup should not remove pending items
        removed = temp_spool.cleanup_old(max_age_hours=72)
        assert removed == 0
        assert temp_spool.pending_count() == 1

    def test_clear_removes_everything(self, temp_spool):
        """clear should remove all entries."""
        for i in range(5):
            temp_spool.enqueue({"id": f"session-{i}"})

        temp_spool.clear()

        assert temp_spool.pending_count() == 0

    def test_mark_failed_transitions_to_failed_status(self, temp_spool):
        """mark_failed should set status='failed' after max_retries."""
        spool_id = temp_spool.enqueue({"id": "will-fail"})

        # First 4 failures should keep status='pending'
        for i in range(4):
            result = temp_spool.mark_failed(spool_id, f"error {i}", max_retries=5)
            assert result is False  # Not permanently failed yet
            assert temp_spool.pending_count() == 1

        # 5th failure should transition to 'failed'
        result = temp_spool.mark_failed(spool_id, "final error", max_retries=5)
        assert result is True  # Now permanently failed
        assert temp_spool.pending_count() == 0  # No longer pending

        # Item should not appear in dequeue_batch
        items = temp_spool.dequeue_batch()
        assert len(items) == 0

    def test_claude_config_dir_parameter(self):
        """Spool uses claude_config_dir when provided."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "custom-claude"
            config_dir.mkdir()

            spool = OfflineSpool(claude_config_dir=config_dir)

            # DB should be in custom config dir
            assert spool.db_path == config_dir / "zerg-shipper-spool.db"

    def test_claude_config_dir_env_var(self, monkeypatch):
        """Spool uses CLAUDE_CONFIG_DIR env var when set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "env-claude"
            config_dir.mkdir()

            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

            spool = OfflineSpool()

            assert spool.db_path == config_dir / "zerg-shipper-spool.db"


class TestShipperSpoolIntegration:
    """Tests for shipper + spool integration."""

    @pytest.fixture
    def temp_env(self):
        """Create temporary environment for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create projects directory with a test session
            projects_dir = tmpdir / "projects" / "test-project"
            projects_dir.mkdir(parents=True)

            session_file = projects_dir / "test-session.jsonl"
            event_data = {
                "type": "user",
                "uuid": "user-1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "user", "content": "Hello"},
            }
            session_file.write_text(json.dumps(event_data) + "\n")

            config = ShipperConfig(
                api_url="http://localhost:47300",
                claude_config_dir=tmpdir,
            )

            spool = OfflineSpool(db_path=tmpdir / "spool.db")

            yield {
                "tmpdir": tmpdir,
                "session_file": session_file,
                "config": config,
                "spool": spool,
            }

    @pytest.mark.asyncio
    async def test_ship_spools_on_connection_error(self, temp_env):
        """ship_session should spool when API is unreachable."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        # Mock _post_ingest to raise connection error
        async def mock_post_ingest(payload):
            raise httpx.ConnectError("Connection refused")

        shipper._post_ingest = mock_post_ingest

        # Ship should not raise but should spool
        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_inserted"] == 0
        assert result["events_spooled"] == 1

        # Verify item is in spool
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_ship_spools_on_timeout(self, temp_env):
        """ship_session should spool on timeout."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            raise httpx.TimeoutException("Request timed out")

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_spooled"] == 1
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_ship_raises_on_auth_error(self, temp_env):
        """ship_session should raise on 401/403 auth errors, not spool."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 401
            raise httpx.HTTPStatusError(
                "Unauthorized",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        # Should raise, not spool
        with pytest.raises(httpx.HTTPStatusError):
            await shipper.ship_session(temp_env["session_file"])

        # Nothing should be spooled
        assert temp_env["spool"].pending_count() == 0

    @pytest.mark.asyncio
    async def test_ship_spools_on_server_error(self, temp_env):
        """ship_session should spool on 5xx server errors."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 503
            raise httpx.HTTPStatusError(
                "Service unavailable",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        assert result["events_spooled"] == 1
        assert temp_env["spool"].pending_count() == 1

    @pytest.mark.asyncio
    async def test_ship_skips_on_4xx_client_error(self, temp_env):
        """ship_session should skip (not spool) on 4xx client errors."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 400
            raise httpx.HTTPStatusError(
                "Bad request",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        result = await shipper.ship_session(temp_env["session_file"])

        # Should skip the events, not spool them
        assert result["events_skipped"] == 1
        assert result["events_spooled"] == 0
        assert temp_env["spool"].pending_count() == 0

    @pytest.mark.asyncio
    async def test_replay_spool_ships_pending(self, temp_env):
        """replay_spool should ship pending items."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        # Add items to spool directly
        temp_env["spool"].enqueue({"id": "session-1", "events": []})
        temp_env["spool"].enqueue({"id": "session-2", "events": []})

        # Mock _post_ingest to succeed
        post_calls = []

        async def mock_post_ingest(payload):
            post_calls.append(payload)
            return {"session_id": payload["id"], "events_inserted": 0, "events_skipped": 0}

        shipper._post_ingest = mock_post_ingest

        # Replay should ship both
        result = await shipper.replay_spool()

        assert result["replayed"] == 2
        assert result["failed"] == 0
        assert result["remaining"] == 0
        assert len(post_calls) == 2

    @pytest.mark.asyncio
    async def test_replay_spool_stops_on_connection_error(self, temp_env):
        """replay_spool should stop early if API is still unreachable."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        # Add items to spool
        temp_env["spool"].enqueue({"id": "session-1", "events": []})
        temp_env["spool"].enqueue({"id": "session-2", "events": []})

        async def mock_post_ingest(payload):
            raise httpx.ConnectError("Still unreachable")

        shipper._post_ingest = mock_post_ingest

        result = await shipper.replay_spool()

        # Should stop early, both items still pending
        assert result["replayed"] == 0
        assert result["remaining"] == 2

    @pytest.mark.asyncio
    async def test_replay_spool_marks_5xx_errors_failed_with_retry(self, temp_env):
        """replay_spool should mark 5xx server errors as failed with retry."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        temp_env["spool"].enqueue({"id": "server-error", "events": []})

        async def mock_post_ingest(payload):
            response = MagicMock()
            response.status_code = 500
            response.text = "Server error"
            raise httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        # First replay should mark it as failed (retry_count = 1)
        result = await shipper.replay_spool()

        assert result["failed"] == 1
        assert result["remaining"] == 1  # Still in spool, will retry

        # Verify retry count was incremented
        items = temp_env["spool"].dequeue_batch()
        assert len(items) == 1
        assert items[0].retry_count == 1

    @pytest.mark.asyncio
    async def test_replay_spool_permanently_fails_after_max_retries(self, temp_env):
        """replay_spool should permanently fail items after max retries for 5xx errors."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        # Enqueue an item
        temp_env["spool"].enqueue({"id": "will-fail-permanently", "events": []})

        call_count = 0

        async def mock_post_ingest(payload):
            nonlocal call_count
            call_count += 1
            response = MagicMock()
            response.status_code = 500
            raise httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        # Replay multiple times until item is permanently failed
        for i in range(5):
            result = await shipper.replay_spool(max_retries=5)
            assert result["failed"] == 1

        # After 5 failures, item should be permanently failed (status='failed')
        # and no longer appear in dequeue_batch
        assert temp_env["spool"].pending_count() == 0

        # One more replay should find nothing to process
        result = await shipper.replay_spool(max_retries=5)
        assert result["replayed"] == 0
        assert result["failed"] == 0
        assert result["remaining"] == 0

        # API was called 5 times (once per retry)
        assert call_count == 5

    @pytest.mark.asyncio
    async def test_replay_spool_auth_error_immediately_fails(self, temp_env):
        """replay_spool should immediately fail items on 401/403 auth errors."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        temp_env["spool"].enqueue({"id": "auth-fail", "events": []})

        call_count = 0

        async def mock_post_ingest(payload):
            nonlocal call_count
            call_count += 1
            response = MagicMock()
            response.status_code = 401
            raise httpx.HTTPStatusError(
                "Unauthorized",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        # Single replay should permanently fail the item
        result = await shipper.replay_spool(max_retries=5)
        assert result["failed"] == 1
        assert temp_env["spool"].pending_count() == 0  # Immediately removed from pending

        # API was called only once
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_replay_spool_4xx_error_immediately_fails(self, temp_env):
        """replay_spool should immediately fail items on other 4xx errors."""
        shipper = SessionShipper(
            config=temp_env["config"],
            spool=temp_env["spool"],
        )

        temp_env["spool"].enqueue({"id": "bad-payload", "events": []})

        call_count = 0

        async def mock_post_ingest(payload):
            nonlocal call_count
            call_count += 1
            response = MagicMock()
            response.status_code = 400
            raise httpx.HTTPStatusError(
                "Bad request",
                request=MagicMock(),
                response=response,
            )

        shipper._post_ingest = mock_post_ingest

        # Single replay should permanently fail the item
        result = await shipper.replay_spool(max_retries=5)
        assert result["failed"] == 1
        assert temp_env["spool"].pending_count() == 0  # Immediately removed from pending

        # API was called only once
        assert call_count == 1
