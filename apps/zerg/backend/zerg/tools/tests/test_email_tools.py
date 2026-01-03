"""Tests for Email (AWS SES) tools."""

from unittest.mock import MagicMock
from unittest.mock import patch

from botocore.exceptions import ClientError
from zerg.tools.builtin.email_tools import send_email


class TestSendEmail:
    """Tests for send_email function."""

    def test_missing_credentials(self):
        """Test that missing credentials returns proper error."""
        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"

    def test_invalid_from_email(self):
        """Test that invalid from email is rejected."""
        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="not-an-email",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert "Invalid" in result["user_message"]

    def test_invalid_to_email(self):
        """Test that invalid to email is rejected."""
        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="not-an-email",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert "Invalid" in result["user_message"]

    def test_missing_content(self):
        """Test that either text or html is required."""
        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
        )
        assert result["ok"] is False
        assert "text or html" in result["user_message"].lower()

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_successful_email(self, mock_boto_client):
        """Test successful email sending."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_123"}
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test Subject",
            text="Test message body",
        )

        assert result["ok"] is True
        assert result["data"]["message_id"] == "ses_msg_123"

        # Verify boto3.client was called with correct params
        mock_boto_client.assert_called_once_with(
            "ses",
            region_name="us-east-1",
            aws_access_key_id="AKIATEST123",
            aws_secret_access_key="test-secret",
        )

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_invalid_credentials_error(self, mock_boto_client):
        """Test handling of invalid credentials error."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "InvalidClientTokenId", "Message": "Invalid credentials"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_email_with_html(self, mock_boto_client):
        """Test email with HTML content."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_456"}
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test HTML Email",
            html="<h1>Hello!</h1>",
        )

        assert result["ok"] is True

        # Verify the payload was constructed correctly
        call_args = mock_ses.send_email.call_args
        message = call_args.kwargs.get("Message") or call_args[1].get("Message")

        assert "Html" in message["Body"]
        assert message["Body"]["Html"]["Data"] == "<h1>Hello!</h1>"

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_email_with_cc_bcc(self, mock_boto_client):
        """Test email with CC and BCC."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_789"}
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
            cc="cc@example.com",
            bcc="bcc@example.com",
        )

        assert result["ok"] is True

        # Verify CC and BCC were added to destination
        call_args = mock_ses.send_email.call_args
        destination = call_args.kwargs.get("Destination") or call_args[1].get("Destination")
        assert destination["CcAddresses"] == ["cc@example.com"]
        assert destination["BccAddresses"] == ["bcc@example.com"]

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_throttling_error(self, mock_boto_client):
        """Test throttling/rate limit handling."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "rate_limited"

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_custom_region(self, mock_boto_client):
        """Test email sending with custom region."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_eu"}
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            region="eu-west-1",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is True

        # Verify region was passed correctly
        mock_boto_client.assert_called_once_with(
            "ses",
            region_name="eu-west-1",
            aws_access_key_id="AKIATEST123",
            aws_secret_access_key="test-secret",
        )

    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_message_rejected_error(self, mock_boto_client):
        """Test handling of message rejected error."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "MessageRejected", "Message": "Email address not verified"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses

        result = send_email(
            access_key_id="AKIATEST123",
            secret_access_key="test-secret",
            from_email="test@example.com",
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "validation_error"
        assert "rejected" in result["user_message"].lower()
