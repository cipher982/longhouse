"""Unit tests for auto-seeding service.

Tests the automatic seeding of user context and credentials on startup.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

from zerg.services.auto_seed import (
    _find_config_file,
    _seed_user_context,
    run_auto_seed,
    USER_CONTEXT_PATHS,
)


class TestFindConfigFile:
    """Tests for _find_config_file helper."""

    def test_finds_first_existing_file(self, tmp_path):
        """Returns first file that exists from the list."""
        # Create second file only
        file2 = tmp_path / "second.json"
        file2.write_text("{}")

        paths = [
            tmp_path / "first.json",  # doesn't exist
            file2,  # exists
            tmp_path / "third.json",  # doesn't exist
        ]

        result = _find_config_file(paths)
        assert result == file2

    def test_returns_none_when_no_files_exist(self, tmp_path):
        """Returns None when no files exist."""
        paths = [
            tmp_path / "missing1.json",
            tmp_path / "missing2.json",
        ]

        result = _find_config_file(paths)
        assert result is None

    def test_returns_first_when_multiple_exist(self, tmp_path):
        """Returns first file when multiple exist."""
        file1 = tmp_path / "first.json"
        file2 = tmp_path / "second.json"
        file1.write_text("{}")
        file2.write_text("{}")

        paths = [file1, file2]

        result = _find_config_file(paths)
        assert result == file1


class TestSeedUserContext:
    """Tests for _seed_user_context function."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = MagicMock()
        return db

    @pytest.fixture
    def mock_user(self):
        """Create a mock user with empty context."""
        user = MagicMock()
        user.id = 1
        user.email = "test@example.com"
        user.context = {}
        return user

    @pytest.fixture
    def sample_context(self):
        """Sample user context for testing."""
        return {
            "display_name": "Test User",
            "servers": [
                {"name": "server1", "ip": "10.0.0.1", "purpose": "Testing"},
                {"name": "server2", "ip": "10.0.0.2", "purpose": "Dev"},
            ],
            "integrations": {"notes": "Obsidian"},
            "custom_instructions": "Be concise",
        }

    def test_skips_when_no_config_file(self):
        """Returns True (success) when no config file exists."""
        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=None
        ):
            result = _seed_user_context()
            assert result is True

    def test_skips_when_no_users_exist(self, tmp_path, sample_context):
        """Returns True when database has no users."""
        config_file = tmp_path / "context.json"
        config_file.write_text(json.dumps(sample_context))

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = []
        mock_db.query.return_value = mock_query

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            result = _seed_user_context()
            assert result is True
            mock_db.close.assert_called_once()

    def test_skips_when_user_has_context(self, tmp_path, mock_user, sample_context):
        """Returns True and doesn't overwrite when user already has context."""
        config_file = tmp_path / "context.json"
        config_file.write_text(json.dumps(sample_context))

        # User already has context with display_name
        mock_user.context = {"display_name": "Existing User"}

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_user]
        mock_db.query.return_value = mock_query

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            result = _seed_user_context()
            assert result is True
            # Should NOT have updated the context
            assert mock_user.context == {"display_name": "Existing User"}
            mock_db.commit.assert_not_called()

    def test_seeds_context_for_new_user(self, tmp_path, mock_user, sample_context):
        """Seeds context when user has no existing context."""
        config_file = tmp_path / "context.json"
        config_file.write_text(json.dumps(sample_context))

        # User has empty context
        mock_user.context = {}

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_user]
        mock_db.query.return_value = mock_query

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            result = _seed_user_context()
            assert result is True
            # Should have set the context
            assert mock_user.context == sample_context
            mock_db.commit.assert_called_once()
            mock_db.close.assert_called_once()

    def test_seeds_context_when_none(self, tmp_path, mock_user, sample_context):
        """Seeds context when user.context is None."""
        config_file = tmp_path / "context.json"
        config_file.write_text(json.dumps(sample_context))

        mock_user.context = None

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_user]
        mock_db.query.return_value = mock_query

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            result = _seed_user_context()
            assert result is True
            assert mock_user.context == sample_context

    def test_handles_invalid_json(self, tmp_path):
        """Returns False when config file has invalid JSON."""
        config_file = tmp_path / "context.json"
        config_file.write_text("not valid json {{{")

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ):
            result = _seed_user_context()
            assert result is False

    def test_handles_db_error(self, tmp_path, sample_context):
        """Returns False and rolls back on database error."""
        config_file = tmp_path / "context.json"
        config_file.write_text(json.dumps(sample_context))

        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("DB connection failed")

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            result = _seed_user_context()
            assert result is False
            mock_db.rollback.assert_called_once()
            mock_db.close.assert_called_once()


