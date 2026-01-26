"""Tests for commis context module."""

import asyncio

import pytest

from zerg.context import CommisContext
from zerg.context import get_commis_context
from zerg.context import reset_commis_context
from zerg.context import set_commis_context


class TestCommisContext:
    """Tests for CommisContext dataclass."""

    def test_create_context(self):
        """Test creating a commis context."""
        ctx = CommisContext(
            commis_id="test-commis-123",
            owner_id=1,
            course_id="run-abc",
            task="Check disk space",
        )
        assert ctx.commis_id == "test-commis-123"
        assert ctx.owner_id == 1
        assert ctx.course_id == "run-abc"
        assert ctx.task == "Check disk space"
        assert ctx.tool_calls == []

    def test_record_tool_start(self):
        """Test recording a tool call start."""
        ctx = CommisContext(commis_id="test")
        tool_call = ctx.record_tool_start(
            tool_name="ssh_exec",
            tool_call_id="call_123",
            args={"host": "cube", "command": "df -h"},
        )

        assert len(ctx.tool_calls) == 1
        assert tool_call.name == "ssh_exec"
        assert tool_call.tool_call_id == "call_123"
        assert tool_call.status == "running"
        assert tool_call.started_at is not None
        assert "host" in tool_call.args_preview

    def test_record_tool_complete_success(self):
        """Test recording a successful tool completion."""
        ctx = CommisContext(commis_id="test")
        tool_call = ctx.record_tool_start("ssh_exec")

        ctx.record_tool_complete(tool_call, success=True)

        assert tool_call.status == "completed"
        assert tool_call.completed_at is not None
        assert tool_call.duration_ms is not None
        assert tool_call.duration_ms >= 0
        assert tool_call.error is None

    def test_record_tool_complete_failure(self):
        """Test recording a failed tool completion."""
        ctx = CommisContext(commis_id="test")
        tool_call = ctx.record_tool_start("ssh_exec")

        ctx.record_tool_complete(
            tool_call,
            success=False,
            error="SSH connection refused",
        )

        assert tool_call.status == "failed"
        assert tool_call.error == "SSH connection refused"


class TestContextVar:
    """Tests for contextvar operations."""

    def test_get_without_set_returns_none(self):
        """Test that get_commis_context returns None when not set."""
        # Reset any existing context by setting and immediately resetting
        temp_ctx = CommisContext(commis_id="temp")
        token = set_commis_context(temp_ctx)
        reset_commis_context(token)

        # Now context should be None
        ctx = get_commis_context()
        assert ctx is None

    def test_set_and_get_context(self):
        """Test setting and getting commis context."""
        ctx = CommisContext(commis_id="test-123", owner_id=42)
        token = set_commis_context(ctx)

        try:
            retrieved = get_commis_context()
            assert retrieved is not None
            assert retrieved.commis_id == "test-123"
            assert retrieved.owner_id == 42
        finally:
            reset_commis_context(token)

    def test_reset_clears_context(self):
        """Test that reset clears the context."""
        ctx = CommisContext(commis_id="test")
        token = set_commis_context(ctx)
        reset_commis_context(token)

        # After reset, should be back to default (None)
        assert get_commis_context() is None

    def test_nested_contexts(self):
        """Test nested context setting (shouldn't happen, but verify behavior)."""
        ctx1 = CommisContext(commis_id="outer")
        token1 = set_commis_context(ctx1)

        try:
            assert get_commis_context().commis_id == "outer"

            ctx2 = CommisContext(commis_id="inner")
            token2 = set_commis_context(ctx2)

            try:
                assert get_commis_context().commis_id == "inner"
            finally:
                reset_commis_context(token2)

            # After resetting inner, should be back to outer
            assert get_commis_context().commis_id == "outer"
        finally:
            reset_commis_context(token1)


class TestContextVarAsyncPropagation:
    """Tests for contextvar propagation through async operations."""

    @pytest.mark.asyncio
    async def test_context_propagates_to_thread(self):
        """Test that context propagates through asyncio.to_thread."""
        ctx = CommisContext(commis_id="async-test", owner_id=99)
        token = set_commis_context(ctx)

        try:

            def check_in_thread():
                thread_ctx = get_commis_context()
                assert thread_ctx is not None
                return thread_ctx.commis_id

            result = await asyncio.to_thread(check_in_thread)
            assert result == "async-test"
        finally:
            reset_commis_context(token)

    @pytest.mark.asyncio
    async def test_context_isolated_between_tasks(self):
        """Test that context is isolated between concurrent tasks."""
        results = {}

        async def task_with_context(task_id: str):
            ctx = CommisContext(commis_id=f"task-{task_id}")
            token = set_commis_context(ctx)
            try:
                # Simulate some async work
                await asyncio.sleep(0.01)
                retrieved = get_commis_context()
                results[task_id] = retrieved.commis_id if retrieved else None
            finally:
                reset_commis_context(token)

        # Run multiple tasks concurrently
        await asyncio.gather(
            task_with_context("A"),
            task_with_context("B"),
            task_with_context("C"),
        )

        # Each task should have seen its own context
        assert results["A"] == "task-A"
        assert results["B"] == "task-B"
        assert results["C"] == "task-C"
