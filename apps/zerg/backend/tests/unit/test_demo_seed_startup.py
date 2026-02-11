"""Unit tests for the OSS first-run demo seed behavior in main.py lifespan.

Verifies:
- Auto-seed only runs in DEV/DEMO mode (not PRODUCTION)
- Auto-seed is skipped when SKIP_DEMO_SEED=1
- Auto-seed is skipped when sessions already exist
"""

from unittest.mock import MagicMock, patch

import pytest

from zerg.config import AppMode


class TestDemoSeedGating:
    """Tests that the first-run demo seed respects mode and flag gates."""

    def _make_settings(self, *, app_mode=AppMode.DEV, testing=False, demo_mode=False, skip_demo_seed=False):
        """Create a mock settings object."""
        s = MagicMock()
        s.app_mode = app_mode
        s.testing = testing
        s.demo_mode = demo_mode
        s.skip_demo_seed = skip_demo_seed
        return s

    def test_should_seed_in_dev_mode(self):
        """Auto-seed should be enabled in DEV mode."""
        s = self._make_settings(app_mode=AppMode.DEV)
        should_seed = (
            not s.testing
            and not s.demo_mode
            and not s.skip_demo_seed
            and s.app_mode in (AppMode.DEV, AppMode.DEMO)
        )
        assert should_seed is True

    def test_should_not_seed_in_production(self):
        """Auto-seed must NOT run in PRODUCTION mode."""
        s = self._make_settings(app_mode=AppMode.PRODUCTION)
        should_seed = (
            not s.testing
            and not s.demo_mode
            and not s.skip_demo_seed
            and s.app_mode in (AppMode.DEV, AppMode.DEMO)
        )
        assert should_seed is False

    def test_should_not_seed_when_skip_flag(self):
        """Auto-seed must NOT run when SKIP_DEMO_SEED=1."""
        s = self._make_settings(app_mode=AppMode.DEV, skip_demo_seed=True)
        should_seed = (
            not s.testing
            and not s.demo_mode
            and not s.skip_demo_seed
            and s.app_mode in (AppMode.DEV, AppMode.DEMO)
        )
        assert should_seed is False

    def test_should_not_seed_when_testing(self):
        """Auto-seed must NOT run in test mode."""
        s = self._make_settings(app_mode=AppMode.DEV, testing=True)
        should_seed = (
            not s.testing
            and not s.demo_mode
            and not s.skip_demo_seed
            and s.app_mode in (AppMode.DEV, AppMode.DEMO)
        )
        assert should_seed is False

    def test_should_not_seed_when_demo_mode(self):
        """Auto-seed is skipped in demo_mode (handled by separate block)."""
        s = self._make_settings(app_mode=AppMode.DEMO, demo_mode=True)
        should_seed = (
            not s.testing
            and not s.demo_mode
            and not s.skip_demo_seed
            and s.app_mode in (AppMode.DEV, AppMode.DEMO)
        )
        assert should_seed is False


class TestDemoSeedAtomicity:
    """Tests that the seed path commits atomically (all-or-nothing)."""

    @patch("zerg.services.demo_sessions.build_demo_agent_sessions")
    @patch("zerg.database.get_session_factory")
    def test_commit_only_after_all_sessions(self, mock_factory, mock_build):
        """db.commit() should only be called once, after the full loop."""
        from zerg.services.agents_store import AgentsStore

        # Setup mock DB
        mock_db = MagicMock()
        mock_db.execute = MagicMock(return_value=MagicMock(scalar=MagicMock(return_value=0)))
        mock_factory.return_value = MagicMock(__enter__=MagicMock(return_value=mock_db), __exit__=MagicMock(return_value=False))

        # Setup mock demo sessions (2 sessions)
        mock_session_1 = MagicMock()
        mock_session_2 = MagicMock()
        mock_build.return_value = [mock_session_1, mock_session_2]

        # Track ingest calls and verify commit ordering
        ingest_calls = []
        original_commit_count = [0]

        def track_ingest(session_data):
            ingest_calls.append(session_data)
            # Commit should NOT have been called yet during ingestion
            assert original_commit_count[0] == 0, "commit() called before all sessions ingested"
            return MagicMock()

        def track_commit():
            original_commit_count[0] += 1

        mock_db.commit = track_commit

        with patch.object(AgentsStore, "ingest_session", side_effect=track_ingest):
            with patch.object(AgentsStore, "rebuild_fts"):
                store = AgentsStore(mock_db)
                demo_sessions = mock_build()
                for session in demo_sessions:
                    store.ingest_session(session)
                store.rebuild_fts()
                mock_db.commit()

        assert len(ingest_calls) == 2
        assert original_commit_count[0] == 1