class TestRunAutoSeed:
    """Tests for run_auto_seed orchestrator."""

    def test_returns_results_dict(self):
        """Returns dict with seeding results."""
        with patch(
            "zerg.services.auto_seed._seed_user_context", return_value=True
        ), patch(
            "zerg.services.auto_seed._seed_personal_credentials", return_value=True
        ), patch(
            "zerg.services.auto_seed._find_config_file", return_value=None
        ):
            result = run_auto_seed()

            assert isinstance(result, dict)
            assert "user_context" in result
            assert "credentials" in result

    def test_reports_success_with_filename(self, tmp_path):
        """Reports success with config filename when seeding works."""
        config_file = tmp_path / "user_context.local.json"
        config_file.write_text('{"display_name": "Test"}')

        with patch(
            "zerg.services.auto_seed._seed_user_context", return_value=True
        ), patch(
            "zerg.services.auto_seed._seed_personal_credentials", return_value=True
        ), patch(
            "zerg.services.auto_seed._seed_runners", return_value=True
        ), patch(
            "zerg.services.auto_seed._find_config_file",
            side_effect=[
                config_file,  # user context
                None,  # credentials
                None,  # runners
            ],
        ):
            result = run_auto_seed()

            assert "user_context.local.json" in result["user_context"]

    def test_reports_failure(self):
        """Reports failure when seeding fails."""
        with patch(
            "zerg.services.auto_seed._seed_user_context", return_value=False
        ), patch(
            "zerg.services.auto_seed._seed_personal_credentials", return_value=True
        ), patch(
            "zerg.services.auto_seed._find_config_file", return_value=None
        ):
            result = run_auto_seed()

            assert result["user_context"] == "failed"

    def test_reports_skipped_when_no_config(self):
        """Reports skipped when no config file exists."""
        with patch(
            "zerg.services.auto_seed._seed_user_context", return_value=True
        ), patch(
            "zerg.services.auto_seed._seed_personal_credentials", return_value=True
        ), patch(
            "zerg.services.auto_seed._find_config_file", return_value=None
        ):
            result = run_auto_seed()

            assert result["user_context"] == "skipped"
            assert result["credentials"] == "skipped"


class TestIdempotency:
    """Tests ensuring seeding is idempotent."""

    def test_multiple_calls_same_result(self, tmp_path):
        """Multiple seed calls don't change already-seeded user."""
        config_file = tmp_path / "context.json"
        sample_context = {"display_name": "Test", "servers": []}
        config_file.write_text(json.dumps(sample_context))

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.email = "test@example.com"
        mock_user.context = {}

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_user]
        mock_db.query.return_value = mock_query

        with patch(
            "zerg.services.auto_seed._find_config_file", return_value=config_file
        ), patch(
            "zerg.services.auto_seed.default_session_factory", return_value=mock_db
        ):
            # First call - should seed
            result1 = _seed_user_context()
            assert result1 is True
            assert mock_user.context == sample_context

            # Simulate user now having context
            mock_user.context = sample_context

            # Reset mock to track second call
            mock_db.reset_mock()

            # Second call - should skip
            result2 = _seed_user_context()
            assert result2 is True
            # Should NOT have called commit (no changes made)
            mock_db.commit.assert_not_called()
