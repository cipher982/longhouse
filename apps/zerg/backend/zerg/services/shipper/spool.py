"""SQLite-backed offline spool for shipper resilience.

When the Zerg API is unreachable, events are queued locally and
replayed when connectivity is restored. Deduplication relies on
DB unique constraints on the server, not client-side idempotency keys.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SpooledPayload:
    """A payload waiting to be shipped."""

    id: str
    payload: dict
    created_at: datetime
    retry_count: int
    last_error: str | None


class OfflineSpool:
    """SQLite-backed queue for offline resilience.

    Events are stored locally when the API is unreachable and
    replayed when connectivity is restored.

    Usage:
        spool = OfflineSpool()

        # On API failure:
        spool.enqueue(payload)

        # Later, when online:
        for item in spool.dequeue_batch(limit=100):
            try:
                await ship(item.payload)
                spool.mark_shipped(item.id)
            except Exception as e:
                spool.mark_failed(item.id, str(e))
    """

    def __init__(self, db_path: Path | None = None, claude_config_dir: Path | None = None):
        """Initialize the spool.

        Args:
            db_path: Path to SQLite database. Defaults to {claude_config_dir}/zerg-shipper-spool.db
            claude_config_dir: Base config directory. Defaults to ~/.claude or CLAUDE_CONFIG_DIR
        """
        if db_path is None:
            if claude_config_dir is None:
                import os

                config_dir = os.getenv("CLAUDE_CONFIG_DIR")
                claude_config_dir = Path(config_dir) if config_dir else Path.home() / ".claude"
            db_path = claude_config_dir / "zerg-shipper-spool.db"

        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS spool (
                    id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    status TEXT DEFAULT 'pending'
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_spool_status
                ON spool(status, created_at)
                """
            )

            conn.commit()
        finally:
            conn.close()

    def enqueue(self, payload: dict) -> str:
        """Add a payload to the spool.

        Args:
            payload: The ingest payload to store

        Returns:
            The spool ID for tracking
        """
        spool_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO spool (id, payload_json, created_at, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (spool_id, json.dumps(payload), created_at),
            )
            conn.commit()
            logger.debug(f"Spooled payload {spool_id}")
            return spool_id
        finally:
            conn.close()

    def dequeue_batch(self, limit: int = 100) -> list[SpooledPayload]:
        """Get pending payloads for retry.

        Args:
            limit: Maximum number of items to return

        Returns:
            List of spooled payloads, oldest first
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, payload_json, created_at, retry_count, last_error
                FROM spool
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )

            results = []
            for row in cursor.fetchall():
                created_at = row[2]
                if created_at.endswith("Z"):
                    created_at = created_at[:-1] + "+00:00"

                results.append(
                    SpooledPayload(
                        id=row[0],
                        payload=json.loads(row[1]),
                        created_at=datetime.fromisoformat(created_at),
                        retry_count=row[3],
                        last_error=row[4],
                    )
                )

            return results
        finally:
            conn.close()

    def mark_shipped(self, spool_id: str) -> None:
        """Mark a payload as successfully shipped.

        Args:
            spool_id: The spool ID to mark
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE spool SET status = 'shipped' WHERE id = ?
                """,
                (spool_id,),
            )
            conn.commit()
            logger.debug(f"Marked {spool_id} as shipped")
        finally:
            conn.close()

    def mark_failed(self, spool_id: str, error: str, max_retries: int = 5) -> bool:
        """Mark a payload as failed, incrementing retry count.

        If retry_count exceeds max_retries, sets status='failed' so the
        item is no longer returned by dequeue_batch().

        Args:
            spool_id: The spool ID to mark
            error: Error message from the failure
            max_retries: After this many failures, mark as permanently failed

        Returns:
            True if item is now permanently failed, False if still retryable
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()

            # First, increment retry count
            cursor.execute(
                """
                UPDATE spool
                SET retry_count = retry_count + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (error, spool_id),
            )

            # Check if we've exceeded max retries
            cursor.execute(
                "SELECT retry_count FROM spool WHERE id = ?",
                (spool_id,),
            )
            row = cursor.fetchone()
            if row and row[0] >= max_retries:
                cursor.execute(
                    "UPDATE spool SET status = 'failed' WHERE id = ?",
                    (spool_id,),
                )
                conn.commit()
                logger.warning(f"Spool {spool_id} permanently failed after {row[0]} retries: {error}")
                return True

            conn.commit()
            logger.debug(f"Marked {spool_id} as failed (retry {row[0] if row else '?'}): {error}")
            return False
        finally:
            conn.close()

    def pending_count(self) -> int:
        """Get the count of pending payloads.

        Returns:
            Number of pending items in the spool
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM spool WHERE status = 'pending'")
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def cleanup_old(self, max_age_hours: int = 72) -> int:
        """Remove old shipped/failed entries.

        Args:
            max_age_hours: Remove entries older than this

        Returns:
            Number of entries removed
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_str = cutoff.isoformat()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM spool
                WHERE status IN ('shipped', 'failed')
                AND created_at < ?
                """,
                (cutoff_str,),
            )
            count = cursor.rowcount
            conn.commit()

            if count > 0:
                logger.info(f"Cleaned up {count} old spool entries")

            return count
        finally:
            conn.close()

    def clear(self) -> None:
        """Clear all spool entries. Use with caution."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM spool")
            conn.commit()
            logger.info("Cleared all spool entries")
        finally:
            conn.close()
