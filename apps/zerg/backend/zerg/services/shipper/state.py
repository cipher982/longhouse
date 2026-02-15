"""SQLite-backed state tracking for the session shipper.

Tracks per-file shipping progress with dual offsets:
- queued_offset: highest byte position queued/shipped
- acked_offset: highest byte position confirmed by server

State lives in the same SQLite DB as the spool queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path

from zerg.services.shipper.spool import _get_db_path
from zerg.services.shipper.spool import get_shared_connection
from zerg.services.shipper.spool import init_schema

logger = logging.getLogger(__name__)


@dataclass
class ShippedSession:
    """Tracking info for a shipped session file."""

    file_path: str
    last_offset: int  # Maps to acked_offset for backward compat
    last_shipped_at: datetime
    session_id: str
    provider_session_id: str

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "last_offset": self.last_offset,
            "last_shipped_at": self.last_shipped_at.isoformat(),
            "session_id": self.session_id,
            "provider_session_id": self.provider_session_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShippedSession:
        last_shipped = data.get("last_shipped_at", "")
        if isinstance(last_shipped, str):
            if last_shipped.endswith("Z"):
                last_shipped = last_shipped[:-1] + "+00:00"
            try:
                last_shipped_dt = datetime.fromisoformat(last_shipped)
            except ValueError:
                last_shipped_dt = datetime.now(timezone.utc)
        else:
            last_shipped_dt = datetime.now(timezone.utc)
        return cls(
            file_path=data.get("file_path", ""),
            last_offset=data.get("last_offset", 0),
            last_shipped_at=last_shipped_dt,
            session_id=data.get("session_id", ""),
            provider_session_id=data.get("provider_session_id", ""),
        )


class ShipperState:
    """SQLite-backed state tracking for incremental session shipping."""

    def __init__(
        self,
        state_path: Path | None = None,  # Kept for backward compat signature
        claude_config_dir: Path | None = None,
        db_path: Path | None = None,
        conn=None,  # sqlite3.Connection - shared with spool
    ):
        # Resolve DB path
        if db_path is not None:
            self.db_path = db_path
        elif state_path is not None:
            # Backward compat: caller passed old JSON state_path
            # Use same directory but new DB name
            self.db_path = state_path.parent / "longhouse-shipper.db"
        else:
            self.db_path = _get_db_path(claude_config_dir=claude_config_dir)

        # For backward compatibility: expose state_path pointing to the DB
        self.state_path = self.db_path

        if conn is not None:
            self._conn = conn
        else:
            self._conn = get_shared_connection(self.db_path)
            init_schema(self._conn)

        # Migrate legacy JSON state if it exists
        self._migrate_json_state()

    def _migrate_json_state(self) -> None:
        """Import legacy zerg-shipper-state.json if it exists."""
        legacy_path = self.db_path.parent / "zerg-shipper-state.json"
        if not legacy_path.exists():
            return

        try:
            with open(legacy_path) as f:
                data = json.load(f)

            sessions = data.get("sessions", {})
            if not sessions:
                legacy_path.rename(legacy_path.with_suffix(".json.bak"))
                return

            now = datetime.now(timezone.utc).isoformat()
            for file_path, session_data in sessions.items():
                offset = session_data.get("last_offset", 0)
                self._conn.execute(
                    """INSERT OR IGNORE INTO file_state (path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated)
                       VALUES (?, 'claude', ?, ?, ?, ?, ?)""",
                    (
                        file_path,
                        offset,
                        offset,
                        session_data.get("session_id", ""),
                        session_data.get("provider_session_id", ""),
                        now,
                    ),
                )
            self._conn.commit()
            legacy_path.rename(legacy_path.with_suffix(".json.bak"))
            logger.info(f"Migrated {len(sessions)} entries from legacy JSON state")
        except Exception as e:
            logger.warning(f"Failed to migrate legacy state: {e}")

    @property
    def conn(self):
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def get_offset(self, file_path: str) -> int:
        """Get the acked offset for a file. Returns 0 if not tracked."""
        cursor = self._conn.execute("SELECT acked_offset FROM file_state WHERE path = ?", (file_path,))
        row = cursor.fetchone()
        return row[0] if row else 0

    def get_queued_offset(self, file_path: str) -> int:
        """Get the queued offset for a file. Returns 0 if not tracked."""
        cursor = self._conn.execute("SELECT queued_offset FROM file_state WHERE path = ?", (file_path,))
        row = cursor.fetchone()
        return row[0] if row else 0

    def set_offset(
        self,
        file_path: str,
        offset: int,
        session_id: str,
        provider_session_id: str,
        provider: str = "claude",
    ) -> None:
        """Update both queued and acked offsets (for successful ship)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO file_state (path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   queued_offset = excluded.queued_offset,
                   acked_offset = excluded.acked_offset,
                   session_id = excluded.session_id,
                   provider_session_id = excluded.provider_session_id,
                   last_updated = excluded.last_updated""",
            (file_path, provider, offset, offset, session_id, provider_session_id, now),
        )
        self._conn.commit()

    def set_queued_offset(
        self,
        file_path: str,
        offset: int,
        provider: str = "claude",
        session_id: str = "",
        provider_session_id: str = "",
    ) -> None:
        """Advance only the queued offset (data enqueued to spool but not yet acked)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO file_state (path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated)
               VALUES (?, ?, ?, 0, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   queued_offset = excluded.queued_offset,
                   session_id = CASE WHEN excluded.session_id != '' THEN excluded.session_id ELSE file_state.session_id END,
                   provider_session_id = CASE WHEN excluded.provider_session_id != '' THEN excluded.provider_session_id ELSE file_state.provider_session_id END,
                   last_updated = excluded.last_updated""",
            (file_path, provider, offset, session_id, provider_session_id, now),
        )
        self._conn.commit()

    def set_acked_offset(self, file_path: str, offset: int) -> None:
        """Advance only the acked offset (server confirmed receipt)."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE file_state SET acked_offset = ?, last_updated = ? WHERE path = ?",
            (offset, now, file_path),
        )
        self._conn.commit()

    def get_session(self, file_path: str) -> ShippedSession | None:
        """Get shipped session info for a file."""
        cursor = self._conn.execute(
            "SELECT path, acked_offset, session_id, provider_session_id, last_updated FROM file_state WHERE path = ?",
            (file_path,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        last_updated = row[4]
        if last_updated.endswith("Z"):
            last_updated = last_updated[:-1] + "+00:00"
        return ShippedSession(
            file_path=row[0],
            last_offset=row[1],
            last_shipped_at=datetime.fromisoformat(last_updated),
            session_id=row[2] or "",
            provider_session_id=row[3] or "",
        )

    def get_unacked_files(self) -> list[tuple[str, int, int]]:
        """Get files where queued_offset > acked_offset (need recovery).

        Returns list of (path, acked_offset, queued_offset).
        """
        cursor = self._conn.execute("SELECT path, acked_offset, queued_offset FROM file_state WHERE queued_offset > acked_offset")
        return [(row[0], row[1], row[2]) for row in cursor.fetchall()]

    def list_sessions(self) -> list[ShippedSession]:
        """List all tracked sessions."""
        cursor = self._conn.execute("SELECT path, acked_offset, session_id, provider_session_id, last_updated FROM file_state")
        results = []
        for row in cursor.fetchall():
            last_updated = row[4]
            if last_updated.endswith("Z"):
                last_updated = last_updated[:-1] + "+00:00"
            results.append(
                ShippedSession(
                    file_path=row[0],
                    last_offset=row[1],
                    last_shipped_at=datetime.fromisoformat(last_updated),
                    session_id=row[2] or "",
                    provider_session_id=row[3] or "",
                )
            )
        return results

    def remove_session(self, file_path: str) -> bool:
        """Remove a session from tracking."""
        cursor = self._conn.execute("DELETE FROM file_state WHERE path = ?", (file_path,))
        self._conn.commit()
        return cursor.rowcount > 0

    def clear(self) -> None:
        """Clear all tracked sessions."""
        self._conn.execute("DELETE FROM file_state")
        self._conn.commit()
