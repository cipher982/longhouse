"""Tests for Discord webhook tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.discord_tools import send_discord_webhook


class TestSendDiscordWebhook:
    """Tests for send_discord_webhook function."""

    def test_invalid_webhook_url_empty(self):
        """Test that empty webhook URL is rejected."""
        result = send_discord_webhook(
            webhook_url="",
            content="Test message"
        )
        assert result["success"] is False
        assert "Invalid webhook URL" in result["error"]

    def test_invalid_webhook_url_wrong_format(self):
        """Test that non-Discord URLs are rejected."""
        result = send_discord_webhook(
            webhook_url="https://example.com/webhook",
            content="Test message"
        )
        assert result["success"] is False
        assert "Invalid webhook URL" in result["error"]

    def test_no_content_or_embeds(self):
        """Test that at least content or embeds must be provided."""
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc"
        )
        assert result["success"] is False
        assert "Must provide either 'content' or 'embeds'" in result["error"]

    def test_content_too_long(self):
        """Test that content over 2000 characters is rejected."""
        long_content = "x" * 2001
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            content=long_content
        )
        assert result["success"] is False
        assert "exceeds 2000 character limit" in result["error"]

    def test_embeds_not_list(self):
        """Test that embeds must be a list."""
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            embeds={"title": "Invalid"}  # Should be a list, not dict
        )
        assert result["success"] is False
        assert "Embeds must be a list" in result["error"]

    def test_too_many_embeds(self):
        """Test that maximum of 10 embeds is enforced."""
        too_many_embeds = [{"title": f"Embed {i}"} for i in range(11)]
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            embeds=too_many_embeds
        )
        assert result["success"] is False
        assert "Maximum of 10 embeds" in result["error"]

    def test_embed_title_too_long(self):
        """Test that embed title length limit is enforced."""
        embeds = [{"title": "x" * 257}]
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            embeds=embeds
        )
        assert result["success"] is False
        assert "title exceeds 256 character limit" in result["error"]

    def test_embed_description_too_long(self):
        """Test that embed description length limit is enforced."""
        embeds = [{"description": "x" * 4097}]
        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            embeds=embeds
        )
        assert result["success"] is False
        assert "description exceeds 4096 character limit" in result["error"]

    @patch("zerg.tools.builtin.discord_tools.httpx.Client")
    def test_successful_message(self, mock_client):
        """Test successful message sending."""
        # Mock successful response (Discord returns 204 No Content)
        mock_response = Mock()
        mock_response.status_code = 204
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            content="Test message"
        )

        assert result["success"] is True
        assert result["status_code"] == 204

    @patch("zerg.tools.builtin.discord_tools.httpx.Client")
    def test_rate_limit_response(self, mock_client):
        """Test rate limit handling."""
        # Mock rate limit response
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.json.return_value = {"retry_after": 5.0}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            content="Test message"
        )

        assert result["success"] is False
        assert result["status_code"] == 429
        assert "Rate limit" in result["error"]
        assert result["rate_limit_retry_after"] == 5.0

    @patch("zerg.tools.builtin.discord_tools.httpx.Client")
    def test_message_with_all_options(self, mock_client):
        """Test message with all optional parameters."""
        mock_response = Mock()
        mock_response.status_code = 204
        mock_post = mock_client.return_value.__enter__.return_value.post
        mock_post.return_value = mock_response

        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            content="Test message",
            username="Test Bot",
            avatar_url="https://example.com/avatar.png",
            embeds=[{
                "title": "Test Embed",
                "description": "Test description",
                "color": 5814783,
                "fields": [
                    {"name": "Field 1", "value": "Value 1", "inline": True},
                    {"name": "Field 2", "value": "Value 2", "inline": True}
                ]
            }],
            tts=True
        )

        assert result["success"] is True
        assert result["status_code"] == 204

        # Verify the payload was constructed correctly
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]

        assert payload["content"] == "Test message"
        assert payload["username"] == "Test Bot"
        assert payload["avatar_url"] == "https://example.com/avatar.png"
        assert payload["tts"] is True
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["title"] == "Test Embed"

    @patch("zerg.tools.builtin.discord_tools.httpx.Client")
    def test_api_error_response(self, mock_client):
        """Test handling of Discord API errors."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"message": "Invalid payload"}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_discord_webhook(
            webhook_url="https://discord.com/api/webhooks/123/abc",
            content="Test message"
        )

        assert result["success"] is False
        assert result["status_code"] == 400
        assert "Invalid payload" in result["error"]
