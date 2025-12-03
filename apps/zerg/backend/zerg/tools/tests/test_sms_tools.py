"""Tests for SMS (Twilio) tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.sms_tools import send_sms


class TestSendSms:
    """Tests for send_sms function."""

    def test_invalid_account_sid_format(self):
        """Test that invalid Account SID format is rejected."""
        result = send_sms(
            account_sid="invalid_sid",
            auth_token="valid_token_32_chars_long_xxxxx",
            from_number="+15551234567",
            to_number="+15559876543",
            message="Test message"
        )
        assert result["ok"] is False
        assert "Account SID" in result.get("user_message", "")

    def test_invalid_from_number(self):
        """Test that invalid from phone number is rejected."""
        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="1234567890",  # Missing + and country code
            to_number="+15559876543",
            message="Test message"
        )
        assert result["ok"] is False

    def test_invalid_to_number(self):
        """Test that invalid to phone number is rejected."""
        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="invalid",
            message="Test message"
        )
        assert result["ok"] is False

    def test_empty_message(self):
        """Test that empty message is rejected."""
        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message=""
        )
        assert result["ok"] is False

    def test_message_too_long(self):
        """Test that message over 1600 characters is rejected."""
        long_message = "x" * 1601
        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message=long_message
        )
        assert result["ok"] is False

    @patch("zerg.tools.builtin.sms_tools.httpx.Client")
    def test_successful_sms(self, mock_client):
        """Test successful SMS sending."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "sid": "SM123456",
            "status": "queued",
            "num_segments": "1",
            "price": "-0.0075"
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message="Test message"
        )

        assert result["ok"] is True
        assert result["data"]["message_sid"] == "SM123456"

    @patch("zerg.tools.builtin.sms_tools.httpx.Client")
    def test_unauthorized_error(self, mock_client):
        """Test handling of unauthorized error."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Invalid credentials"}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message="Test message"
        )

        assert result["ok"] is False

    @patch("zerg.tools.builtin.sms_tools.httpx.Client")
    def test_rate_limit(self, mock_client):
        """Test rate limit handling."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.json.return_value = {"message": "Too many requests"}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message="Test message"
        )

        assert result["ok"] is False

    @patch("zerg.tools.builtin.sms_tools.httpx.Client")
    def test_sms_with_callback(self, mock_client):
        """Test SMS with status callback URL."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "sid": "SM789012",
            "status": "queued"
        }
        mock_post = mock_client.return_value.__enter__.return_value.post
        mock_post.return_value = mock_response

        result = send_sms(
            account_sid="AC" + "x" * 32,
            auth_token="x" * 32,
            from_number="+15551234567",
            to_number="+15559876543",
            message="Test message",
            status_callback="https://example.com/callback"
        )

        assert result["ok"] is True
