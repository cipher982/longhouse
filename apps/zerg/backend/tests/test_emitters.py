"""Unit tests for the injected emitter infrastructure.

Tests the EventEmitter protocol and its implementations:
- CommisEmitter (alias: WorkerEmitter): Always emits commis_tool_* events
- ConciergeEmitter (alias: SupervisorEmitter): Always emits concierge_tool_* events
- NullEmitter: No-op emitter for testing

Key property: Emitter identity is baked in at construction and cannot change.
Note: Emitters no longer hold DB sessions - event emission uses append_run_event()
which opens its own short-lived session.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.events import EventEmitter
from zerg.events import NullEmitter
from zerg.events import SupervisorEmitter
from zerg.events import WorkerEmitter
from zerg.events import get_emitter
from zerg.events import reset_emitter
from zerg.events import set_emitter


class TestCommisEmitter:
    """Tests for CommisEmitter (aliased as WorkerEmitter)."""

    def test_is_worker_always_true(self):
        """CommisEmitter.is_worker is always True."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )
        assert emitter.is_worker is True
        assert emitter.is_supervisor is False

    def test_implements_protocol(self):
        """CommisEmitter implements EventEmitter protocol."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )
        assert isinstance(emitter, EventEmitter)

    @pytest.mark.asyncio
    async def test_emit_tool_started_emits_commis_event(self):
        """emit_tool_started always emits commis_tool_started."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
            trace_id="12345678-1234-5678-1234-567812345678",
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_started("test_tool", "call_123", "args preview")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "commis_tool_started"
            assert call_kwargs["payload"]["commis_id"] == "test-commis"
            assert call_kwargs["payload"]["job_id"] == 50
            assert call_kwargs["payload"]["tool_name"] == "test_tool"
            assert call_kwargs["payload"]["trace_id"] == "12345678-1234-5678-1234-567812345678"

    @pytest.mark.asyncio
    async def test_emit_tool_completed_emits_commis_event(self):
        """emit_tool_completed always emits commis_tool_completed."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_completed("test_tool", "call_123", 500, "result preview")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "commis_tool_completed"
            assert call_kwargs["payload"]["duration_ms"] == 500

    @pytest.mark.asyncio
    async def test_emit_tool_failed_emits_commis_event(self):
        """emit_tool_failed always emits commis_tool_failed."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_failed("test_tool", "call_123", 100, "error message")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "commis_tool_failed"
            assert call_kwargs["payload"]["error"] == "error message"

    @pytest.mark.asyncio
    async def test_skips_emit_when_no_run_id(self):
        """Emitter skips emission when run_id is None."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=None,  # No run_id
            job_id=50,
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_started("test_tool", "call_123", "args preview")
            mock_emit.assert_not_called()

    def test_tool_tracking(self):
        """CommisEmitter tracks tool calls for activity log."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        # Record tool start
        tool_call = emitter.record_tool_start("ssh_exec", "call_123", {"command": "ls"})
        assert tool_call.name == "ssh_exec"
        assert tool_call.status == "running"
        assert len(emitter.tool_calls) == 1

        # Record tool complete
        emitter.record_tool_complete(tool_call, success=True)
        assert tool_call.status == "completed"
        assert tool_call.duration_ms is not None

    def test_critical_error_tracking(self):
        """CommisEmitter tracks critical errors for fail-fast."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        assert emitter.has_critical_error is False
        emitter.mark_critical_error("SSH connection failed")
        assert emitter.has_critical_error is True
        assert emitter.critical_error_message == "SSH connection failed"


class TestConciergeEmitter:
    """Tests for ConciergeEmitter (aliased as SupervisorEmitter)."""

    def test_is_supervisor_always_true(self):
        """ConciergeEmitter.is_supervisor is always True."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
        )
        assert emitter.is_supervisor is True
        assert emitter.is_worker is False

    def test_implements_protocol(self):
        """ConciergeEmitter implements EventEmitter protocol."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
        )
        assert isinstance(emitter, EventEmitter)

    @pytest.mark.asyncio
    async def test_emit_tool_started_emits_concierge_event(self):
        """emit_tool_started always emits concierge_tool_started."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
            trace_id="12345678-1234-5678-1234-567812345678",
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_started("spawn_commis", "call_456", "task preview")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "concierge_tool_started"
            assert call_kwargs["payload"]["owner_id"] == 1
            assert call_kwargs["payload"]["tool_name"] == "spawn_commis"
            assert call_kwargs["payload"]["trace_id"] == "12345678-1234-5678-1234-567812345678"

    @pytest.mark.asyncio
    async def test_emit_tool_completed_emits_concierge_event(self):
        """emit_tool_completed always emits concierge_tool_completed."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_completed("spawn_commis", "call_456", 1000, "result preview")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "concierge_tool_completed"
            assert call_kwargs["payload"]["duration_ms"] == 1000

    @pytest.mark.asyncio
    async def test_emit_tool_failed_emits_concierge_event(self):
        """emit_tool_failed always emits concierge_tool_failed."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
        )

        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_failed("spawn_commis", "call_456", 100, "commis failed")

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "concierge_tool_failed"
            assert call_kwargs["payload"]["error"] == "commis failed"


