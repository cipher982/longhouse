"""Tests for Email (AWS SES) tools."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from botocore.exceptions import ClientError
from zerg.tools.builtin.email_tools import send_email


def _fake_db_session(counter_count: int = 0):
    db = MagicMock()
    counter = SimpleNamespace(count=counter_count)
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = counter
    session = MagicMock()
    session.__enter__.return_value = db
    session.__exit__.return_value = False
    return session, db


class TestSendEmail:
    """Tests for send_email function."""

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "",
            "AWS_SES_SECRET_ACCESS_KEY": "",
            "FROM_EMAIL": "",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    def test_missing_credentials(self, _mock_resolver):
        """Test that missing credentials returns proper error."""
        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert result["error_type"] == "connector_not_configured"

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "not-an-email",
        },
        clear=False,
    )
    def test_invalid_from_email(self):
        """Test that invalid from email is rejected."""
        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert "Invalid" in result["user_message"]

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    def test_invalid_to_email(self):
        """Test that invalid to email is rejected."""
        result = send_email(
            to="not-an-email",
            subject="Test",
            text="Test message",
        )
        assert result["ok"] is False
        assert "Invalid" in result["user_message"]

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    def test_missing_content(self):
        """Test that either text or html is required."""
        result = send_email(
            to="recipient@example.com",
            subject="Test",
        )
        assert result["ok"] is False
        assert "text or html" in result["user_message"].lower()

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_successful_email(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test successful email sending."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_123"}
        mock_boto_client.return_value = mock_ses
        session, db = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test Subject",
            text="Test message body",
        )

        assert result["ok"] is True
        assert result["data"]["message_id"] == "ses_msg_123"
        mock_boto_client.assert_called_once_with(
            "ses",
            region_name="us-east-1",
            aws_access_key_id="AKIATEST123",
            aws_secret_access_key="test-secret",
        )
        assert db.commit.call_count == 2

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_invalid_credentials_error(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test handling of invalid credentials error."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "InvalidClientTokenId", "Message": "Invalid credentials"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_email_with_html(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test email with HTML content."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_456"}
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test HTML Email",
            html="<h1>Hello!</h1>",
        )

        assert result["ok"] is True
        call_args = mock_ses.send_email.call_args
        message = call_args.kwargs.get("Message") or call_args[1].get("Message")
        assert "Html" in message["Body"]
        assert message["Body"]["Html"]["Data"] == "<h1>Hello!</h1>"

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_email_with_cc_bcc(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test email with CC and BCC."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_789"}
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
            cc="cc@example.com",
            bcc="bcc@example.com",
        )

        assert result["ok"] is True
        call_args = mock_ses.send_email.call_args
        destination = call_args.kwargs.get("Destination") or call_args[1].get("Destination")
        assert destination["CcAddresses"] == ["cc@example.com"]
        assert destination["BccAddresses"] == ["bcc@example.com"]

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_throttling_error(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test throttling/rate limit handling."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "rate_limited"

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
            "AWS_SES_REGION": "eu-west-1",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_custom_region(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test email sending with custom region."""
        mock_ses = MagicMock()
        mock_ses.send_email.return_value = {"MessageId": "ses_msg_eu"}
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is True
        mock_boto_client.assert_called_once_with(
            "ses",
            region_name="eu-west-1",
            aws_access_key_id="AKIATEST123",
            aws_secret_access_key="test-secret",
        )

    @patch.dict(
        "os.environ",
        {
            "AWS_SES_ACCESS_KEY_ID": "AKIATEST123",
            "AWS_SES_SECRET_ACCESS_KEY": "test-secret",
            "FROM_EMAIL": "test@example.com",
        },
        clear=False,
    )
    @patch("zerg.tools.builtin.email_tools.get_commis_context", return_value=SimpleNamespace(owner_id=1))
    @patch("zerg.tools.builtin.email_tools.get_credential_resolver", return_value=None)
    @patch("zerg.tools.builtin.email_tools.db_session")
    @patch("zerg.tools.builtin.email_tools.boto3.client")
    def test_message_rejected_error(self, mock_boto_client, mock_db_session, _mock_resolver, _mock_ctx):
        """Test handling of message rejected error."""
        mock_ses = MagicMock()
        error_response = {"Error": {"Code": "MessageRejected", "Message": "Email address not verified"}}
        mock_ses.send_email.side_effect = ClientError(error_response, "SendEmail")
        mock_boto_client.return_value = mock_ses
        session, _ = _fake_db_session()
        mock_db_session.return_value = session

        result = send_email(
            to="recipient@example.com",
            subject="Test",
            text="Test message",
        )

        assert result["ok"] is False
        assert result["error_type"] == "validation_error"
        assert "rejected" in result["user_message"].lower()
