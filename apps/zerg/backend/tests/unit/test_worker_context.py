"""Tests for worker context module."""

import asyncio

import pytest

from zerg.context import WorkerContext
from zerg.context import get_worker_context
from zerg.context import reset_worker_context
from zerg.context import set_worker_context


class TestWorkerContext:
    """Tests for WorkerContext dataclass."""

    def test_create_context(self):
        """Test creating a worker context."""
        ctx = WorkerContext(
            worker_id="test-worker-123",
            owner_id=1,
            run_id="run-abc",
            task="Check disk space",
        )
        assert ctx.worker_id == "test-worker-123"
        assert ctx.owner_id == 1
        assert ctx.run_id == "run-abc"
        assert ctx.task == "Check disk space"
        assert ctx.tool_calls == []

    def test_record_tool_start(self):
        """Test recording a tool call start."""
        ctx = WorkerContext(worker_id="test")
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
        ctx = WorkerContext(worker_id="test")
        tool_call = ctx.record_tool_start("ssh_exec")

        ctx.record_tool_complete(tool_call, success=True)

        assert tool_call.status == "completed"
        assert tool_call.completed_at is not None
        assert tool_call.duration_ms is not None
        assert tool_call.duration_ms >= 0
        assert tool_call.error is None

    def test_record_tool_complete_failure(self):
        """Test recording a failed tool completion."""
        ctx = WorkerContext(worker_id="test")
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
        """Test that get_worker_context returns None when not set."""
        # Reset any existing context by setting and immediately resetting
        temp_ctx = WorkerContext(worker_id="temp")
        token = set_worker_context(temp_ctx)
        reset_worker_context(token)

        # Now context should be None
        ctx = get_worker_context()
        assert ctx is None

    def test_set_and_get_context(self):
        """Test setting and getting worker context."""
        ctx = WorkerContext(worker_id="test-123", owner_id=42)
        token = set_worker_context(ctx)

        try:
            retrieved = get_worker_context()
            assert retrieved is not None
            assert retrieved.worker_id == "test-123"
            assert retrieved.owner_id == 42
        finally:
            reset_worker_context(token)

    def test_reset_clears_context(self):
        """Test that reset clears the context."""
        ctx = WorkerContext(worker_id="test")
        token = set_worker_context(ctx)
        reset_worker_context(token)

        # After reset, should be back to default (None)
        assert get_worker_context() is None

    def test_nested_contexts(self):
        """Test nested context setting (shouldn't happen, but verify behavior)."""
        ctx1 = WorkerContext(worker_id="outer")
        token1 = set_worker_context(ctx1)

        try:
            assert get_worker_context().worker_id == "outer"

            ctx2 = WorkerContext(worker_id="inner")
            token2 = set_worker_context(ctx2)

            try:
                assert get_worker_context().worker_id == "inner"
            finally:
                reset_worker_context(token2)

            # After resetting inner, should be back to outer
            assert get_worker_context().worker_id == "outer"
        finally:
            reset_worker_context(token1)


class TestContextVarAsyncPropagation:
    """Tests for contextvar propagation through async operations."""

    @pytest.mark.asyncio
    async def test_context_propagates_to_thread(self):
        """Test that context propagates through asyncio.to_thread."""
        ctx = WorkerContext(worker_id="async-test", owner_id=99)
        token = set_worker_context(ctx)

        try:

            def check_in_thread():
                thread_ctx = get_worker_context()
                assert thread_ctx is not None
                return thread_ctx.worker_id

            result = await asyncio.to_thread(check_in_thread)
            assert result == "async-test"
        finally:
            reset_worker_context(token)

    @pytest.mark.asyncio
    async def test_context_isolated_between_tasks(self):
        """Test that context is isolated between concurrent tasks."""
        results = {}

        async def task_with_context(task_id: str):
            ctx = WorkerContext(worker_id=f"task-{task_id}")
            token = set_worker_context(ctx)
            try:
                # Simulate some async work
                await asyncio.sleep(0.01)
                retrieved = get_worker_context()
                results[task_id] = retrieved.worker_id if retrieved else None
            finally:
                reset_worker_context(token)

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
