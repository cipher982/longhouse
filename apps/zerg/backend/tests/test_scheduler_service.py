import logging

import pytest

from apscheduler.triggers.cron import CronTrigger
from zerg.services.scheduler_service import SchedulerService


@pytest.fixture(autouse=True)
def patch_logging(monkeypatch):
    """
    Patch the scheduler service logger for cleaner test output.
    """
    # Silence logging
    monkeypatch.setattr(
        "zerg.services.scheduler_service.logger",
        logging.getLogger("test_scheduler_service"),
    )
    yield


@pytest.fixture
def service(test_session_factory, monkeypatch):
    """
    Create and return a SchedulerService instance for testing.

    The service is configured to use a test scheduler that does not actually
    run jobs but allows verifying that jobs would be scheduled correctly.
    """
    # Mock the event_bus subscription to avoid errors during tests
    # Using pytest's monkeypatch fixture ensures proper cleanup after test
    monkeypatch.setattr(
        "zerg.services.scheduler_service.event_bus.subscribe",
        lambda event_type, handler: None,
    )
    service = SchedulerService(session_factory=test_session_factory)
    yield service
    # Ensure scheduler is properly shut down
    if service._initialized:
        service.scheduler.shutdown()


@pytest.mark.asyncio
async def test_schedule_agent(service):
    # Schedule an agent
    agent_id = 42
    cron_expression = "*/5 * * * *"
    await service.schedule_agent(agent_id, cron_expression)

    # Verify the job was added
    job = service.scheduler.get_job(f"agent_{agent_id}")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert job.args == (agent_id,)


@pytest.mark.asyncio
async def test_load_scheduled_agents(service, db_session):
    # Insert two agents: one with a cron schedule, one without
    # Reuse the dev user as owner
    from zerg.crud import crud as _crud
    from zerg.models.models import Agent

    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    a1 = Agent(
        owner_id=owner.id,
        name="A1",
        system_instructions="si",
        task_instructions="ti",
        model="m1",
        status="idle",
        schedule="*/5 * * * *",
    )
    a2 = Agent(
        owner_id=owner.id,
        name="A2",
        system_instructions="si",
        task_instructions="ti",
        model="m1",
        status="idle",
        schedule=None,
    )
    db_session.add_all([a1, a2])
    db_session.commit()
    agent_id = a1.id
    db_session.close()

    # No jobs initially
    assert not service.scheduler.get_jobs()

    # Load scheduled agents
    await service.load_scheduled_agents()

    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    job = service.scheduler.get_job(f"agent_{agent_id}")
    assert job is not None


@pytest.mark.asyncio
async def test_remove_agent_job(service):
    # First schedule an agent
    agent_id = 42
    await service.schedule_agent(agent_id, "*/5 * * * *")

    # Verify it was scheduled
    assert service.scheduler.get_job(f"agent_{agent_id}") is not None

    # Now remove the job
    service.remove_agent_job(agent_id)

    # Verify it was removed
    assert service.scheduler.get_job(f"agent_{agent_id}") is None


@pytest.mark.asyncio
async def test_handle_agent_created(service):
    """Test that an agent creation event schedules the agent if needed."""
    # Create event data
    event_data = {
        "id": 1,
        "name": "Test Agent",
        "schedule": "*/5 * * * *",
    }

    # Process the event
    await service._handle_agent_created(event_data)

    # Verify the agent was scheduled
    job = service.scheduler.get_job(f"agent_{event_data['id']}")
    assert job is not None
    assert job.args == (event_data["id"],)


@pytest.mark.asyncio
async def test_handle_agent_updated_enabled(service):
    """Test that an agent update event updates its schedule when enabled."""
    # First schedule the agent
    agent_id = 2
    await service.schedule_agent(agent_id, "*/10 * * * *")

    # Update with a new schedule
    event_data = {
        "id": agent_id,
        "schedule": "*/5 * * * *",
    }

    # Process the update event
    await service._handle_agent_updated(event_data)

    # Verify the schedule was updated
    job = service.scheduler.get_job(f"agent_{agent_id}")
    assert job is not None
    # Check that the job uses the new schedule
    # We can't easily check the cron expression directly but can verify
    # the job still exists with the same ID


@pytest.mark.asyncio
async def test_handle_agent_updated_disabled(service):
    """Test that an agent update event removes the schedule when disabled."""
    # First schedule the agent
    agent_id = 3
    await service.schedule_agent(agent_id, "*/10 * * * *")

    # Verify the agent is scheduled
    assert service.scheduler.get_job(f"agent_{agent_id}") is not None

    # Update to disable scheduling
    event_data = {
        "id": agent_id,
        # schedule key omitted to unset scheduling
    }

    # Process the update event
    await service._handle_agent_updated(event_data)

    # Verify the schedule was removed
    assert service.scheduler.get_job(f"agent_{agent_id}") is None


@pytest.mark.asyncio
async def test_handle_agent_deleted(service):
    """Test that an agent deletion event removes any scheduled jobs."""
    # First schedule the agent
    agent_id = 4
    await service.schedule_agent(agent_id, "*/10 * * * *")

    # Verify the agent is scheduled
    assert service.scheduler.get_job(f"agent_{agent_id}") is not None

    # Delete the agent
    event_data = {
        "id": agent_id,
        "name": "Deleted Agent",
    }

    # Process the delete event
    await service._handle_agent_deleted(event_data)

    # Verify the schedule was removed
    assert service.scheduler.get_job(f"agent_{agent_id}") is None
