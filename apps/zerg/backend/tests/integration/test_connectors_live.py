"""
Live integration tests for all connectors.

Run with: pytest tests/integration/test_connectors_live.py -v

WARNING: These tests make REAL API calls and may:
- Send real messages to Slack/Discord
- Send real emails
- Send real SMS (costs money!)
- Create real issues in GitHub/Jira/Linear/Notion

Only run these when you intentionally want to test the full integration.
"""

import pytest


class TestDiscordIntegration:
    """Live Discord webhook tests."""

    def test_send_simple_message(self, discord_webhook_url):
        """Test sending a simple message to Discord."""
        from zerg.tools.builtin.discord_tools import send_discord_webhook

        result = send_discord_webhook(
            webhook_url=discord_webhook_url,
            content="🧪 Integration test - simple message"
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert result["status_code"] == 204

    def test_send_embed_message(self, discord_webhook_url):
        """Test sending an embed message to Discord."""
        from zerg.tools.builtin.discord_tools import send_discord_webhook

        result = send_discord_webhook(
            webhook_url=discord_webhook_url,
            content="",
            embeds=[{
                "title": "🧪 Integration Test",
                "description": "This is an automated integration test",
                "color": 3066993,  # Green
                "fields": [
                    {"name": "Status", "value": "Testing", "inline": True}
                ]
            }]
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"


class TestSlackIntegration:
    """Live Slack webhook tests."""

    def test_send_simple_message(self, slack_webhook_url):
        """Test sending a simple message to Slack."""
        from zerg.tools.builtin.slack_tools import send_slack_webhook

        result = send_slack_webhook(
            webhook_url=slack_webhook_url,
            text="🧪 Integration test - simple message"
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"

    def test_send_block_message(self, slack_webhook_url):
        """Test sending a Block Kit message to Slack."""
        from zerg.tools.builtin.slack_tools import send_slack_webhook

        result = send_slack_webhook(
            webhook_url=slack_webhook_url,
            text="Integration test",
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "🧪 *Integration Test*\nThis is an automated test with Block Kit"
                }
            }]
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"


class TestEmailIntegration:
    """Live Resend email tests."""

    def test_send_text_email(self, resend_api_key, resend_from_email, resend_to_email):
        """Test sending a plain text email."""
        from zerg.tools.builtin.email_tools import send_email

        result = send_email(
            api_key=resend_api_key,
            from_email=resend_from_email,
            to=resend_to_email,
            subject="🧪 Integration Test - Text Email",
            text="This is an automated integration test email."
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert "message_id" in result

    def test_send_html_email(self, resend_api_key, resend_from_email, resend_to_email):
        """Test sending an HTML email."""
        from zerg.tools.builtin.email_tools import send_email

        result = send_email(
            api_key=resend_api_key,
            from_email=resend_from_email,
            to=resend_to_email,
            subject="🧪 Integration Test - HTML Email",
            html="<h1>Integration Test</h1><p>This is an automated test.</p>"
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"


class TestSmsIntegration:
    """Live Twilio SMS tests.

    WARNING: These tests send real SMS messages and cost money!
    """

    def test_send_sms(self, twilio_account_sid, twilio_auth_token, twilio_from_number, twilio_to_number):
        """Test sending an SMS message."""
        from zerg.tools.builtin.sms_tools import send_sms

        result = send_sms(
            account_sid=twilio_account_sid,
            auth_token=twilio_auth_token,
            from_number=twilio_from_number,
            to_number=twilio_to_number,
            message="🧪 Zerg integration test"
        )

        assert result["success"] is True, f"Failed: {result.get('error_message', result.get('error'))}"
        assert "message_sid" in result


class TestGitHubIntegration:
    """Live GitHub API tests."""

    def test_list_issues(self, github_token, github_test_repo):
        """Test listing issues from a repository."""
        from zerg.tools.builtin.github_tools import github_list_issues

        owner, repo = github_test_repo.split("/")

        result = github_list_issues(
            token=github_token,
            owner=owner,
            repo=repo,
            per_page=5
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert "data" in result

    def test_create_and_close_issue(self, github_token, github_test_repo):
        """Test creating and then closing an issue."""
        from zerg.tools.builtin.github_tools import github_create_issue, github_add_comment

        owner, repo = github_test_repo.split("/")

        # Create issue
        create_result = github_create_issue(
            token=github_token,
            owner=owner,
            repo=repo,
            title="🧪 Integration Test Issue - Auto Delete",
            body="This is an automated integration test. This issue can be safely deleted.",
            labels=["test", "automated"]
        )

        assert create_result["success"] is True, f"Create failed: {create_result.get('error')}"

        # Add a comment
        issue_number = create_result["data"]["number"]
        comment_result = github_add_comment(
            token=github_token,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            body="🤖 Automated comment from integration test"
        )

        assert comment_result["success"] is True, f"Comment failed: {comment_result.get('error')}"


class TestJiraIntegration:
    """Live Jira API tests."""

    def test_list_issues(self, jira_domain, jira_email, jira_api_token, jira_project_key):
        """Test listing issues from Jira."""
        from zerg.tools.builtin.jira_tools import jira_list_issues

        result = jira_list_issues(
            domain=jira_domain,
            email=jira_email,
            api_token=jira_api_token,
            project_key=jira_project_key,
            max_results=5
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"


class TestLinearIntegration:
    """Live Linear API tests."""

    def test_list_teams(self, linear_api_key):
        """Test listing teams from Linear."""
        from zerg.tools.builtin.linear_tools import linear_list_teams

        result = linear_list_teams(api_key=linear_api_key)

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert "data" in result

    def test_list_issues(self, linear_api_key):
        """Test listing issues from Linear."""
        from zerg.tools.builtin.linear_tools import linear_list_issues

        result = linear_list_issues(
            api_key=linear_api_key,
            first=5
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"


class TestNotionIntegration:
    """Live Notion API tests."""

    def test_search(self, notion_api_key):
        """Test searching Notion workspace."""
        from zerg.tools.builtin.notion_tools import notion_search

        result = notion_search(
            api_key=notion_api_key,
            query="test"
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"

    def test_get_page(self, notion_api_key, notion_test_page_id):
        """Test getting a Notion page."""
        from zerg.tools.builtin.notion_tools import notion_get_page

        result = notion_get_page(
            api_key=notion_api_key,
            page_id=notion_test_page_id
        )

        assert result["success"] is True, f"Failed: {result.get('error')}"
