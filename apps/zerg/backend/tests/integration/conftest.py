"""
Integration test configuration.

These tests require REAL credentials and make REAL API calls.
They are NOT run by default - use: pytest tests/integration/ -v

IMPORTANT: Tests will FAIL LOUDLY if credentials are missing.
No silent skipping - if you run these, you need the creds.
"""

import os
from pathlib import Path

import dotenv
import pytest

# Load .env from repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]  # integration -> tests -> backend -> zerg -> apps -> repo_root
_env_path = _REPO_ROOT / ".env"
if _env_path.exists():
    dotenv.load_dotenv(_env_path)

# Also try .env.test for dedicated test credentials
_env_test_path = _REPO_ROOT / ".env.test"
if _env_test_path.exists():
    dotenv.load_dotenv(_env_test_path, override=True)


def _require_env(var_name: str, description: str) -> str:
    """Get required environment variable or FAIL with clear message."""
    value = os.environ.get(var_name)
    if not value:
        pytest.fail(
            f"\n\n"
            f"{'='*60}\n"
            f"MISSING REQUIRED CREDENTIAL: {var_name}\n"
            f"{'='*60}\n"
            f"\n"
            f"This integration test requires: {description}\n"
            f"\n"
            f"To run integration tests, either:\n"
            f"1. Set {var_name} in your environment\n"
            f"2. Add it to .env.test in the repo root\n"
            f"\n"
            f"If you don't have this credential, don't run integration tests.\n"
            f"Unit tests (pytest tests/) don't require credentials.\n"
            f"{'='*60}\n"
        )
    return value


# ============================================================
# Credential fixtures - FAIL if missing, no silent skip
# ============================================================

@pytest.fixture
def discord_webhook_url():
    """Discord webhook URL - required for Discord integration tests."""
    return _require_env(
        "TEST_DISCORD_WEBHOOK_URL",
        "Discord webhook URL (https://discord.com/api/webhooks/...)"
    )


@pytest.fixture
def slack_webhook_url():
    """Slack webhook URL - required for Slack integration tests."""
    return _require_env(
        "TEST_SLACK_WEBHOOK_URL",
        "Slack webhook URL (https://hooks.slack.com/services/...)"
    )


@pytest.fixture
def resend_api_key():
    """Resend API key - required for email integration tests."""
    return _require_env(
        "TEST_RESEND_API_KEY",
        "Resend API key (starts with re_)"
    )


@pytest.fixture
def resend_from_email():
    """Verified from email for Resend."""
    return _require_env(
        "TEST_RESEND_FROM_EMAIL",
        "Verified sender email for Resend"
    )


@pytest.fixture
def resend_to_email():
    """Test recipient email for Resend."""
    return _require_env(
        "TEST_RESEND_TO_EMAIL",
        "Test recipient email for Resend"
    )


@pytest.fixture
def twilio_account_sid():
    """Twilio Account SID - required for SMS integration tests."""
    return _require_env(
        "TEST_TWILIO_ACCOUNT_SID",
        "Twilio Account SID (starts with AC)"
    )


@pytest.fixture
def twilio_auth_token():
    """Twilio Auth Token."""
    return _require_env(
        "TEST_TWILIO_AUTH_TOKEN",
        "Twilio Auth Token"
    )


@pytest.fixture
def twilio_from_number():
    """Twilio phone number."""
    return _require_env(
        "TEST_TWILIO_FROM_NUMBER",
        "Twilio phone number (E.164 format: +15551234567)"
    )


@pytest.fixture
def twilio_to_number():
    """Test recipient phone number."""
    return _require_env(
        "TEST_TWILIO_TO_NUMBER",
        "Test recipient phone number (E.164 format)"
    )


@pytest.fixture
def github_token():
    """GitHub Personal Access Token."""
    return _require_env(
        "TEST_GITHUB_TOKEN",
        "GitHub Personal Access Token (starts with ghp_)"
    )


@pytest.fixture
def github_test_repo():
    """GitHub test repository in owner/repo format."""
    return _require_env(
        "TEST_GITHUB_REPO",
        "GitHub test repository (format: owner/repo)"
    )


@pytest.fixture
def jira_domain():
    """Jira Cloud domain."""
    return _require_env(
        "TEST_JIRA_DOMAIN",
        "Jira Cloud domain (e.g., yourcompany.atlassian.net)"
    )


@pytest.fixture
def jira_email():
    """Jira account email."""
    return _require_env(
        "TEST_JIRA_EMAIL",
        "Jira account email"
    )


@pytest.fixture
def jira_api_token():
    """Jira API token."""
    return _require_env(
        "TEST_JIRA_API_TOKEN",
        "Jira API token"
    )


@pytest.fixture
def jira_project_key():
    """Jira test project key."""
    return _require_env(
        "TEST_JIRA_PROJECT_KEY",
        "Jira project key for testing (e.g., TEST)"
    )


@pytest.fixture
def linear_api_key():
    """Linear API key."""
    return _require_env(
        "TEST_LINEAR_API_KEY",
        "Linear API key (starts with lin_api_)"
    )


@pytest.fixture
def notion_api_key():
    """Notion integration token."""
    return _require_env(
        "TEST_NOTION_API_KEY",
        "Notion integration token (starts with secret_)"
    )


@pytest.fixture
def notion_test_page_id():
    """Notion test page ID."""
    return _require_env(
        "TEST_NOTION_PAGE_ID",
        "Notion page ID for testing"
    )
