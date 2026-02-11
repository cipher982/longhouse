"""Tests for shipper state tracking."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from zerg.services.shipper.state import ShippedSession
from zerg.services.shipper.state import ShipperState


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
    """Tests for ShipperState."""

    def test_new_state(self, tmp_path: Path):
        """New state starts empty."""
        state = ShipperState(state_path=tmp_path / "state.json")

        assert state.list_sessions() == []
        assert state.get_offset("/test/file.jsonl") == 0

    def test_set_and_get_offset(self, tmp_path: Path):
        """Set and get offset."""
        state = ShipperState(state_path=tmp_path / "state.json")

        state.set_offset(
            "/test/file.jsonl",
            1000,
            "zerg-abc",
            "claude-xyz",
        )

        assert state.get_offset("/test/file.jsonl") == 1000
        assert state.get_offset("/other/file.jsonl") == 0

    def test_get_session(self, tmp_path: Path):
        """Get session info."""
        state = ShipperState(state_path=tmp_path / "state.json")

        state.set_offset(
            "/test/file.jsonl",
            1000,
            "zerg-abc",
            "claude-xyz",
        )

        session = state.get_session("/test/file.jsonl")
        assert session is not None
        assert session.session_id == "zerg-abc"
        assert session.provider_session_id == "claude-xyz"

        assert state.get_session("/other/file.jsonl") is None

    def test_list_sessions(self, tmp_path: Path):
        """List all tracked sessions."""
        state = ShipperState(state_path=tmp_path / "state.json")

        state.set_offset("/test/file1.jsonl", 100, "zerg-1", "claude-1")
        state.set_offset("/test/file2.jsonl", 200, "zerg-2", "claude-2")

        sessions = state.list_sessions()
        assert len(sessions) == 2

    def test_remove_session(self, tmp_path: Path):
        """Remove a session from tracking."""
        state = ShipperState(state_path=tmp_path / "state.json")

        state.set_offset("/test/file.jsonl", 100, "zerg-1", "claude-1")
        assert len(state.list_sessions()) == 1

        result = state.remove_session("/test/file.jsonl")
        assert result is True
        assert len(state.list_sessions()) == 0

        result = state.remove_session("/nonexistent.jsonl")
        assert result is False

    def test_clear(self, tmp_path: Path):
        """Clear all sessions."""
        state = ShipperState(state_path=tmp_path / "state.json")

        state.set_offset("/test/file1.jsonl", 100, "zerg-1", "claude-1")
        state.set_offset("/test/file2.jsonl", 200, "zerg-2", "claude-2")
        assert len(state.list_sessions()) == 2

        state.clear()
        assert len(state.list_sessions()) == 0

    def test_persistence(self, tmp_path: Path):
        """State persists across restarts."""
        state_path = tmp_path / "state.json"

        # Create state and add session
        state1 = ShipperState(state_path=state_path)
        state1.set_offset("/test/file.jsonl", 1000, "zerg-abc", "claude-xyz")

        # Create new state instance
        state2 = ShipperState(state_path=state_path)
        assert state2.get_offset("/test/file.jsonl") == 1000

    def test_corrupted_state_file(self, tmp_path: Path):
        """Handle corrupted state file gracefully."""
        state_path = tmp_path / "state.json"
        state_path.write_text("not valid json")

        # Should not raise, just start with empty state
        state = ShipperState(state_path=state_path)
        assert state.list_sessions() == []

    def test_claude_config_dir_parameter(self, tmp_path: Path):
        """State uses claude_config_dir when provided."""
        config_dir = tmp_path / "custom-claude"
        config_dir.mkdir()

        state = ShipperState(claude_config_dir=config_dir)

        # State file should be in custom config dir
        assert state.state_path == config_dir / "zerg-shipper-state.json"

    def test_claude_config_dir_env_var(self, tmp_path: Path, monkeypatch):
        """State uses CLAUDE_CONFIG_DIR env var when set."""
        config_dir = tmp_path / "env-claude"
        config_dir.mkdir()

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        state = ShipperState()

        assert state.state_path == config_dir / "zerg-shipper-state.json"
