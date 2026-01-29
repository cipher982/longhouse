"""State tracking for the session shipper.

Tracks what has been shipped to enable incremental sync:
- file_path: Path to the session file
- last_offset: Byte offset of last shipped position
- last_shipped_at: When we last shipped
- session_id: Zerg session UUID (for resumption)

State is stored in a JSON file at ~/.claude/zerg-shipper-state.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ShippedSession:
    """Tracking info for a shipped session file."""

    file_path: str
    last_offset: int
    last_shipped_at: datetime
    session_id: str  # Zerg session UUID
    provider_session_id: str  # Claude Code session ID (filename)

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "file_path": self.file_path,
            "last_offset": self.last_offset,
            "last_shipped_at": self.last_shipped_at.isoformat(),
            "session_id": self.session_id,
            "provider_session_id": self.provider_session_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ShippedSession:
        """Create from dict."""
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
    """Tracks what sessions have been shipped (for incremental sync).

    State is persisted to a JSON file to survive restarts.
    """

    def __init__(self, state_path: Path | None = None):
        """Initialize state tracker.

        Args:
            state_path: Path to state file. Defaults to ~/.claude/zerg-shipper-state.json
        """
        if state_path is None:
            state_path = Path.home() / ".claude" / "zerg-shipper-state.json"

        self.state_path = state_path
        self._sessions: dict[str, ShippedSession] = {}
        self._load()

    def _load(self) -> None:
        """Load state from disk."""
        if not self.state_path.exists():
            return

        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)

            for file_path, session_data in data.get("sessions", {}).items():
                self._sessions[file_path] = ShippedSession.from_dict(session_data)

            logger.debug(f"Loaded shipper state with {len(self._sessions)} sessions")

        except Exception as e:
            logger.warning(f"Failed to load shipper state: {e}")

    def _save(self) -> None:
        """Save state to disk."""
        try:
            # Ensure parent directory exists
            self.state_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "sessions": {path: session.to_dict() for path, session in self._sessions.items()},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            with open(self.state_path, "w") as f:
                json.dump(data, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to save shipper state: {e}")

    def get_offset(self, file_path: str) -> int:
        """Get the last shipped offset for a file.

        Returns 0 if file hasn't been shipped before.
        """
        session = self._sessions.get(file_path)
        return session.last_offset if session else 0

    def get_session(self, file_path: str) -> ShippedSession | None:
        """Get shipped session info for a file."""
        return self._sessions.get(file_path)

    def set_offset(
        self,
        file_path: str,
        offset: int,
        session_id: str,
        provider_session_id: str,
    ) -> None:
        """Update the shipped offset for a file.

        Args:
            file_path: Path to the session file
            offset: New byte offset
            session_id: Zerg session UUID
            provider_session_id: Claude Code session ID
        """
        self._sessions[file_path] = ShippedSession(
            file_path=file_path,
            last_offset=offset,
            last_shipped_at=datetime.now(timezone.utc),
            session_id=session_id,
            provider_session_id=provider_session_id,
        )
        self._save()

    def list_sessions(self) -> list[ShippedSession]:
        """List all tracked sessions."""
        return list(self._sessions.values())

    def remove_session(self, file_path: str) -> bool:
        """Remove a session from tracking.

        Returns True if session was removed, False if not found.
        """
        if file_path in self._sessions:
            del self._sessions[file_path]
            self._save()
            return True
        return False

    def clear(self) -> None:
        """Clear all tracked sessions."""
        self._sessions.clear()
        self._save()
