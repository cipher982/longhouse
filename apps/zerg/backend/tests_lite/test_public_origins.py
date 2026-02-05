"""Tests for public origin helpers.

These helpers drive CORS defaults and funnel origin checks.
"""

from zerg.config import Settings
from zerg.config import get_funnel_allowed_hosts
from zerg.config import get_public_origins
from zerg.config import resolve_cors_origins


def _make_settings(**overrides):
    base = dict(
        testing=True,
        auth_disabled=True,
        single_tenant=True,
        jwt_secret="test-secret",
        longhouse_password="",
        longhouse_password_hash="",
        internal_api_secret="test-internal-secret",
        google_client_id=None,
        google_client_secret=None,
        github_client_id=None,
        github_client_secret=None,
        trigger_signing_secret=None,
        database_url="sqlite:///test.db",
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
        public_site_url=None,
        public_api_url=None,
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
        control_plane_jwt_secret=None,
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


def test_funnel_hosts_from_public_site():
    settings = _make_settings(public_site_url="https://longhouse.ai")
    hosts = get_funnel_allowed_hosts(settings)
    assert "longhouse.ai" in hosts
