"""Tests for Slack webhook tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.slack_tools import send_slack_webhook


class TestSendSlackWebhook:
    """Tests for send_slack_webhook function."""

    def test_invalid_webhook_url_empty(self):
        """Test that empty webhook URL is rejected."""
        result = send_slack_webhook(
            webhook_url="",
            text="Test message"
        )
        assert result["ok"] is False
        assert "error_type" in result
        assert "user_message" in result

    def test_invalid_webhook_url_wrong_format(self):
        """Test that non-Slack URLs are rejected."""
        result = send_slack_webhook(
            webhook_url="https://example.com/webhook",
            text="Test message"
        )
        assert result["ok"] is False
        assert "error_type" in result
        assert "user_message" in result

    def test_empty_text_without_blocks(self):
        """Test that empty text without blocks is rejected."""
        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text=""
        )
        assert result["ok"] is False

    @patch("zerg.tools.builtin.slack_tools.httpx.Client")
    def test_successful_message(self, mock_client):
        """Test successful message sending."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "ok"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text="Test message"
        )

        assert result["ok"] is True
        assert result["data"]["status_code"] == 200

    @patch("zerg.tools.builtin.slack_tools.httpx.Client")
    def test_rate_limit_response(self, mock_client):
        """Test rate limit handling."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "5"}
        mock_response.text = "rate_limited"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text="Test message"
        )

        assert result["ok"] is False
        assert result["error_type"] == "rate_limited"

    @patch("zerg.tools.builtin.slack_tools.httpx.Client")
    def test_message_with_blocks(self, mock_client):
        """Test message with Block Kit blocks."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "ok"
        mock_post = mock_client.return_value.__enter__.return_value.post
        mock_post.return_value = mock_response

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Bold text*"}
            }
        ]

        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text="Fallback text",
            blocks=blocks
        )

        assert result["ok"] is True

        # Verify the payload was constructed correctly
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")

        assert payload["text"] == "Fallback text"
        assert "blocks" in payload

    @patch("zerg.tools.builtin.slack_tools.httpx.Client")
    def test_webhook_not_found(self, mock_client):
        """Test handling of webhook not found."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "no_team"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text="Test message"
        )

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"

    @patch("zerg.tools.builtin.slack_tools.httpx.Client")
    def test_invalid_payload(self, mock_client):
        """Test handling of invalid payload error."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = "invalid_payload"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_slack_webhook(
            webhook_url="https://hooks.slack.com/services/T00/B00/xxx",
            text="Test message"
        )

        assert result["ok"] is False
        assert result["error_type"] == "execution_error"
