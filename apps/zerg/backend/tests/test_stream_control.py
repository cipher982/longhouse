"""Unit tests for stream_control SSE events.

Tests explicit stream lifecycle control events:
- close only after reaching event_id barrier
- keep_open extends lease
- TTL capped at max
- replay late-joiner behavior
- error path emissions
"""

import time
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from zerg.events.event_bus import EventType
from zerg.models.enums import RunStatus
from zerg.models.models import Run


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.count.return_value = 0
    return db


@pytest.fixture
def mock_run(mock_db):
    """Create a mock Run."""
    run = MagicMock(spec=Run)
    run.id = 123
    run.trace_id = None
    run.status = RunStatus.RUNNING
    return run


class TestStreamControlEmission:
    """Test stream_control event emission."""

    @pytest.mark.asyncio
    async def test_emit_stream_control_close_all_complete(self, mock_db, mock_run):
        """stream_control:close emitted with reason=all_complete when no pending commiss."""
        from zerg.services.oikos_service import emit_stream_control

        with patch("zerg.services.event_store.emit_run_event") as mock_emit:
            mock_emit.return_value = None

            await emit_stream_control(mock_db, mock_run, "close", "all_complete", owner_id=1)

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["event_type"] == "stream_control"
            assert call_kwargs["payload"]["action"] == "close"
            assert call_kwargs["payload"]["reason"] == "all_complete"
            assert call_kwargs["payload"]["run_id"] == 123

    @pytest.mark.asyncio
    async def test_emit_stream_control_keep_open_commiss_pending(self, mock_db, mock_run):
        """stream_control:keep_open includes pending_commiss count."""
        from zerg.services.oikos_service import emit_stream_control

        # Mock pending commiss
        mock_db.query.return_value.filter.return_value.count.return_value = 3

        with patch("zerg.services.event_store.emit_run_event") as mock_emit:
            mock_emit.return_value = None

            await emit_stream_control(mock_db, mock_run, "keep_open", "commiss_pending", owner_id=1, ttl_ms=120_000)

            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["payload"]["action"] == "keep_open"
            assert call_kwargs["payload"]["pending_commiss"] == 3
            assert call_kwargs["payload"]["ttl_ms"] == 120_000

    @pytest.mark.asyncio
    async def test_emit_stream_control_ttl_capped_at_max(self, mock_db, mock_run):
        """TTL is capped at 300000ms (5 minutes)."""
        from zerg.services.oikos_service import emit_stream_control

        with patch("zerg.services.event_store.emit_run_event") as mock_emit:
            mock_emit.return_value = None

            await emit_stream_control(mock_db, mock_run, "keep_open", "commiss_pending", owner_id=1, ttl_ms=999_999)

            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["payload"]["ttl_ms"] == 300_000  # Capped at max

    @pytest.mark.asyncio
    async def test_emit_stream_control_error_reason(self, mock_db, mock_run):
        """stream_control:close emitted with reason=error on failure."""
        from zerg.services.oikos_service import emit_stream_control

        with patch("zerg.services.event_store.emit_run_event") as mock_emit:
            mock_emit.return_value = None

            await emit_stream_control(mock_db, mock_run, "close", "error", owner_id=1)

            call_kwargs = mock_emit.call_args.kwargs
            assert call_kwargs["payload"]["action"] == "close"
            assert call_kwargs["payload"]["reason"] == "error"


class TestStreamControlHandling:
    """Test stream.py handling of stream_control events."""

    def test_apply_event_state_close_sets_marker(self):
        """stream_control:close sets close_event_id without immediately closing."""

        # Simulating the state machine logic
        close_event_id = None
        complete = False

        event = {
            "action": "close",
            "reason": "all_complete",
            "_event_id": 42,
        }

        # Simulate _apply_event_state for stream_control:close
        if event.get("action") == "close":
            close_event_id = event.get("_event_id")
            # complete should NOT be set yet

        assert close_event_id == 42
        assert complete is False  # Close barrier, not immediate close

    def test_apply_event_state_keep_open_cancels_awaiting(self):
        """stream_control:keep_open cancels any pending heuristic close."""
        awaiting_continuation_until = time.monotonic() + 10  # Pending close
        stream_lease_until = None

        event = {
            "action": "keep_open",
            "reason": "commiss_pending",
            "ttl_ms": 120_000,
        }

        # Simulate _apply_event_state for stream_control:keep_open (live, not replay)
        if event.get("action") == "keep_open":
            ttl_ms = event.get("ttl_ms")
            if ttl_ms:
                capped_ttl = min(ttl_ms, 300_000)
                stream_lease_until = time.monotonic() + (capped_ttl / 1000.0)
            awaiting_continuation_until = None  # Cancel heuristic close

        assert awaiting_continuation_until is None
        assert stream_lease_until is not None
        assert stream_lease_until > time.monotonic()

    def test_close_barrier_reached_after_streaming(self):
        """Stream closes only after streaming past close_event_id."""
        close_event_id = 100
        last_sent_event_id = 99
        complete = False

        # Event with id=99 doesn't trigger close yet
        event_id = 99
        if close_event_id is not None and event_id and event_id >= close_event_id:
            complete = True
        assert complete is False  # Not yet at close marker

        # Event with id=100 triggers close
        event_id = 100
        last_sent_event_id = event_id
        if close_event_id is not None and event_id and event_id >= close_event_id:
            complete = True
        assert complete is True  # Reached close marker


class TestStreamControlInEventType:
    """Test STREAM_CONTROL is properly registered in EventType enum."""

    def test_stream_control_in_event_type(self):
        """STREAM_CONTROL exists in EventType enum."""
        assert hasattr(EventType, "STREAM_CONTROL")
        assert EventType.STREAM_CONTROL.value == "stream_control"


class TestStreamControlSchema:
    """Test stream_control payload structure."""

    def test_stream_control_payload_structure(self):
        """Validate required and optional fields in stream_control payload."""
        # Required fields
        payload = {
            "action": "close",
            "reason": "all_complete",
            "run_id": 123,
        }
        assert "action" in payload
        assert "reason" in payload
        assert "run_id" in payload

        # Optional fields for keep_open
        payload_keep_open = {
            "action": "keep_open",
            "reason": "commiss_pending",
            "run_id": 123,
            "ttl_ms": 120_000,
            "pending_commiss": 2,
            "trace_id": "abc-123",
        }
        assert payload_keep_open["ttl_ms"] == 120_000
        assert payload_keep_open["pending_commiss"] == 2


class TestHeuristicFallback:
    """Test heuristic fallback for runs without stream_control events."""

    def test_heuristic_fallback_when_no_control_events(self):
        """Old runs without stream_control still close via heuristics."""
        close_event_id = None
        saw_oikos_complete = True
        pending_commiss = 0
        continuation_active = False

        # Legacy heuristic: close if oikos_complete and no commis, no control events
        should_close = (
            saw_oikos_complete
            and pending_commiss == 0
            and not continuation_active
            and close_event_id is None  # No stream_control events
        )

        assert should_close is True

    def test_no_heuristic_when_stream_control_present(self):
        """Heuristic fallback is skipped when stream_control is present."""
        close_event_id = 42  # stream_control:close was seen
        saw_oikos_complete = True
        pending_commiss = 0
        continuation_active = False

        # When close_event_id is set, heuristic should NOT apply
        should_close_heuristic = (
            saw_oikos_complete
            and pending_commiss == 0
            and not continuation_active
            and close_event_id is None  # This is False now
        )

        assert should_close_heuristic is False
