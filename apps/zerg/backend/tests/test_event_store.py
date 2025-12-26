"""Tests for event store service (Resumable SSE v1 Phase 1)."""

import pytest
from datetime import datetime
from sqlalchemy.orm import Session

from zerg.crud import crud as _crud
from zerg.models.agent_run_event import AgentRunEvent
from zerg.models.run import AgentRun
from zerg.models.enums import RunStatus, RunTrigger
from zerg.services.event_store import emit_run_event, EventStore


@pytest.fixture
def test_run(db_session: Session):
    """Create a test agent run for event storage tests."""
    # Get or create test user
    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    # Create a test agent
    from tests.conftest import TEST_MODEL
    from zerg.models.models import Agent

    agent = Agent(
        owner_id=owner.id,
        name="Test Event Agent",
        system_instructions="Test system instructions",
        task_instructions="Test task instructions",
        model=TEST_MODEL,
        status="idle",
    )
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    # Create a test thread
    from zerg.models.thread import Thread

    thread = Thread(
        agent_id=agent.id,
        title="Test Thread",
        active=True,
    )
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)

    # Create a test run
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.MANUAL,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    return run


@pytest.mark.asyncio
async def test_emit_run_event_persists_to_db(db_session: Session, test_run: AgentRun):
    """Test that emit_run_event persists events to the database."""
    payload = {
        "event_type": "supervisor_started",
        "run_id": test_run.id,
        "task": "Test task",
        "owner_id": 1,
    }

    event_id = await emit_run_event(
        db=db_session,
        run_id=test_run.id,
        event_type="supervisor_started",
        payload=payload,
    )

    # Verify event was persisted
    assert event_id > 0

    # Query the event from database
    event = db_session.query(AgentRunEvent).filter(AgentRunEvent.id == event_id).first()
    assert event is not None
    assert event.run_id == test_run.id
    assert event.event_type == "supervisor_started"
    assert event.sequence == 1
    assert event.payload == payload
    assert event.created_at is not None
    assert isinstance(event.created_at, datetime)


@pytest.mark.asyncio
async def test_sequence_numbers_are_monotonic(db_session: Session, test_run: AgentRun):
    """Test that sequence numbers increment correctly."""
    event_types = ["supervisor_started", "worker_spawned", "worker_complete", "supervisor_complete"]

    event_ids = []
    for event_type in event_types:
        payload = {"event_type": event_type, "run_id": test_run.id}
        event_id = await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=event_type,
            payload=payload,
        )
        event_ids.append(event_id)

    # Query all events and verify sequence numbers
    events = db_session.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == test_run.id
    ).order_by(AgentRunEvent.sequence).all()

    assert len(events) == 4
    for i, event in enumerate(events):
        assert event.sequence == i + 1
        assert event.event_type == event_types[i]


@pytest.mark.asyncio
async def test_invalid_payload_raises_valueerror(db_session: Session, test_run: AgentRun):
    """Test that non-JSON-serializable payloads raise ValueError."""
    import json

    # Create an object that jsonable_encoder can't handle
    # Use a circular reference which causes RecursionError in jsonable_encoder
    circular = {}
    circular['self'] = circular

    payload = {
        "event_type": "supervisor_started",
        "circular": circular,
    }

    # jsonable_encoder raises RecursionError for circular refs, which we catch and convert to ValueError
    with pytest.raises(ValueError, match="Invalid event payload"):
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type="supervisor_started",
            payload=payload,
        )


@pytest.mark.asyncio
async def test_get_events_after_returns_correct_events(db_session: Session, test_run: AgentRun):
    """Test that get_events_after returns events after a specific ID."""
    # Create several events
    event_ids = []
    for i in range(5):
        payload = {"event_type": f"event_{i}", "run_id": test_run.id}
        event_id = await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"event_{i}",
            payload=payload,
        )
        event_ids.append(event_id)

    # Get events after the 2nd event (should return events 3, 4, 5)
    events = EventStore.get_events_after(
        db=db_session,
        run_id=test_run.id,
        after_id=event_ids[1],
        include_tokens=True,
    )

    assert len(events) == 3
    assert events[0].id == event_ids[2]
    assert events[1].id == event_ids[3]
    assert events[2].id == event_ids[4]


@pytest.mark.asyncio
async def test_get_events_after_filters_tokens(db_session: Session, test_run: AgentRun):
    """Test that get_events_after can filter out token events."""
    # Create mixed events including tokens
    event_types = ["supervisor_started", "supervisor_token", "supervisor_token", "supervisor_complete"]

    for event_type in event_types:
        payload = {"event_type": event_type, "run_id": test_run.id}
        if event_type == "supervisor_token":
            payload["token"] = "test token"
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=event_type,
            payload=payload,
        )

    # Get events without tokens
    events = EventStore.get_events_after(
        db=db_session,
        run_id=test_run.id,
        after_id=0,
        include_tokens=False,
    )

    assert len(events) == 2
    assert events[0].event_type == "supervisor_started"
    assert events[1].event_type == "supervisor_complete"


@pytest.mark.asyncio
async def test_cascade_delete_works(db_session: Session, test_run: AgentRun):
    """Test that deleting a run cascades to delete its events."""
    run_id = test_run.id

    # Create events for the run
    for i in range(3):
        payload = {"event_type": f"event_{i}", "run_id": run_id}
        await emit_run_event(
            db=db_session,
            run_id=run_id,
            event_type=f"event_{i}",
            payload=payload,
        )

    # Verify events exist
    events_before = db_session.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == run_id
    ).count()
    assert events_before == 3

    # Delete the run - expunge first to avoid stale state issues
    db_session.expunge(test_run)
    db_session.query(AgentRun).filter(AgentRun.id == run_id).delete()
    db_session.commit()

    # Verify events were cascade deleted
    events_after = db_session.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == run_id
    ).count()
    assert events_after == 0