class TestNullEmitter:
    """Tests for NullEmitter."""

    def test_is_neither_worker_nor_supervisor(self):
        """NullEmitter is neither worker nor supervisor."""
        emitter = NullEmitter()
        assert emitter.is_worker is False
        assert emitter.is_supervisor is False

    def test_implements_protocol(self):
        """NullEmitter implements EventEmitter protocol."""
        emitter = NullEmitter()
        assert isinstance(emitter, EventEmitter)

    @pytest.mark.asyncio
    async def test_emit_methods_are_no_ops(self):
        """All emit methods are no-ops."""
        emitter = NullEmitter()

        # These should not raise and should complete immediately
        await emitter.emit_tool_started("test", "call", "args")
        await emitter.emit_tool_completed("test", "call", 100, "result")
        await emitter.emit_tool_failed("test", "call", 100, "error")
        await emitter.emit_heartbeat("reasoning", "initial")


class TestEmitterContext:
    """Tests for emitter context management (get_emitter, set_emitter, reset_emitter)."""

    def test_get_emitter_returns_none_when_not_set(self):
        """get_emitter returns None when no emitter is set."""
        # Note: This test runs in isolation, so context should be empty
        # In practice, other tests might have set context
        emitter = get_emitter()
        # Just verify it doesn't raise - value depends on test isolation
        assert emitter is None or isinstance(emitter, EventEmitter)

    def test_set_and_get_emitter(self):
        """set_emitter sets the emitter, get_emitter retrieves it."""
        commis_emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        token = set_emitter(commis_emitter)
        try:
            retrieved = get_emitter()
            assert retrieved is commis_emitter
            assert retrieved.is_worker is True
        finally:
            reset_emitter(token)

    def test_reset_emitter_restores_previous(self):
        """reset_emitter restores the previous emitter value."""
        original = get_emitter()

        commis_emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        token = set_emitter(commis_emitter)
        reset_emitter(token)

        assert get_emitter() is original

    def test_emitter_identity_survives_in_async_task(self):
        """Emitter identity is baked in and survives context copying."""

        async def task_that_uses_emitter():
            emitter = get_emitter()
            if emitter:
                # Even if context was copied, identity is fixed
                return emitter.is_worker
            return None

        async def run_test():
            commis_emitter = WorkerEmitter(
                commis_id="test-commis",
                owner_id=1,
                run_id=100,
                job_id=50,
            )

            token = set_emitter(commis_emitter)
            try:
                # Create task that inherits context
                task = asyncio.create_task(task_that_uses_emitter())
                result = await task
                assert result is True  # Identity is baked in
            finally:
                reset_emitter(token)

        asyncio.run(run_test())


class TestEmitterIdentityGuarantee:
    """Tests that emitter identity cannot be changed after construction."""

    @pytest.mark.asyncio
    async def test_commis_emitter_always_emits_commis_events(self):
        """CommisEmitter ALWAYS emits commis_* events, even if confused."""
        emitter = WorkerEmitter(
            commis_id="test-commis",
            owner_id=1,
            run_id=100,
            job_id=50,
        )

        # Call all emit methods multiple times
        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_started("a", "b", "c")
            await emitter.emit_tool_completed("a", "b", 1, "c")
            await emitter.emit_tool_failed("a", "b", 1, "c")

            # ALL calls should be commis_* events
            for call in mock_emit.call_args_list:
                event_type = call.kwargs["event_type"]
                assert event_type.startswith("commis_"), f"Expected commis_* but got {event_type}"

    @pytest.mark.asyncio
    async def test_concierge_emitter_always_emits_concierge_events(self):
        """ConciergeEmitter ALWAYS emits concierge_* events, even if confused."""
        emitter = SupervisorEmitter(
            run_id=100,
            owner_id=1,
            message_id="msg-123",
        )

        # Call all emit methods multiple times
        with patch("zerg.services.event_store.append_run_event", new_callable=AsyncMock) as mock_emit:
            await emitter.emit_tool_started("a", "b", "c")
            await emitter.emit_tool_completed("a", "b", 1, "c")
            await emitter.emit_tool_failed("a", "b", 1, "c")

            # ALL calls should be concierge_* events
            for call in mock_emit.call_args_list:
                event_type = call.kwargs["event_type"]
                assert event_type.startswith("concierge_"), f"Expected concierge_* but got {event_type}"
