"""Tests for Settings.db_is_sqlite property.

Ensures config.db_is_sqlite() handles quoted URLs from .env files correctly,
aligning with database._is_sqlite_url() behavior.
"""

import os
from unittest.mock import patch

import pytest


class TestDbIsSqlite:
    """Test Settings.db_is_sqlite() properly detects SQLite URLs."""

    def _make_settings(self, database_url: str):
        """Create a Settings instance with the given DATABASE_URL."""
        # Import here to avoid module-level side effects
        from zerg.config import Settings

        return Settings(
            testing=True,
            auth_disabled=True,
            single_tenant=True,
            jwt_secret="test-secret",
            internal_api_secret="test-internal-secret",
            google_client_id=None,
            google_client_secret=None,
            github_client_id=None,
            github_client_secret=None,
            trigger_signing_secret=None,
            database_url=database_url,
            fernet_secret="test-fernet",
            _llm_token_stream_default=False,
            dev_admin=False,
            log_level="INFO",
            e2e_log_suppress=False,
            environment="test",
            allowed_cors_origins="",
            openai_api_key=None,
            groq_api_key=None,
            app_public_url=None,
            runner_docker_image="test",
            pubsub_audience=None,
            gmail_pubsub_topic=None,
            pubsub_sa_email=None,
            max_users=10,
            admin_emails="",
            allowed_models_non_admin="",
            daily_runs_per_user=0,
            daily_cost_per_user_cents=0,
            daily_cost_global_cents=0,
            discord_webhook_url=None,
            discord_enable_alerts=False,
            discord_daily_digest_cron="0 8 * * *",
            db_reset_password=None,
            oikos_device_secret=None,
            oikos_workspace_path="/tmp",
            notification_webhook=None,
            smoke_test_secret=None,
            agents_api_token=None,
            job_queue_enabled=False,
            jobs_git_repo_url=None,
            jobs_git_branch="main",
            jobs_git_token=None,
            jobs_dir="/opt/sauron-jobs",
            jobs_refresh_interval_seconds=60,
            container_default_image=None,
            container_network_enabled=False,
            container_user_id="65532",
            container_memory_limit="512m",
            container_cpus="0.5",
            container_timeout_secs=30,
            container_seccomp_profile=None,
            container_tools_enabled=False,
            roundabout_routing_model=None,
            roundabout_llm_timeout=1.5,
            bootstrap_token=None,
            oikos_tool_output_max_chars=8000,
            oikos_tool_output_preview_chars=1200,
        )

    def test_unquoted_sqlite_url(self):
        """Unquoted sqlite URL should be detected."""
        settings = self._make_settings("sqlite:///path/to/db.sqlite")
        assert settings.db_is_sqlite is True
        assert settings.lite_mode is True

    def test_double_quoted_sqlite_url(self):
        """Double-quoted sqlite URL should be detected (common .env format)."""
        settings = self._make_settings('"sqlite:///path/to/db.sqlite"')
        assert settings.db_is_sqlite is True
        assert settings.lite_mode is True

    def test_single_quoted_sqlite_url(self):
        """Single-quoted sqlite URL should be detected."""
        settings = self._make_settings("'sqlite:///path/to/db.sqlite'")
        assert settings.db_is_sqlite is True
        assert settings.lite_mode is True

    def test_postgres_url_not_sqlite(self):
        """Postgres URL should not be detected as SQLite."""
        settings = self._make_settings("postgresql://user:pass@host:5432/db")
        assert settings.db_is_sqlite is False
        assert settings.lite_mode is False

    def test_quoted_postgres_url_not_sqlite(self):
        """Quoted Postgres URL should not be detected as SQLite."""
        settings = self._make_settings('"postgresql://user:pass@host:5432/db"')
        assert settings.db_is_sqlite is False
        assert settings.lite_mode is False

    def test_empty_url(self):
        """Empty URL should return False."""
        settings = self._make_settings("")
        assert settings.db_is_sqlite is False

    def test_whitespace_url(self):
        """Whitespace-only URL should return False."""
        settings = self._make_settings("   ")
        assert settings.db_is_sqlite is False

    def test_memory_sqlite_url(self):
        """In-memory SQLite URL should be detected."""
        settings = self._make_settings("sqlite:///:memory:")
        assert settings.db_is_sqlite is True
