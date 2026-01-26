"""Tests for durable runs v2.2 return_on_deferred behavior.

Tests the two timeout handling modes:
- return_on_deferred=True: Returns DEFERRED status immediately on timeout
- return_on_deferred=False: Emits DEFERRED event but continues to completion
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from zerg.events import EventType, event_bus
from zerg.models.enums import CourseStatus
from zerg.models.models import Course
from zerg.services.concierge_service import ConciergeService


class TestReturnOnDeferred:
    """Tests for the return_on_deferred parameter."""

    @pytest.fixture
    def captured_events(self):
        """Fixture to capture events during test."""
        events = []

        async def capture(event_data):
            events.append((event_data.get("event_type") or "event", event_data))

        event_bus.subscribe(EventType.CONCIERGE_DEFERRED, capture)
        event_bus.subscribe(EventType.CONCIERGE_COMPLETE, capture)
        event_bus.subscribe(EventType.ERROR, capture)

        yield events

        event_bus.unsubscribe(EventType.CONCIERGE_DEFERRED, capture)
        event_bus.unsubscribe(EventType.CONCIERGE_COMPLETE, capture)
        event_bus.unsubscribe(EventType.ERROR, capture)

    @pytest.mark.asyncio
    async def test_return_on_deferred_true_returns_immediately(
        self, db_session, test_user, captured_events
    ):
        """When return_on_deferred=True, returns DEFERRED status on timeout."""

        async def slow_success(*_args, **_kwargs):
            await asyncio.sleep(0.5)
            return [SimpleNamespace(role="assistant", content="completed")]

        service = ConciergeService(db_session)

        with (
            patch("zerg.services.checkpointer.get_checkpointer", return_value=MemorySaver()),
            patch("zerg.managers.fiche_runner.FicheRunner.run_thread", new=AsyncMock(side_effect=slow_success)),
        ):
            result = await service.run_concierge(
                owner_id=test_user.id,
                task="slow task",
                timeout=0.1,  # Short timeout to trigger deferred
                return_on_deferred=True,
            )

        # Should return immediately with deferred status
        assert result.status == "deferred"

        # DB status should be DEFERRED
        run = db_session.query(Course).filter(Course.id == result.course_id).first()
        assert run.status == CourseStatus.DEFERRED

        # Should have emitted CONCIERGE_DEFERRED event
        event_types = [e[0] for e in captured_events]
        assert EventType.CONCIERGE_DEFERRED in event_types

    @pytest.mark.asyncio
    async def test_return_on_deferred_false_continues_to_success(
        self, db_session, test_user, captured_events
    ):
        """When return_on_deferred=False, continues running until success."""

        async def slow_success(*_args, **_kwargs):
            await asyncio.sleep(0.3)
            return [SimpleNamespace(role="assistant", content="completed after timeout")]

        service = ConciergeService(db_session)

        with (
            patch("zerg.services.checkpointer.get_checkpointer", return_value=MemorySaver()),
            patch("zerg.managers.fiche_runner.FicheRunner.run_thread", new=AsyncMock(side_effect=slow_success)),
        ):
            result = await service.run_concierge(
                owner_id=test_user.id,
                task="slow task that completes",
                timeout=0.1,  # Short timeout
                return_on_deferred=False,  # Continue to completion
            )

        # Should return with success status after completion
        assert result.status == "success"

        # DB status should be SUCCESS
        run = db_session.query(Course).filter(Course.id == result.course_id).first()
        assert run.status == CourseStatus.SUCCESS

        # Should have both DEFERRED and COMPLETE events
        event_types = [e[0] for e in captured_events]
        assert EventType.CONCIERGE_DEFERRED in event_types
        assert EventType.CONCIERGE_COMPLETE in event_types

    @pytest.mark.asyncio
    async def test_return_on_deferred_false_continues_to_failure(
        self, db_session, test_user, captured_events
    ):
        """When return_on_deferred=False and task fails, returns failure."""

        async def slow_fail(*_args, **_kwargs):
            await asyncio.sleep(0.3)
            raise RuntimeError("simulated failure")

        service = ConciergeService(db_session)

        with (
            patch("zerg.services.checkpointer.get_checkpointer", return_value=MemorySaver()),
            patch("zerg.managers.fiche_runner.FicheRunner.run_thread", new=AsyncMock(side_effect=slow_fail)),
        ):
            result = await service.run_concierge(
                owner_id=test_user.id,
                task="slow task that fails",
                timeout=0.1,
                return_on_deferred=False,
            )

        # Should return with failed status
        assert result.status == "failed"
        assert "simulated failure" in result.error

        # DB status should be FAILED
        run = db_session.query(Course).filter(Course.id == result.course_id).first()
        assert run.status == CourseStatus.FAILED

        # Should have DEFERRED and ERROR events
        event_types = [e[0] for e in captured_events]
        assert EventType.CONCIERGE_DEFERRED in event_types
        assert EventType.ERROR in event_types

    @pytest.mark.asyncio
    async def test_no_timeout_completes_normally(
        self, db_session, test_user, captured_events
    ):
        """When no timeout occurs, completes normally without DEFERRED event."""

        async def fast_success(*_args, **_kwargs):
            return [SimpleNamespace(role="assistant", content="quick result")]

        service = ConciergeService(db_session)

        with (
            patch("zerg.services.checkpointer.get_checkpointer", return_value=MemorySaver()),
            patch("zerg.managers.fiche_runner.FicheRunner.run_thread", new=AsyncMock(side_effect=fast_success)),
        ):
            result = await service.run_concierge(
                owner_id=test_user.id,
                task="fast task",
                timeout=10,  # Long timeout - won't trigger
            )

        assert result.status == "success"

        # Should NOT have DEFERRED event
        event_types = [e[0] for e in captured_events]
        assert EventType.CONCIERGE_DEFERRED not in event_types
        assert EventType.CONCIERGE_COMPLETE in event_types
