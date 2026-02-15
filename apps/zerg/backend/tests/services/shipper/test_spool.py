"""Tests for the pointer-based offline spool."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zerg.services.shipper.spool import (
    MAX_QUEUE_SIZE,
    OfflineSpool,
    SpoolEntry,
    init_schema,
)


class TestOfflineSpool:
    """Tests for the pointer-based OfflineSpool."""

    @pytest.fixture
    def spool(self, tmp_path: Path) -> OfflineSpool:
        """Create a spool with a temporary database."""
        db_path = tmp_path / "test-spool.db"
        return OfflineSpool(db_path=db_path)

    def test_init_creates_database_with_both_tables(self, spool: OfflineSpool):
        """Spool should create database with both spool_queue and file_state tables."""
        assert spool.db_path.exists()

        conn = sqlite3.connect(str(spool.db_path))
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='spool_queue'")
        assert cursor.fetchone() is not None

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_state'")
        assert cursor.fetchone() is not None

        conn.close()

    def test_enqueue_stores_pointer(self, spool: OfflineSpool):
        """enqueue should store a byte-range pointer, not a payload."""
        result = spool.enqueue("claude", "/tmp/session.jsonl", 0, 1024, "sess-1")

        assert result is True

        # Verify pointer is stored in DB
        cursor = spool.conn.execute(
            "SELECT provider, file_path, start_offset, end_offset, session_id FROM spool_queue"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row == ("claude", "/tmp/session.jsonl", 0, 1024, "sess-1")

    def test_enqueue_returns_false_at_capacity(self, tmp_path: Path):
        """enqueue should return False when queue is at MAX_QUEUE_SIZE."""
        db_path = tmp_path / "cap-test.db"
        spool = OfflineSpool(db_path=db_path)

        # Insert MAX_QUEUE_SIZE rows directly to simulate full queue
        now = datetime.now(timezone.utc).isoformat()
        spool.conn.executemany(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            [("claude", f"/tmp/f{i}.jsonl", 0, 100, None, now, now) for i in range(MAX_QUEUE_SIZE)],
        )
        spool.conn.commit()

        assert spool.total_size() == MAX_QUEUE_SIZE

        # Next enqueue should fail
        result = spool.enqueue("claude", "/tmp/overflow.jsonl", 0, 100)
        assert result is False

    def test_dequeue_batch_returns_ready_entries(self, spool: OfflineSpool):
        """dequeue_batch should return entries whose next_retry_at <= now."""
        spool.enqueue("claude", "/tmp/f1.jsonl", 0, 100, "s1")
        spool.enqueue("claude", "/tmp/f2.jsonl", 100, 200, "s2")
        spool.enqueue("claude", "/tmp/f3.jsonl", 200, 300, "s3")

        entries = spool.dequeue_batch(limit=2)

        assert len(entries) == 2
        assert all(isinstance(e, SpoolEntry) for e in entries)
        assert entries[0].file_path == "/tmp/f1.jsonl"
        assert entries[0].start_offset == 0
        assert entries[0].end_offset == 100
        assert entries[1].file_path == "/tmp/f2.jsonl"

    def test_dequeue_batch_respects_next_retry_at(self, spool: OfflineSpool):
        """dequeue_batch should not return entries with future next_retry_at."""
        spool.enqueue("claude", "/tmp/ready.jsonl", 0, 100)

        # Insert an entry with future next_retry_at
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        spool.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            ("claude", "/tmp/future.jsonl", 0, 100, datetime.now(timezone.utc).isoformat(), future),
        )
        spool.conn.commit()

        entries = spool.dequeue_batch()
        assert len(entries) == 1
        assert entries[0].file_path == "/tmp/ready.jsonl"

    def test_mark_shipped_deletes_row(self, spool: OfflineSpool):
        """mark_shipped should delete the entry from the DB."""
        spool.enqueue("claude", "/tmp/f.jsonl", 0, 100)
        entries = spool.dequeue_batch()
        assert len(entries) == 1

        spool.mark_shipped(entries[0].id)

        # Entry should be gone
        entries = spool.dequeue_batch()
        assert len(entries) == 0
        assert spool.pending_count() == 0

        # Verify row is actually deleted
        cursor = spool.conn.execute("SELECT COUNT(*) FROM spool_queue")
        assert cursor.fetchone()[0] == 0

    def test_mark_failed_increments_retry_with_backoff(self, spool: OfflineSpool):
        """mark_failed should increment retry count and set future next_retry_at."""
        spool.enqueue("claude", "/tmp/f.jsonl", 0, 100)
        entries = spool.dequeue_batch()
        entry_id = entries[0].id

        is_dead = spool.mark_failed(entry_id, "Connection refused")
        assert is_dead is False

        # Check retry count incremented
        cursor = spool.conn.execute(
            "SELECT retry_count, last_error, next_retry_at FROM spool_queue WHERE id = ?", (entry_id,)
        )
        row = cursor.fetchone()
        assert row[0] == 1
        assert row[1] == "Connection refused"
        # next_retry_at should be in the future
        next_retry = datetime.fromisoformat(row[2])
        assert next_retry > datetime.now(timezone.utc)

    def test_mark_failed_transitions_to_dead(self, spool: OfflineSpool):
        """mark_failed should set status='dead' after max retries."""
        spool.enqueue("claude", "/tmp/f.jsonl", 0, 100)
        entries = spool.dequeue_batch()
        entry_id = entries[0].id

        # Fail 4 times with max_retries=5
        for i in range(4):
            is_dead = spool.mark_failed(entry_id, f"error {i}", max_retries=5)
            assert is_dead is False

        # 5th failure should mark as dead
        is_dead = spool.mark_failed(entry_id, "final error", max_retries=5)
        assert is_dead is True

        # Should not appear in pending
        assert spool.pending_count() == 0
        entries = spool.dequeue_batch()
        assert len(entries) == 0

        # But should still exist as dead
        cursor = spool.conn.execute("SELECT status FROM spool_queue WHERE id = ?", (entry_id,))
        row = cursor.fetchone()
        assert row[0] == "dead"

    def test_pending_count(self, spool: OfflineSpool):
        """pending_count should return number of pending entries."""
        assert spool.pending_count() == 0

        spool.enqueue("claude", "/tmp/f1.jsonl", 0, 100)
        spool.enqueue("claude", "/tmp/f2.jsonl", 100, 200)
        spool.enqueue("claude", "/tmp/f3.jsonl", 200, 300)

        assert spool.pending_count() == 3

        # Ship one
        entries = spool.dequeue_batch(limit=1)
        spool.mark_shipped(entries[0].id)

        assert spool.pending_count() == 2

    def test_total_size_includes_dead(self, spool: OfflineSpool):
        """total_size should count both pending and dead entries."""
        spool.enqueue("claude", "/tmp/f1.jsonl", 0, 100)
        spool.enqueue("claude", "/tmp/f2.jsonl", 100, 200)

        # Kill one
        entries = spool.dequeue_batch(limit=1)
        spool.mark_failed(entries[0].id, "dead", max_retries=1)

        assert spool.pending_count() == 1
        assert spool.total_size() == 2  # 1 pending + 1 dead

    def test_cleanup_removes_old_dead_only(self, spool: OfflineSpool):
        """cleanup should remove only dead entries older than DEAD_AGE_DAYS, not pending."""
        # Insert old entries directly
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        spool.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, 'dead')",
            ("claude", "/tmp/old-dead.jsonl", 0, 100, old_time, now),
        )
        spool.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            ("claude", "/tmp/old-pending.jsonl", 0, 100, old_time, now),
        )
        # Recent pending should survive
        spool.enqueue("claude", "/tmp/recent.jsonl", 0, 100)
        spool.conn.commit()

        removed = spool.cleanup()
        assert removed == 1  # Only the old dead entry

        # Both pending entries should remain (old + recent)
        assert spool.pending_count() == 2

    def test_cleanup_preserves_recent_entries(self, spool: OfflineSpool):
        """cleanup should not remove recent entries."""
        spool.enqueue("claude", "/tmp/recent.jsonl", 0, 100)

        removed = spool.cleanup()
        assert removed == 0
        assert spool.pending_count() == 1

    def test_clear_removes_everything(self, spool: OfflineSpool):
        """clear should remove all spool entries."""
        for i in range(5):
            spool.enqueue("claude", f"/tmp/f{i}.jsonl", 0, 100)

        spool.clear()

        assert spool.pending_count() == 0
        assert spool.total_size() == 0

    def test_enqueue_without_session_id(self, spool: OfflineSpool):
        """enqueue should work without session_id."""
        result = spool.enqueue("claude", "/tmp/f.jsonl", 0, 100)
        assert result is True

        entries = spool.dequeue_batch()
        assert len(entries) == 1
        assert entries[0].session_id is None

    def test_claude_config_dir_parameter(self, tmp_path: Path):
        """Spool uses claude_config_dir when provided."""
        config_dir = tmp_path / "custom-claude"
        config_dir.mkdir()

        spool = OfflineSpool(claude_config_dir=config_dir)
        assert spool.db_path == config_dir / "longhouse-shipper.db"

    def test_claude_config_dir_env_var(self, tmp_path: Path, monkeypatch):
        """Spool uses CLAUDE_CONFIG_DIR env var when set."""
        config_dir = tmp_path / "env-claude"
        config_dir.mkdir()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        spool = OfflineSpool()
        assert spool.db_path == config_dir / "longhouse-shipper.db"

    def test_cleanup_preserves_old_pending_entries(self, spool: OfflineSpool):
        """Bug 5: cleanup must NOT delete pending entries, even after 7+ days."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        now = datetime.now(timezone.utc).isoformat()

        # Insert old pending entry
        spool.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            ("claude", "/tmp/old-pending.jsonl", 0, 100, old_time, now),
        )
        spool.conn.commit()

        removed = spool.cleanup()
        assert removed == 0  # Nothing should be removed

        # Old pending entry should survive
        assert spool.pending_count() == 1

    def test_shared_connection(self, tmp_path: Path):
        """Spool accepts an external connection."""
        from zerg.services.shipper.spool import get_shared_connection

        db_path = tmp_path / "shared.db"
        conn = get_shared_connection(db_path)
        init_schema(conn)

        spool = OfflineSpool(db_path=db_path, conn=conn)
        spool.enqueue("claude", "/tmp/f.jsonl", 0, 100)
        assert spool.pending_count() == 1

        conn.close()
