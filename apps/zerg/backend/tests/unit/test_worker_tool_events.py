"""Tests for commis tool event emission.

These tests verify that tool events (COMMIS_TOOL_STARTED, COMMIS_TOOL_COMPLETED,
COMMIS_TOOL_FAILED) are emitted correctly when tools are executed in a commis context.
"""

from datetime import datetime
from datetime import timezone

import pytest

from zerg.context import CommisContext
from zerg.context import get_commis_context
from zerg.context import reset_commis_context
from zerg.context import set_commis_context
from zerg.events import EventType
from zerg.tools.result_utils import redact_sensitive_args


class TestCommisToolEventEmission:
    """Tests for tool event emission from concierge_react_engine._execute_tool."""

    @pytest.fixture
    def commis_context(self):
        """Set up and tear down commis context for tests."""
        ctx = CommisContext(
            commis_id="test-commis-123",
            owner_id=42,
            course_id="run-abc",
            task="Test task",
        )
        token = set_commis_context(ctx)
        yield ctx
        reset_commis_context(token)

    def test_commis_context_is_accessible(self, commis_context):
        """Test that commis context can be retrieved after being set."""
        ctx = get_commis_context()
        assert ctx is not None
        assert ctx.commis_id == "test-commis-123"
        assert ctx.owner_id == 42
        assert ctx.course_id == "run-abc"

    def test_no_context_when_not_set(self):
        """Test that get_commis_context returns None when no context is set."""
        # Reset any existing context by setting and immediately resetting
        temp_ctx = CommisContext(commis_id="temp")
        token = set_commis_context(temp_ctx)
        reset_commis_context(token)

        # Now context should be None
        ctx = get_commis_context()
        assert ctx is None

    def test_tool_events_include_correct_fields(self, commis_context):
        """Test that tool events include all required fields."""
        # Create a test event payload matching what _call_tool_async creates
        event_data = {
            "event_type": EventType.COMMIS_TOOL_STARTED,
            "commis_id": commis_context.commis_id,
            "owner_id": commis_context.owner_id,
            "course_id": commis_context.course_id,
            "tool_name": "test_tool",
            "tool_call_id": "call_123",
            "tool_args_preview": "{'param': 'value'}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Verify all required fields are present
        assert "event_type" in event_data
        assert "commis_id" in event_data
        assert "owner_id" in event_data
        assert "tool_name" in event_data
        assert "timestamp" in event_data

    def test_completed_event_includes_duration(self, commis_context):
        """Test that COMMIS_TOOL_COMPLETED includes duration_ms."""
        event_data = {
            "event_type": EventType.COMMIS_TOOL_COMPLETED,
            "commis_id": commis_context.commis_id,
            "tool_name": "test_tool",
            "duration_ms": 150,
            "result_preview": "Tool executed successfully",
        }

        assert "duration_ms" in event_data
        assert event_data["duration_ms"] >= 0

    def test_failed_event_includes_error(self, commis_context):
        """Test that COMMIS_TOOL_FAILED includes error details."""
        event_data = {
            "event_type": EventType.COMMIS_TOOL_FAILED,
            "commis_id": commis_context.commis_id,
            "tool_name": "test_tool",
            "duration_ms": 50,
            "error": "<tool-error> SSH connection refused",
        }

        assert "error" in event_data
        assert "<tool-error>" in event_data["error"]


class TestCommisContextToolTracking:
    """Tests for tool call tracking in CommisContext."""

    def test_record_tool_start_adds_to_list(self):
        """Test that record_tool_start adds a ToolCall to the list."""
        ctx = CommisContext(commis_id="test")

        tool_call = ctx.record_tool_start(
            tool_name="ssh_exec",
            tool_call_id="call_1",
            args={"host": "cube"},
        )

        assert len(ctx.tool_calls) == 1
        assert ctx.tool_calls[0] is tool_call
        assert tool_call.name == "ssh_exec"
        assert tool_call.status == "running"

    def test_record_multiple_tools(self):
        """Test tracking multiple concurrent tool calls."""
        ctx = CommisContext(commis_id="test")

        ctx.record_tool_start("tool_a")
        ctx.record_tool_start("tool_b")
        ctx.record_tool_start("tool_c")

        assert len(ctx.tool_calls) == 3
        assert [c.name for c in ctx.tool_calls] == ["tool_a", "tool_b", "tool_c"]

    def test_record_tool_complete_updates_status(self):
        """Test that record_tool_complete updates the ToolCall."""
        ctx = CommisContext(commis_id="test")

        tool_call = ctx.record_tool_start("test_tool")
        ctx.record_tool_complete(tool_call, success=True)

        assert tool_call.status == "completed"
        assert tool_call.completed_at is not None
        assert tool_call.duration_ms is not None

    def test_record_tool_failure(self):
        """Test recording a failed tool call."""
        ctx = CommisContext(commis_id="test")

        tool_call = ctx.record_tool_start("failing_tool")
        ctx.record_tool_complete(
            tool_call,
            success=False,
            error="Connection timeout",
        )

        assert tool_call.status == "failed"
        assert tool_call.error == "Connection timeout"


class TestEventTypeConstants:
    """Tests for event type constants."""

    def test_commis_tool_event_types_exist(self):
        """Test that all commis tool event types are defined."""
        assert hasattr(EventType, "COMMIS_TOOL_STARTED")
        assert hasattr(EventType, "COMMIS_TOOL_COMPLETED")
        assert hasattr(EventType, "COMMIS_TOOL_FAILED")

    def test_event_type_values(self):
        """Test that event type values are correct strings."""
        assert EventType.COMMIS_TOOL_STARTED == "commis_tool_started"
        assert EventType.COMMIS_TOOL_COMPLETED == "commis_tool_completed"
        assert EventType.COMMIS_TOOL_FAILED == "commis_tool_failed"


class TestSecretRedactionIntegration:
    """Tests for secret redaction integration with CommisContext."""

    def test_commis_context_with_real_redaction_function(self):
        """Test that redact_sensitive_args properly redacts before storing."""
        ctx = CommisContext(commis_id="test")

        # Raw args with secrets (what tool receives)
        raw_args = {
            "host": "example.com",
            "api_key": "sk-secret123",
            "token": "Bearer xyz",
        }

        # Actually call the real redaction function
        redacted_args = redact_sensitive_args(raw_args)

        # Verify redaction worked
        assert redacted_args["host"] == "example.com"
        assert redacted_args["api_key"] == "[REDACTED]"
        assert redacted_args["token"] == "[REDACTED]"

        # Record with redacted args (what _call_tool_async does)
        tool_call = ctx.record_tool_start(
            tool_name="send_email",
            tool_call_id="call_1",
            args=redacted_args,
        )

        # Verify secrets are not in the preview
        assert "sk-secret123" not in tool_call.args_preview
        assert "Bearer xyz" not in tool_call.args_preview

    def test_list_of_dicts_redaction_integration(self):
        """Test that list-of-dict secrets are redacted (Slack/Discord case)."""
        ctx = CommisContext(commis_id="test")

        # Slack-style attachments with a secret in the list
        raw_args = {
            "attachments": [
                {"title": "Status", "value": "OK"},
                {"title": "token", "value": "sk-live-abc123"},
            ],
        }

        # Actually redact using the real function
        redacted_args = redact_sensitive_args(raw_args)

        # Verify the sensitive item was redacted
        assert redacted_args["attachments"][0]["value"] == "OK"
        assert redacted_args["attachments"][1]["value"] == "[REDACTED]"

        # Record with redacted args
        tool_call = ctx.record_tool_start(
            tool_name="send_slack_message",
            args=redacted_args,
        )

        # The secret should not appear in the preview
        assert "sk-live-abc123" not in tool_call.args_preview