@pytest.mark.asyncio
async def test_get_latest_event_id(db_session: Session, test_run: AgentRun):
    """Test getting the latest event ID for a run."""
    # No events yet
    latest = EventStore.get_latest_event_id(db_session, test_run.id)
    assert latest is None

    # Create events
    event_ids = []
    for i in range(3):
        payload = {"event_type": f"event_{i}", "run_id": test_run.id}
        event_id = await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"event_{i}",
            payload=payload,
        )
        event_ids.append(event_id)

    # Latest should be the last event ID
    latest = EventStore.get_latest_event_id(db_session, test_run.id)
    assert latest == event_ids[-1]


@pytest.mark.asyncio
async def test_get_latest_sequence(db_session: Session, test_run: AgentRun):
    """Test getting the latest sequence number for a run."""
    # No events yet
    latest = EventStore.get_latest_sequence(db_session, test_run.id)
    assert latest is None

    # Create events
    for i in range(3):
        payload = {"event_type": f"event_{i}", "run_id": test_run.id}
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"event_{i}",
            payload=payload,
        )

    # Latest sequence should be 3
    latest = EventStore.get_latest_sequence(db_session, test_run.id)
    assert latest == 3


@pytest.mark.asyncio
async def test_delete_events_for_run(db_session: Session, test_run: AgentRun):
    """Test deleting all events for a run."""
    # Create events
    for i in range(3):
        payload = {"event_type": f"event_{i}", "run_id": test_run.id}
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"event_{i}",
            payload=payload,
        )

    # Verify events exist
    count_before = db_session.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == test_run.id
    ).count()
    assert count_before == 3

    # Delete events
    deleted_count = EventStore.delete_events_for_run(db_session, test_run.id)
    assert deleted_count == 3

    # Verify events were deleted
    count_after = db_session.query(AgentRunEvent).filter(
        AgentRunEvent.run_id == test_run.id
    ).count()
    assert count_after == 0


@pytest.mark.asyncio
async def test_get_event_count(db_session: Session, test_run: AgentRun):
    """Test getting event count with optional type filter."""
    # Create mixed event types
    event_types = ["supervisor_started", "worker_spawned", "worker_complete", "supervisor_complete"]

    for event_type in event_types:
        payload = {"event_type": event_type, "run_id": test_run.id}
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=event_type,
            payload=payload,
        )

    # Total count
    total = EventStore.get_event_count(db_session, test_run.id)
    assert total == 4

    # Count by type
    supervisor_count = EventStore.get_event_count(db_session, test_run.id, event_type="supervisor_started")
    assert supervisor_count == 1

    worker_count = EventStore.get_event_count(db_session, test_run.id, event_type="worker_spawned")
    assert worker_count == 1


@pytest.mark.asyncio
async def test_get_events_after_sequence(db_session: Session, test_run: AgentRun):
    """Test getting events after a specific sequence number."""
    # Create events
    for i in range(5):
        payload = {"event_type": f"event_{i}", "run_id": test_run.id}
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"event_{i}",
            payload=payload,
        )

    # Get events after sequence 2 (should return seq 3, 4, 5)
    events = EventStore.get_events_after_sequence(
        db=db_session,
        run_id=test_run.id,
        after_sequence=2,
        include_tokens=True,
    )

    assert len(events) == 3
    assert events[0].sequence == 3
    assert events[1].sequence == 4
    assert events[2].sequence == 5


@pytest.mark.asyncio
async def test_datetime_serialization(db_session: Session, test_run: AgentRun):
    """Test that datetime objects in payloads are serialized correctly."""
    from datetime import timezone
    payload = {
        "event_type": "supervisor_started",
        "run_id": test_run.id,
        "timestamp": datetime.now(timezone.utc),
    }

    # Should not raise ValueError
    event_id = await emit_run_event(
        db=db_session,
        run_id=test_run.id,
        event_type="supervisor_started",
        payload=payload,
    )

    # Verify event was persisted and datetime was serialized
    event = db_session.query(AgentRunEvent).filter(AgentRunEvent.id == event_id).first()
    assert event is not None
    assert "timestamp" in event.payload
    # Datetime should be serialized as ISO string
    assert isinstance(event.payload["timestamp"], str)


@pytest.mark.asyncio
async def test_multiple_runs_isolated_sequences(db_session: Session, test_run: AgentRun):
    """Test that sequence numbers are isolated per run."""
    # Create a second run
    run2 = AgentRun(
        agent_id=test_run.agent_id,
        thread_id=test_run.thread_id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.MANUAL,
    )
    db_session.add(run2)
    db_session.commit()
    db_session.refresh(run2)

    # Create events for both runs
    for i in range(3):
        await emit_run_event(
            db=db_session,
            run_id=test_run.id,
            event_type=f"run1_event_{i}",
            payload={"event_type": f"run1_event_{i}"},
        )

        await emit_run_event(
            db=db_session,
            run_id=run2.id,
            event_type=f"run2_event_{i}",
            payload={"event_type": f"run2_event_{i}"},
        )

    # Both runs should have sequence numbers starting at 1
    run1_events = EventStore.get_events_after(db_session, test_run.id)
    run2_events = EventStore.get_events_after(db_session, run2.id)

    assert len(run1_events) == 3
    assert len(run2_events) == 3

    # Each run should have sequences 1, 2, 3
    assert [e.sequence for e in run1_events] == [1, 2, 3]
    assert [e.sequence for e in run2_events] == [1, 2, 3]
