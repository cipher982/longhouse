"""Tests for contact_user tool."""

from unittest.mock import MagicMock, patch
import pytest

from zerg.context import WorkerContext, set_worker_context, reset_worker_context
from zerg.tools.builtin.contact_user import contact_user
from zerg.tools.error_envelope import ErrorType


@pytest.fixture
def mock_worker_context():
    """Create a mock worker context."""
    ctx = WorkerContext(
        worker_id="test-worker-123",
        owner_id=1,
        run_id="test-run-456",
        task="Test task",
    )
    token = set_worker_context(ctx)
    yield ctx
    reset_worker_context(token)


@pytest.fixture
def mock_user():
    """Create a mock user object."""
    user = MagicMock()
    user.id = 1
    user.email = "user@example.com"
    user.display_name = "Test User"
    return user


class TestContactUser:
    """Test suite for contact_user tool."""

    def test_successful_notification(self, mock_worker_context, mock_user):
        """Test successful user notification."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud, \
             patch("zerg.tools.builtin.contact_user.send_email") as mock_send_email:

            # Setup mocks
            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = mock_user
            mock_send_email.return_value = {
                "ok": True,
                "data": {"message_id": "msg-123"},
            }

            # Call the tool
            result = contact_user(
                subject="Task completed",
                message="Your task has finished successfully.",
                priority="normal",
            )

            # Verify success
            assert result["ok"] is True
            assert "data" in result
            assert result["data"]["message_id"] == "msg-123"

            # Verify send_email was called with correct params
            mock_send_email.assert_called_once()
            call_kwargs = mock_send_email.call_args[1]
            assert call_kwargs["to"] == "user@example.com"
            assert call_kwargs["subject"] == "[Swarmlet] Task completed"
            assert "Your task has finished successfully" in call_kwargs["html"]

    def test_missing_worker_context(self):
        """Test behavior when no worker context exists."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx:
            mock_get_ctx.return_value = None

            result = contact_user(
                subject="Test",
                message="Test message",
            )

            # Verify error
            assert result["ok"] is False
            assert result["error_type"] == ErrorType.EXECUTION_ERROR.value
            assert "worker context" in result["user_message"].lower()

    def test_missing_owner_id(self):
        """Test behavior when worker context has no owner_id."""
        ctx = WorkerContext(worker_id="test-worker", owner_id=None)

        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx:
            mock_get_ctx.return_value = ctx

            result = contact_user(
                subject="Test",
                message="Test message",
            )

            # Verify error
            assert result["ok"] is False
            assert result["error_type"] == ErrorType.EXECUTION_ERROR.value
            assert "owner" in result["user_message"].lower()

    def test_user_not_found(self, mock_worker_context):
        """Test behavior when user does not exist."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud:

            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = None

            result = contact_user(
                subject="Test",
                message="Test message",
            )

            # Verify error
            assert result["ok"] is False
            assert result["error_type"] == ErrorType.EXECUTION_ERROR.value
            assert "not found" in result["user_message"].lower()

    def test_user_no_email(self, mock_worker_context):
        """Test behavior when user has no email configured."""
        user = MagicMock()
        user.id = 1
        user.email = None

        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud:

            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = user

            result = contact_user(
                subject="Test",
                message="Test message",
            )

            # Verify error
            assert result["ok"] is False
            assert result["error_type"] == ErrorType.VALIDATION_ERROR.value
            assert "no email" in result["user_message"].lower()

    def test_email_send_failure(self, mock_worker_context, mock_user):
        """Test behavior when email sending fails."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud, \
             patch("zerg.tools.builtin.contact_user.send_email") as mock_send_email:

            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = mock_user
            mock_send_email.return_value = {
                "ok": False,
                "error_type": ErrorType.INVALID_CREDENTIALS.value,
                "user_message": "API key invalid",
            }

            result = contact_user(
                subject="Test",
                message="Test message",
            )

            # Verify error is propagated
            assert result["ok"] is False
            assert result["error_type"] == ErrorType.INVALID_CREDENTIALS.value

    def test_input_validation_empty_subject(self, mock_worker_context):
        """Test input validation for empty subject."""
        result = contact_user(
            subject="",
            message="Test message",
        )

        # Verify validation error
        assert result["ok"] is False
        assert result["error_type"] == ErrorType.VALIDATION_ERROR.value
        assert "subject" in result["user_message"].lower()

    def test_input_validation_empty_message(self, mock_worker_context):
        """Test input validation for empty message."""
        result = contact_user(
            subject="Test",
            message="",
        )

        # Verify validation error
        assert result["ok"] is False
        assert result["error_type"] == ErrorType.VALIDATION_ERROR.value
        assert "message" in result["user_message"].lower()

    def test_priority_levels(self, mock_worker_context, mock_user):
        """Test different priority levels."""
        priorities = ["low", "normal", "high", "urgent"]

        for priority in priorities:
            with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
                 patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
                 patch("zerg.tools.builtin.contact_user.crud") as mock_crud, \
                 patch("zerg.tools.builtin.contact_user.send_email") as mock_send_email:

                mock_get_ctx.return_value = mock_worker_context
                mock_db_session.return_value.__enter__.return_value = MagicMock()
                mock_crud.get_user.return_value = mock_user
                mock_send_email.return_value = {
                    "ok": True,
                    "data": {"message_id": f"msg-{priority}"},
                }

                result = contact_user(
                    subject="Test",
                    message="Test message",
                    priority=priority,
                )

                assert result["ok"] is True

    def test_invalid_priority(self, mock_worker_context):
        """Test invalid priority level."""
        result = contact_user(
            subject="Test",
            message="Test message",
            priority="invalid",
        )

        # Verify validation error
        assert result["ok"] is False
        assert result["error_type"] == ErrorType.VALIDATION_ERROR.value

    def test_markdown_in_message(self, mock_worker_context, mock_user):
        """Test that markdown is properly converted to HTML."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud, \
             patch("zerg.tools.builtin.contact_user.send_email") as mock_send_email:

            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = mock_user
            mock_send_email.return_value = {
                "ok": True,
                "data": {"message_id": "msg-123"},
            }

            # Send message with markdown
            result = contact_user(
                subject="Test",
                message="# Heading\n\n**Bold text** and *italic*\n\n- List item 1\n- List item 2",
            )

            assert result["ok"] is True

            # Verify HTML was passed to send_email
            call_kwargs = mock_send_email.call_args[1]
            assert "<h1>" in call_kwargs["html"] or "<h2>" in call_kwargs["html"]  # Markdown converted
            assert "**Bold text**" not in call_kwargs["html"]  # Not raw markdown

    def test_subject_prefix(self, mock_worker_context, mock_user):
        """Test that subject gets Swarmlet prefix."""
        with patch("zerg.tools.builtin.contact_user.get_worker_context") as mock_get_ctx, \
             patch("zerg.tools.builtin.contact_user.db_session") as mock_db_session, \
             patch("zerg.tools.builtin.contact_user.crud") as mock_crud, \
             patch("zerg.tools.builtin.contact_user.send_email") as mock_send_email:

            mock_get_ctx.return_value = mock_worker_context
            mock_db_session.return_value.__enter__.return_value = MagicMock()
            mock_crud.get_user.return_value = mock_user
            mock_send_email.return_value = {
                "ok": True,
                "data": {"message_id": "msg-123"},
            }

            result = contact_user(
                subject="Task completed",
                message="Done!",
            )

            assert result["ok"] is True

            # Verify subject prefix
            call_kwargs = mock_send_email.call_args[1]
            assert call_kwargs["subject"].startswith("[Swarmlet]")
            assert "Task completed" in call_kwargs["subject"]
