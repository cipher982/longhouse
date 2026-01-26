import contextlib
from unittest.mock import patch

import pytest

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.config import Settings
from zerg.crud import crud
from zerg.main import app


def _make_admin_user(db_session):
    user = crud.get_user_by_email(db_session, "admin@local")
    if user is None:
        user = crud.create_user(db_session, email="admin@local", provider=None, role="ADMIN")
    else:
        user.role = "ADMIN"  # type: ignore[attr-defined]
        db_session.commit()
    return user


def _mock_settings_with_allowlist(allowed_model: str):
    """Create a mock Settings object with the specified allowed model."""
    from zerg.config import get_settings

    # Get the real settings to use as a base
    real_settings = get_settings()

    # Create a new Settings instance with modified allowed_models_non_admin
    mock_settings = Settings(
        testing=real_settings.testing,
        auth_disabled=real_settings.auth_disabled,
        jwt_secret=real_settings.jwt_secret,
        internal_api_secret=real_settings.internal_api_secret,
        google_client_id=real_settings.google_client_id,
        google_client_secret=real_settings.google_client_secret,
        github_client_id=real_settings.github_client_id,
        github_client_secret=real_settings.github_client_secret,
        trigger_signing_secret=real_settings.trigger_signing_secret,
        database_url=real_settings.database_url,
        fernet_secret=real_settings.fernet_secret,
        _llm_token_stream_default=real_settings._llm_token_stream_default,
        dev_admin=real_settings.dev_admin,
        log_level=real_settings.log_level,
        e2e_log_suppress=real_settings.e2e_log_suppress,
        environment=real_settings.environment,
        allowed_cors_origins=real_settings.allowed_cors_origins,
        openai_api_key=real_settings.openai_api_key,
        groq_api_key=real_settings.groq_api_key,
        app_public_url=real_settings.app_public_url,
        runner_docker_image=real_settings.runner_docker_image,
        pubsub_audience=real_settings.pubsub_audience,
        gmail_pubsub_topic=real_settings.gmail_pubsub_topic,
        pubsub_sa_email=real_settings.pubsub_sa_email,
        max_users=real_settings.max_users,
        admin_emails=real_settings.admin_emails,
        allowed_models_non_admin=allowed_model,  # Override this
        daily_courses_per_user=real_settings.daily_courses_per_user,
        daily_cost_per_user_cents=real_settings.daily_cost_per_user_cents,
        daily_cost_global_cents=real_settings.daily_cost_global_cents,
        discord_webhook_url=real_settings.discord_webhook_url,
        discord_enable_alerts=real_settings.discord_enable_alerts,
        discord_daily_digest_cron=real_settings.discord_daily_digest_cron,
        db_reset_password=real_settings.db_reset_password,
        jarvis_device_secret=real_settings.jarvis_device_secret,
        jarvis_workspace_path=real_settings.jarvis_workspace_path,
        notification_webhook=real_settings.notification_webhook,
        smoke_test_secret=real_settings.smoke_test_secret,
        job_queue_enabled=real_settings.job_queue_enabled,
        bootstrap_token=real_settings.bootstrap_token,
        container_default_image=real_settings.container_default_image,
        container_network_enabled=real_settings.container_network_enabled,
        container_user_id=real_settings.container_user_id,
        container_memory_limit=real_settings.container_memory_limit,
        container_cpus=real_settings.container_cpus,
        container_timeout_secs=real_settings.container_timeout_secs,
        container_seccomp_profile=real_settings.container_seccomp_profile,
        container_tools_enabled=real_settings.container_tools_enabled,
        roundabout_routing_model=real_settings.roundabout_routing_model,
        roundabout_llm_timeout=real_settings.roundabout_llm_timeout,
        concierge_tool_output_max_chars=real_settings.concierge_tool_output_max_chars,
        concierge_tool_output_preview_chars=real_settings.concierge_tool_output_preview_chars,
        e2e_use_postgres_schemas=real_settings.e2e_use_postgres_schemas,
        e2e_commis_id=real_settings.e2e_commis_id,
        jobs_git_repo_url=real_settings.jobs_git_repo_url,
        jobs_git_branch=real_settings.jobs_git_branch,
        jobs_git_token=real_settings.jobs_git_token,
        jobs_dir=real_settings.jobs_dir,
        jobs_refresh_interval_seconds=real_settings.jobs_refresh_interval_seconds,
    )
    return mock_settings


