"""Tests for public origin helpers.

These helpers drive CORS defaults.
"""

from zerg.config import AppMode
from zerg.config import Settings
from zerg.config import get_public_origins
from zerg.config import resolve_cors_origins


def _make_settings(**overrides):
    base = dict(
        app_mode=AppMode.DEV,
        testing=True,
        auth_disabled=True,
        single_tenant=True,
        demo_mode=False,
        jwt_secret="test-secret",
        longhouse_password="",
        longhouse_password_hash="",
        internal_api_secret="test-internal-secret",
        google_client_id=None,
        google_ios_client_id=None,
        google_client_secret=None,
        github_client_id=None,
        github_client_secret=None,
        database_url="sqlite:///test.db",
        archive_root="archive",
        archive_primary_tenant_id="default",
        archive_primary_chunk_target_bytes=1024,
        fernet_secret="test-fernet",
        _llm_token_stream_default=False,
        dev_admin=False,
        log_level="INFO",
        e2e_log_suppress=False,
        environment="test",
        allowed_cors_origins="",
        openai_api_key=None,
        app_public_url=None,
        public_site_url=None,
        public_api_url=None,
        runner_docker_image="test",
        runner_binary_tag="runner-v0.1.3",
        max_users=10,
        admin_emails="",
        allowed_models_non_admin="",
        daily_runs_per_user=0,
        daily_cost_per_user_cents=0,
        daily_cost_global_cents=0,
        discord_webhook_url=None,
        discord_enable_alerts=False,
        control_plane_url=None,
        smoke_test_secret=None,
        tool_output_max_chars=8000,
    )
    base.update(overrides)
    return Settings(**base)


def test_public_origins_from_site_and_api():
    settings = _make_settings(
        public_site_url="https://longhouse.ai",
        public_api_url="https://api.longhouse.ai",
    )
    assert get_public_origins(settings) == ["https://longhouse.ai", "https://api.longhouse.ai"]


def test_resolve_cors_origins_prefers_explicit_env():
    settings = _make_settings(allowed_cors_origins="https://a.com, https://b.com")
    assert resolve_cors_origins(settings) == ["https://a.com", "https://b.com"]
