"""Tests for Email (Resend) tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.email_tools import send_email


class TestSendEmail:
    """Tests for send_email function."""

    def test_invalid_api_key_format(self):
        """Test that invalid API key format is rejected."""
        result = send_email(
            api_key="invalid_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message"
        )
        assert result["success"] is False
        assert "Invalid API key format" in result["error"]

    def test_invalid_from_email(self):
        """Test that invalid from email is rejected."""
        result = send_email(
            api_key="re_test_key",
            from_email="not-an-email",
            to="recipient@example.com",
            subject="Test",
            text="Test message"
        )
        assert result["success"] is False
        assert "Invalid" in result["error"]

    def test_invalid_to_email(self):
        """Test that invalid to email is rejected."""
        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="not-an-email",
            subject="Test",
            text="Test message"
        )
        assert result["success"] is False
        assert "Invalid" in result["error"]

    def test_missing_content(self):
        """Test that either text or html is required."""
        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test"
        )
        assert result["success"] is False
        assert "text or html" in result["error"].lower()

    @patch("zerg.tools.builtin.email_tools.httpx.Client")
    def test_successful_email(self, mock_client):
        """Test successful email sending."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "msg_123"}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test Subject",
            text="Test message body"
        )

        assert result["success"] is True
        assert result["message_id"] == "msg_123"

    @patch("zerg.tools.builtin.email_tools.httpx.Client")
    def test_unauthorized_error(self, mock_client):
        """Test handling of unauthorized error."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Invalid API key"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message"
        )

        assert result["success"] is False
        assert "401" in result["error"]

    @patch("zerg.tools.builtin.email_tools.httpx.Client")
    def test_email_with_html(self, mock_client):
        """Test email with HTML content."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "msg_456"}
        mock_post = mock_client.return_value.__enter__.return_value.post
        mock_post.return_value = mock_response

        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test HTML Email",
            html="<h1>Hello!</h1>"
        )

        assert result["success"] is True

        # Verify the payload was constructed correctly
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")

        assert payload["html"] == "<h1>Hello!</h1>"

    @patch("zerg.tools.builtin.email_tools.httpx.Client")
    def test_email_with_cc_bcc(self, mock_client):
        """Test email with CC and BCC."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "msg_789"}
        mock_post = mock_client.return_value.__enter__.return_value.post
        mock_post.return_value = mock_response

        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
            cc="cc@example.com",
            bcc="bcc@example.com"
        )

        assert result["success"] is True

    @patch("zerg.tools.builtin.email_tools.httpx.Client")
    def test_rate_limit(self, mock_client):
        """Test rate limit handling."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.text = "Too many requests"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_email(
            api_key="re_test_key",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message"
        )

        assert result["success"] is False
        assert "429" in result["error"] or "rate" in result["error"].lower()