@pytest.mark.asyncio
async def test_non_admin_create_fiche_disallowed_model(client, db_session, _dev_user):
    # Mock get_settings to return allowlist restricted to TEST_COMMIS_MODEL
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)

    # Attempt to create fiche with a disallowed model
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        with patch("zerg.routers.fiches.get_settings", return_value=mock_settings):
            resp = client.post(
                "/api/fiches",
                json={
                    "name": "NA fiche",
                    "system_instructions": "sys",
                    "task_instructions": "task",
                    "model": TEST_MODEL,  # not in allowlist
                    "schedule": None,
                    "config": {},
                },
            )
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 422, resp.text
    assert "not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_non_admin_create_fiche_allowed_model(client, db_session, _dev_user):
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        with patch("zerg.routers.fiches.get_settings", return_value=mock_settings):
            resp = client.post(
                "/api/fiches",
                json={
                    "name": "OK fiche",
                    "system_instructions": "sys",
                    "task_instructions": "task",
                    "model": TEST_COMMIS_MODEL,
                    "schedule": None,
                    "config": {},
                },
            )
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["model"] == TEST_COMMIS_MODEL


@pytest.mark.asyncio
async def test_admin_bypasses_model_allowlist(client, db_session):
    # Restrict allowlist, but override current_user to ADMIN
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)
    admin = _make_admin_user(db_session)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        with patch("zerg.routers.fiches.get_settings", return_value=mock_settings):
            resp = client.post(
                "/api/fiches",
                json={
                    "name": "Admin fiche",
                    "system_instructions": "sys",
                    "task_instructions": "task",
                    "model": TEST_MODEL,  # disallowed for non-admins
                    "schedule": None,
                    "config": {},
                },
            )
    finally:
        # Clean override regardless of assertion outcome
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 201, resp.text
    assert resp.json()["model"] == TEST_MODEL


@pytest.mark.asyncio
async def test_models_endpoint_filtered_for_non_admin(client, db_session, _dev_user):
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        with patch("zerg.routers.models.get_settings", return_value=mock_settings):
            resp = client.get("/api/models/")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    assert ids == {TEST_COMMIS_MODEL}


@pytest.mark.asyncio
async def test_models_endpoint_admin_sees_all(client, db_session):
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)
    admin = _make_admin_user(db_session)

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        with patch("zerg.routers.models.get_settings", return_value=mock_settings):
            resp = client.get("/api/models/")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()}
    # Registry includes more than the single allowed id
    assert TEST_COMMIS_MODEL in ids and len(ids) > 1


@pytest.mark.asyncio
async def test_non_admin_update_fiche_disallowed_model(client, db_session, _dev_user):
    mock_settings = _mock_settings_with_allowlist(TEST_COMMIS_MODEL)
    # Ensure current user is non-admin dev user
    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _dev_user
    try:
        with patch("zerg.routers.fiches.get_settings", return_value=mock_settings):
            # Create an allowed fiche first
            resp = client.post(
                "/api/fiches",
                json={
                    "name": "Fiche",
                    "system_instructions": "sys",
                    "task_instructions": "task",
                    "model": TEST_COMMIS_MODEL,
                    "schedule": None,
                    "config": {},
                },
            )
            assert resp.status_code == 201, resp.text
            aid = resp.json()["id"]

            # Try to update to disallowed model
            resp2 = client.put(
                f"/api/fiches/{aid}",
                json={
                    "model": TEST_MODEL,
                },
            )
            assert resp2.status_code == 422, resp2.text
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]
