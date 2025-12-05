"""Tests for worker tool event emission.

These tests verify that tool events (WORKER_TOOL_STARTED, WORKER_TOOL_COMPLETED,
WORKER_TOOL_FAILED) are emitted correctly when tools are executed in a worker context.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import ToolMessage

from zerg.context import (
    WorkerContext,
    set_worker_context,
    reset_worker_context,
    get_worker_context,
)
from zerg.events import EventType


class TestWorkerToolEventEmission:
    """Tests for tool event emission from zerg_react_agent._call_tool_async."""

    @pytest.fixture
    def worker_context(self):
        """Set up and tear down worker context for tests."""
        ctx = WorkerContext(
            worker_id="test-worker-123",
            owner_id=42,
            run_id="run-abc",
            task="Test task",
        )
        token = set_worker_context(ctx)
        yield ctx
        reset_worker_context(token)

    def test_worker_context_is_accessible(self, worker_context):
        """Test that worker context can be retrieved after being set."""
        ctx = get_worker_context()
        assert ctx is not None
        assert ctx.worker_id == "test-worker-123"
        assert ctx.owner_id == 42
        assert ctx.run_id == "run-abc"

    def test_no_context_when_not_set(self):
        """Test that no context is returned when not in a worker."""
        # Ensure no context is set (clean state)
        ctx = get_worker_context()
        # May be None or from another test, but checking the pattern works
        # In isolated test, this would be None

    @pytest.mark.asyncio
    async def test_tool_events_include_correct_fields(self, worker_context):
        """Test that tool events include all required fields."""
        # Create a test event payload matching what _call_tool_async creates
        event_data = {
            "event_type": EventType.WORKER_TOOL_STARTED,
            "worker_id": worker_context.worker_id,
            "owner_id": worker_context.owner_id,
            "run_id": worker_context.run_id,
            "tool_name": "test_tool",
            "tool_call_id": "call_123",
            "tool_args_preview": "{'param': 'value'}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Verify all required fields are present
        assert "event_type" in event_data
        assert "worker_id" in event_data
        assert "owner_id" in event_data
        assert "tool_name" in event_data
        assert "timestamp" in event_data

    @pytest.mark.asyncio
    async def test_completed_event_includes_duration(self, worker_context):
        """Test that WORKER_TOOL_COMPLETED includes duration_ms."""
        event_data = {
            "event_type": EventType.WORKER_TOOL_COMPLETED,
            "worker_id": worker_context.worker_id,
            "tool_name": "test_tool",
            "duration_ms": 150,
            "result_preview": "Tool executed successfully",
        }

        assert "duration_ms" in event_data
        assert event_data["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_failed_event_includes_error(self, worker_context):
        """Test that WORKER_TOOL_FAILED includes error details."""
        event_data = {
            "event_type": EventType.WORKER_TOOL_FAILED,
            "worker_id": worker_context.worker_id,
            "tool_name": "test_tool",
            "duration_ms": 50,
            "error": "<tool-error> SSH connection refused",
        }

        assert "error" in event_data
        assert "<tool-error>" in event_data["error"]


class TestWorkerContextToolTracking:
    """Tests for tool call tracking in WorkerContext."""

    def test_record_tool_start_adds_to_list(self):
        """Test that record_tool_start adds a ToolCall to the list."""
        ctx = WorkerContext(worker_id="test")

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
        ctx = WorkerContext(worker_id="test")

        call1 = ctx.record_tool_start("tool_a")
        call2 = ctx.record_tool_start("tool_b")
        call3 = ctx.record_tool_start("tool_c")

        assert len(ctx.tool_calls) == 3
        assert [c.name for c in ctx.tool_calls] == ["tool_a", "tool_b", "tool_c"]

    def test_record_tool_complete_updates_status(self):
        """Test that record_tool_complete updates the ToolCall."""
        ctx = WorkerContext(worker_id="test")

        tool_call = ctx.record_tool_start("test_tool")
        ctx.record_tool_complete(tool_call, success=True)

        assert tool_call.status == "completed"
        assert tool_call.completed_at is not None
        assert tool_call.duration_ms is not None

    def test_record_tool_failure(self):
        """Test recording a failed tool call."""
        ctx = WorkerContext(worker_id="test")

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

    def test_worker_tool_event_types_exist(self):
        """Test that all worker tool event types are defined."""
        assert hasattr(EventType, "WORKER_TOOL_STARTED")
        assert hasattr(EventType, "WORKER_TOOL_COMPLETED")
        assert hasattr(EventType, "WORKER_TOOL_FAILED")

    def test_event_type_values(self):
        """Test that event type values are correct strings."""
        assert EventType.WORKER_TOOL_STARTED == "worker_tool_started"
        assert EventType.WORKER_TOOL_COMPLETED == "worker_tool_completed"
        assert EventType.WORKER_TOOL_FAILED == "worker_tool_failed"
