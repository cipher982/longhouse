"""Tests for SQLite-backed shipper state tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zerg.services.shipper.state import ShippedSession, ShipperState


class TestShippedSession:
    """Tests for ShippedSession dataclass."""

    def test_to_dict(self):
        """Convert to dict."""
        session = ShippedSession(
            file_path="/test/session.jsonl",
            last_offset=1000,
            last_shipped_at=datetime(2026, 1, 28, 10, 0, 0, tzinfo=timezone.utc),
            session_id="zerg-abc",
            provider_session_id="claude-xyz",
        )

        d = session.to_dict()

        assert d["file_path"] == "/test/session.jsonl"
        assert d["last_offset"] == 1000
        assert d["session_id"] == "zerg-abc"
        assert d["provider_session_id"] == "claude-xyz"
        assert "2026-01-28" in d["last_shipped_at"]

    def test_from_dict(self):
        """Create from dict."""
        d = {
            "file_path": "/test/session.jsonl",
            "last_offset": 1000,
            "last_shipped_at": "2026-01-28T10:00:00+00:00",
            "session_id": "zerg-abc",
            "provider_session_id": "claude-xyz",
        }

        session = ShippedSession.from_dict(d)

        assert session.file_path == "/test/session.jsonl"
        assert session.last_offset == 1000
        assert session.session_id == "zerg-abc"


class TestShipperState:
    """Tests for SQLite-backed ShipperState."""

    def test_new_state_starts_empty(self, tmp_path: Path):
        """New state starts with no sessions."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        assert state.list_sessions() == []
        assert state.get_offset("/test/file.jsonl") == 0

    def test_set_and_get_offset(self, tmp_path: Path):
        """set_offset and get_offset work for backward compat."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file.jsonl", 1000, "zerg-abc", "claude-xyz")

        assert state.get_offset("/test/file.jsonl") == 1000
        assert state.get_offset("/other/file.jsonl") == 0

    def test_get_session(self, tmp_path: Path):
        """get_session returns ShippedSession with correct fields."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file.jsonl", 1000, "zerg-abc", "claude-xyz")

        session = state.get_session("/test/file.jsonl")
        assert session is not None
        assert session.session_id == "zerg-abc"
        assert session.provider_session_id == "claude-xyz"
        assert session.last_offset == 1000

        assert state.get_session("/other/file.jsonl") is None

    def test_dual_offsets_queued_vs_acked(self, tmp_path: Path):
        """set_queued_offset and set_acked_offset track independently."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        # Initially set via set_queued_offset
        state.set_queued_offset("/test/file.jsonl", 500, session_id="s1", provider_session_id="p1")

        assert state.get_queued_offset("/test/file.jsonl") == 500
        assert state.get_offset("/test/file.jsonl") == 0  # acked still 0

        # Ack some data
        state.set_acked_offset("/test/file.jsonl", 300)

        assert state.get_offset("/test/file.jsonl") == 300
        assert state.get_queued_offset("/test/file.jsonl") == 500

    def test_get_unacked_files(self, tmp_path: Path):
        """get_unacked_files returns files where queued > acked."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        # File with gap
        state.set_queued_offset("/test/gap.jsonl", 500)
        # File fully acked
        state.set_offset("/test/acked.jsonl", 300, "s", "p")

        unacked = state.get_unacked_files()
        assert len(unacked) == 1
        assert unacked[0][0] == "/test/gap.jsonl"
        assert unacked[0][1] == 0   # acked_offset
        assert unacked[0][2] == 500  # queued_offset

    def test_list_sessions(self, tmp_path: Path):
        """list_sessions returns all tracked sessions."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file1.jsonl", 100, "zerg-1", "claude-1")
        state.set_offset("/test/file2.jsonl", 200, "zerg-2", "claude-2")

        sessions = state.list_sessions()
        assert len(sessions) == 2

    def test_remove_session(self, tmp_path: Path):
        """remove_session removes the entry and returns True/False."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file.jsonl", 100, "zerg-1", "claude-1")
        assert len(state.list_sessions()) == 1

        result = state.remove_session("/test/file.jsonl")
        assert result is True
        assert len(state.list_sessions()) == 0

        result = state.remove_session("/nonexistent.jsonl")
        assert result is False

    def test_clear(self, tmp_path: Path):
        """clear removes all sessions."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file1.jsonl", 100, "zerg-1", "claude-1")
        state.set_offset("/test/file2.jsonl", 200, "zerg-2", "claude-2")
        assert len(state.list_sessions()) == 2

        state.clear()
        assert len(state.list_sessions()) == 0

    def test_persistence(self, tmp_path: Path):
        """State persists across close and reopen."""
        db_path = tmp_path / "shipper.db"

        # Create state and add session
        state1 = ShipperState(db_path=db_path)
        state1.set_offset("/test/file.jsonl", 1000, "zerg-abc", "claude-xyz")
        state1.close()

        # Reopen and verify
        state2 = ShipperState(db_path=db_path)
        assert state2.get_offset("/test/file.jsonl") == 1000
        state2.close()

    def test_legacy_json_migration(self, tmp_path: Path):
        """Migrates legacy zerg-shipper-state.json on init."""
        # Create legacy JSON file
        legacy_data = {
            "sessions": {
                "/test/file1.jsonl": {
                    "file_path": "/test/file1.jsonl",
                    "last_offset": 500,
                    "last_shipped_at": "2026-01-28T10:00:00+00:00",
                    "session_id": "zerg-1",
                    "provider_session_id": "claude-1",
                },
                "/test/file2.jsonl": {
                    "file_path": "/test/file2.jsonl",
                    "last_offset": 1000,
                    "last_shipped_at": "2026-01-28T11:00:00+00:00",
                    "session_id": "zerg-2",
                    "provider_session_id": "claude-2",
                },
            }
        }
        legacy_path = tmp_path / "zerg-shipper-state.json"
        legacy_path.write_text(json.dumps(legacy_data))

        # Init state — should migrate
        state = ShipperState(db_path=tmp_path / "longhouse-shipper.db")

        # Verify data migrated
        assert state.get_offset("/test/file1.jsonl") == 500
        assert state.get_offset("/test/file2.jsonl") == 1000

        session = state.get_session("/test/file1.jsonl")
        assert session is not None
        assert session.session_id == "zerg-1"

        # Legacy file should be renamed to .bak
        assert not legacy_path.exists()
        assert (tmp_path / "zerg-shipper-state.json.bak").exists()

        state.close()

    def test_corrupted_json_migration(self, tmp_path: Path):
        """Handles corrupted legacy JSON gracefully."""
        legacy_path = tmp_path / "zerg-shipper-state.json"
        legacy_path.write_text("not valid json")

        # Should not raise, just start empty
        state = ShipperState(db_path=tmp_path / "longhouse-shipper.db")
        assert state.list_sessions() == []
        state.close()

    def test_state_path_backward_compat(self, tmp_path: Path):
        """state_path parameter maps to DB in same directory."""
        state = ShipperState(state_path=tmp_path / "old-state.json")

        # Should use DB in same directory
        assert state.db_path == tmp_path / "longhouse-shipper.db"
        assert state.state_path == tmp_path / "longhouse-shipper.db"

        # Should work normally
        state.set_offset("/test/file.jsonl", 100, "s", "p")
        assert state.get_offset("/test/file.jsonl") == 100
        state.close()

    def test_claude_config_dir_parameter(self, tmp_path: Path):
        """State uses claude_config_dir when provided."""
        config_dir = tmp_path / "custom-claude"
        config_dir.mkdir()

        state = ShipperState(claude_config_dir=config_dir)
        assert state.db_path == config_dir / "longhouse-shipper.db"
        state.close()

    def test_claude_config_dir_env_var(self, tmp_path: Path, monkeypatch):
        """State uses CLAUDE_CONFIG_DIR env var when set."""
        config_dir = tmp_path / "env-claude"
        config_dir.mkdir()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        state = ShipperState()
        assert state.db_path == config_dir / "longhouse-shipper.db"
        state.close()

    def test_set_offset_updates_both_offsets(self, tmp_path: Path):
        """set_offset should update both queued_offset and acked_offset."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_offset("/test/file.jsonl", 1000, "s", "p")

        assert state.get_offset("/test/file.jsonl") == 1000
        assert state.get_queued_offset("/test/file.jsonl") == 1000
        state.close()

    def test_set_queued_offset_preserves_existing_session_ids(self, tmp_path: Path):
        """set_queued_offset with empty session_id should preserve existing values."""
        state = ShipperState(db_path=tmp_path / "shipper.db")

        state.set_queued_offset("/test/file.jsonl", 100, session_id="s1", provider_session_id="p1")
        # Update offset without session_id
        state.set_queued_offset("/test/file.jsonl", 200)

        session = state.get_session("/test/file.jsonl")
        assert session is not None
        assert session.session_id == "s1"
        assert session.provider_session_id == "p1"
        assert state.get_queued_offset("/test/file.jsonl") == 200
        state.close()
