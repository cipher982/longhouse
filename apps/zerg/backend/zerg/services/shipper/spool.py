"""Pointer-based offline spool for shipper resilience.

When the Zerg API is unreachable, byte-range pointers are queued locally
and replayed when connectivity is restored. NO payload storage -- source
files are re-read on retry.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Shared DB path and connection management
_DEFAULT_DB_NAME = "longhouse-shipper.db"
MAX_QUEUE_SIZE = 10_000
BASE_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 3600  # 1 hour
MAX_RETRY_COUNT = 50
DEAD_AGE_DAYS = 7


def _get_db_path(db_path: Path | None = None, claude_config_dir: Path | None = None) -> Path:
    """Resolve DB path from explicit path, config dir, or environment."""
    if db_path is not None:
        return db_path
    if claude_config_dir is None:
        import os

        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        claude_config_dir = Path(config_dir) if config_dir else Path.home() / ".claude"
    return claude_config_dir / _DEFAULT_DB_NAME


def get_shared_connection(db_path: Path) -> sqlite3.Connection:
    """Create a WAL-mode connection for the shared shipper DB."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS file_state (
            path TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            queued_offset INTEGER NOT NULL DEFAULT 0,
            acked_offset INTEGER NOT NULL DEFAULT 0,
            session_id TEXT,
            provider_session_id TEXT,
            last_updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS spool_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            last_error TEXT,
            status TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_spool_status
        ON spool_queue(status, next_retry_at);
    """)


@dataclass
class SpoolEntry:
    """A pointer entry in the spool queue."""

    id: int
    provider: str
    file_path: str
    start_offset: int
    end_offset: int
    session_id: str | None
    created_at: datetime
    retry_count: int
    last_error: str | None


class OfflineSpool:
    """Pointer-based queue for offline resilience.

    Stores file path + byte range pointers instead of payloads.
    Source files are re-read and re-parsed on retry.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        claude_config_dir: Path | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        self.db_path = _get_db_path(db_path, claude_config_dir)
        if conn is not None:
            self._conn = conn
        else:
            self._conn = get_shared_connection(self.db_path)
            init_schema(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def enqueue(
        self,
        provider: str,
        file_path: str,
        start_offset: int,
        end_offset: int,
        session_id: str | None = None,
    ) -> bool:
        """Add a byte-range pointer to the spool.

        Returns False if queue is at capacity (backpressure).
        """
        if self.total_size() >= MAX_QUEUE_SIZE:
            logger.warning(f"Spool at capacity ({MAX_QUEUE_SIZE}), dropping enqueue for {file_path}")
            return False

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (provider, file_path, start_offset, end_offset, session_id, now, now),
        )
        self._conn.commit()
        return True

    def dequeue_batch(self, limit: int = 100) -> list[SpoolEntry]:
        """Get pending entries ready for retry."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            """SELECT id, provider, file_path, start_offset, end_offset, session_id, created_at, retry_count, last_error
               FROM spool_queue
               WHERE status = 'pending' AND next_retry_at <= ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (now, limit),
        )
        results = []
        for row in cursor.fetchall():
            created = row[6]
            if created.endswith("Z"):
                created = created[:-1] + "+00:00"
            results.append(
                SpoolEntry(
                    id=row[0],
                    provider=row[1],
                    file_path=row[2],
                    start_offset=row[3],
                    end_offset=row[4],
                    session_id=row[5],
                    created_at=datetime.fromisoformat(created),
                    retry_count=row[7],
                    last_error=row[8],
                )
            )
        return results

    def mark_shipped(self, entry_id: int) -> None:
        """Remove a successfully shipped entry."""
        self._conn.execute("DELETE FROM spool_queue WHERE id = ?", (entry_id,))
        self._conn.commit()

    def mark_failed(self, entry_id: int, error: str, max_retries: int = MAX_RETRY_COUNT) -> bool:
        """Increment retry count with exponential backoff.

        Returns True if entry is now permanently dead.
        """
        # Get current retry count
        cursor = self._conn.execute("SELECT retry_count FROM spool_queue WHERE id = ?", (entry_id,))
        row = cursor.fetchone()
        if not row:
            return True

        new_count = row[0] + 1

        if new_count >= max_retries:
            self._conn.execute(
                "UPDATE spool_queue SET status = 'dead', retry_count = ?, last_error = ? WHERE id = ?",
                (new_count, error, entry_id),
            )
            self._conn.commit()
            logger.warning(f"Spool entry {entry_id} dead after {new_count} retries: {error}")
            return True

        # Exponential backoff: min(base * 2^retry_count, max_backoff)
        backoff = min(BASE_BACKOFF_SECONDS * (2**new_count), MAX_BACKOFF_SECONDS)
        next_retry = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()

        self._conn.execute(
            "UPDATE spool_queue SET retry_count = ?, last_error = ?, next_retry_at = ? WHERE id = ?",
            (new_count, error, next_retry, entry_id),
        )
        self._conn.commit()
        return False

    def pending_count(self) -> int:
        """Count of pending entries."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM spool_queue WHERE status = 'pending'")
        return cursor.fetchone()[0]

    def total_size(self) -> int:
        """Total entries (for backpressure check)."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM spool_queue WHERE status IN ('pending', 'dead')")
        return cursor.fetchone()[0]

    def cleanup(self) -> int:
        """Remove dead entries older than 7 days and stale pending entries older than 7 days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=DEAD_AGE_DAYS)).isoformat()
        cursor = self._conn.execute(
            "DELETE FROM spool_queue WHERE (status = 'dead' AND created_at < ?) OR (status = 'pending' AND created_at < ?)",
            (cutoff, cutoff),
        )
        count = cursor.rowcount
        self._conn.commit()
        if count > 0:
            logger.info(f"Cleaned up {count} old spool entries")
        return count

    def clear(self) -> None:
        """Clear all spool entries."""
        self._conn.execute("DELETE FROM spool_queue")
        self._conn.commit()
